"""Removing preinstalled apps ("debloating") over adb, without root, and undoing it.

Two reversible actions are offered for each package:
- disable:   `pm disable-user --user 0 PKG`  (app stays installed but can't run; undo: `pm enable`)
- uninstall: `pm uninstall --user 0 PKG`     (removed for the phone's main user; undo for system apps:
                                              `cmd package install-existing`; app data is not restored)
Nothing is removed from the read-only system partition, so a factory reset brings every app back.
Every action is recorded in a per-phone log under the app data folder so it can be undone later.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import tempfile
import time
from dataclasses import dataclass

from .appcatalog import AppCatalog
from .config import app_dir
from .devices import DeviceManager
from .remotefs import RemoteFS

ACTIONS = ("disable", "uninstall", "restore")
PACKAGE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z0-9_]+)+$")
_LIST_LINE = re.compile(r"^package:(?:(.*)=)?([A-Za-z0-9_.]+)(?: versionCode:(\d+))?(?: uid:(\d+))?\s*$")

# Never touched, not even in expert mode: removing these breaks the phone or cuts off adb itself.
CORE = frozenset({
    "android", "com.android.shell", "com.android.systemui", "com.android.settings",
    "com.android.providers.settings", "com.android.phone", "com.android.server.telecom",
    "com.android.permissioncontroller", "com.google.android.permissioncontroller",
    "com.android.packageinstaller", "com.google.android.packageinstaller",
})

LIST_COMMAND = "; ".join([
    "echo @@all; pm list packages -f -U --show-versioncode -u",
    "echo @@installed; pm list packages",
    "echo @@system; pm list packages -s -u",
    "echo @@disabled; pm list packages -d",
    "echo @@installers; pm list packages -3 -i",
    "echo @@home; cmd role get-role-holders --user 0 android.app.role.HOME 2>/dev/null",
    "echo @@sms; cmd role get-role-holders --user 0 android.app.role.SMS 2>/dev/null",
    "echo @@dialer; cmd role get-role-holders --user 0 android.app.role.DIALER 2>/dev/null",
    "echo @@ime; settings get secure default_input_method 2>/dev/null",
    "echo @@end",
])
# Apps outside the system partition that the maker's own services installed during setup ("removable
# preloads", common on Samsung). They count as preinstalled, but uninstalling removes them completely.
PRELOAD_INSTALLERS = {
    "com.samsung.android.app.omcagent": "Samsung",
    "com.samsung.android.app.updatecenter": "Samsung",
    "com.android.preload": "the manufacturer",
}
_INSTALLER_LINE = re.compile(r"^package:(\S+)\s+installer=(\S+)")
_ROLE_TEXT = {
    "home": "it's your current home screen app. Install and select another launcher first",
    "sms": "it's your default SMS app. Choose another messaging app first",
    "dialer": "it's your default phone app. Choose another phone app first",
    "ime": "it's your current keyboard. Switch to another keyboard first",
}


class AppActionError(Exception):
    pass


def _sections(text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        if line.startswith("@@"):
            current = out.setdefault(line[2:].strip(), [])
        elif current is not None and line.strip():
            current.append(line.strip())
    return out


def _names(lines: list[str]) -> set[str]:
    return {m.group(2) for line in lines if (m := _LIST_LINE.match(line))}


@dataclass(slots=True)
class AppState:
    package: str
    path: str
    version_code: int | None
    uid: int | None
    system: bool
    updated: bool  # a system app with a newer version installed from the Play Store / Galaxy Store
    enabled: bool
    installed: bool  # False: uninstalled for the main user
    preloaded_by: str | None = None  # maker that preinstalled a non-system app (removable preload)

    @property
    def state(self) -> str:
        if not self.installed:
            return "uninstalled"
        return "enabled" if self.enabled else "disabled"


def parse_listing(text: str) -> tuple[dict[str, AppState], dict[str, str]]:
    sec = _sections(text)
    installed, system, disabled = _names(sec.get("installed", [])), _names(sec.get("system", [])), _names(sec.get("disabled", []))
    installers = {m.group(1): m.group(2) for line in sec.get("installers", []) if (m := _INSTALLER_LINE.match(line))}
    apps: dict[str, AppState] = {}
    for line in sec.get("all", []):
        m = _LIST_LINE.match(line)
        if not m:
            continue
        path, pkg = m.group(1) or "", m.group(2)
        is_system = pkg in system
        apps[pkg] = AppState(
            package=pkg, path=path,
            version_code=int(m.group(3)) if m.group(3) else None,
            uid=int(m.group(4)) if m.group(4) else None,
            system=is_system,
            updated=is_system and path.startswith("/data/app/"),
            enabled=pkg not in disabled,
            installed=pkg in installed,
            preloaded_by=None if is_system else PRELOAD_INSTALLERS.get(installers.get(pkg, "")),
        )
    in_use: dict[str, str] = {}
    for role in ("home", "sms", "dialer"):
        for holder in sec.get(role, []):
            if PACKAGE_RE.match(holder):
                in_use.setdefault(holder, role)
    for value in sec.get("ime", []):
        pkg = value.split("/", 1)[0]
        if PACKAGE_RE.match(pkg):
            in_use.setdefault(pkg, "ime")
    return apps, in_use


class AppManager:
    def __init__(self, remote: RemoteFS, devices: DeviceManager, catalog: AppCatalog) -> None:
        self.remote = remote
        self.devices = devices
        self.catalog = catalog

    # listing -------------------------------------------------------------------------
    async def _state(self, device_id: str) -> tuple[dict[str, AppState], dict[str, str]]:
        res = await self.remote.shell(device_id, LIST_COMMAND, timeout=60)
        apps, in_use = parse_listing(res.text)
        if not apps:
            raise AppActionError("the phone returned no package list")
        return apps, in_use

    def _serial(self, device_id: str) -> str:
        t = self.devices.transport(device_id)
        return t.serialno or self.devices.get(device_id).id

    async def list(self, device_id: str) -> dict:
        apps, in_use = await self._state(device_id)
        history = self._load_history(self._serial(device_id))
        changed = {e["package"] for e in history["entries"] if e.get("ok")}
        rows = []
        for app in sorted(apps.values(), key=lambda a: a.package):
            rating = self.catalog.rate(app.package)
            blocked = self.block_reason(app.package, rating.rating, in_use, expert=True)
            rows.append({
                "package": app.package,
                "name": rating.name,
                "description": rating.description,
                "rating": rating.rating,
                "rating_source": rating.source,
                "community_rating": rating.community_rating,
                "system": app.system,
                "preloaded_by": app.preloaded_by,
                "updated": app.updated,
                "state": app.state,
                "version_code": app.version_code,
                "in_use": in_use.get(app.package),
                "core": blocked is not None,
                "changed_by_droidxfer": app.package in changed and app.state != "enabled",
            })
        return {"device_id": device_id, "apps": rows, "catalog": self.catalog.status()}

    # safety ----------------------------------------------------------------------------
    @staticmethod
    def block_reason(package: str, rating: str, in_use: dict[str, str], *, expert: bool) -> str | None:
        if package in CORE:
            return "it's a core part of Android; removing it would break the phone or cut off adb"
        if package in in_use:
            return _ROLE_TEXT[in_use[package]]
        if rating == "unsafe" and not expert:
            return "it's rated 'Don't remove'. Turn on expert mode to change it anyway"
        return None

    # actions ---------------------------------------------------------------------------
    async def plan(self, device_id: str, action: str, packages: list[str], *, expert: bool = False) -> list[dict]:
        """What `apply` would do, without changing anything."""
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {ACTIONS}")
        apps, in_use = await self._state(device_id)
        steps = []
        for pkg in dict.fromkeys(packages):
            step = {"package": pkg, "action": action, "commands": [], "skip": None}
            app = apps.get(pkg)
            if not PACKAGE_RE.match(pkg):
                step["skip"] = "not a valid package name"
            elif app is None:
                step["skip"] = "not on this phone"
            elif action == "restore":
                if not app.installed:
                    step["commands"].append(f"cmd package install-existing --user 0 {shlex.quote(pkg)}")
                if not app.enabled or not app.installed:
                    step["commands"].append(f"pm enable --user 0 {shlex.quote(pkg)}")
                if not step["commands"]:
                    step["skip"] = "already enabled"
            else:
                reason = self.block_reason(pkg, self.catalog.rate(pkg).rating, in_use, expert=expert)
                if reason:
                    step["skip"] = f"blocked: {reason}"
                elif action == "disable":
                    if not app.installed:
                        step["skip"] = "already uninstalled"
                    elif not app.enabled:
                        step["skip"] = "already disabled"
                    else:
                        step["commands"].append(f"pm disable-user --user 0 {shlex.quote(pkg)}")
                elif action == "uninstall":
                    if not app.installed:
                        step["skip"] = "already uninstalled"
                    else:
                        step["commands"].append(f"pm uninstall --user 0 {shlex.quote(pkg)}")
                        if app.preloaded_by:
                            step["warning"] = (f"preloaded by {app.preloaded_by}, not part of the system: it's removed "
                                               "completely; reinstall it from Galaxy Store or Play Store")
                        elif not app.system:
                            step["warning"] = "not a system app: it's removed completely; reinstall it from the app store"
            step["before"] = app.state if app else None
            steps.append(step)
        return steps

    async def apply(self, device_id: str, action: str, packages: list[str], *, expert: bool = False) -> list[dict]:
        steps = await self.plan(device_id, action, packages, expert=expert)
        serial = self._serial(device_id)
        results = []
        for step in steps:
            result = {"package": step["package"], "action": action, "before": step["before"]}
            if step["skip"]:
                results.append(result | {"ok": None, "message": step["skip"]})
                continue
            ok, message = True, ""
            for command in step["commands"]:
                res = await self.remote.shell(device_id, command, timeout=60)
                out = (res.stdout + res.stderr).decode("utf-8", "replace").strip()
                ok, message = _check(command, out)
                if not ok:
                    break
            results.append(result | {"ok": ok, "message": message})
            self._record(serial, device_id, result | {"ok": ok, "message": message})
        return results

    async def restore_all(self, device_id: str) -> list[dict]:
        apps, _ = await self._state(device_id)
        history = self._load_history(self._serial(device_id))
        touched = list(dict.fromkeys(e["package"] for e in history["entries"] if e.get("ok") and e["action"] != "restore"))
        pending = [p for p in touched if p in apps and apps[p].state != "enabled"]
        return await self.apply(device_id, "restore", pending)

    # history, export, import ---------------------------------------------------------------
    def _history_path(self, serial: str) -> os.PathLike:
        safe = re.sub(r"[^A-Za-z0-9_.-]", "_", serial) or "unknown"
        return app_dir() / "apps" / f"{safe}.json"

    def _load_history(self, serial: str) -> dict:
        try:
            data = json.loads(open(self._history_path(serial), encoding="utf-8").read())
            if isinstance(data, dict) and isinstance(data.get("entries"), list):
                return data
        except (OSError, ValueError):
            pass
        return {"serial": serial, "entries": []}

    def _record(self, serial: str, device_id: str, result: dict) -> None:
        path = self._history_path(serial)
        data = self._load_history(serial)
        device = self.devices.get(device_id).to_dict()
        data["device"] = {"name": device["name"], "android_version": device["android_version"]}
        data["entries"].append({"time": time.time(), **result})
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".apps-")
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, indent=1)
        os.replace(tmp, path)

    def history(self, device_id: str) -> dict:
        return self._load_history(self._serial(device_id))

    async def export(self, device_id: str) -> dict:
        """The changes droidxfer made on this phone that are still in effect, as a reusable list."""
        apps, _ = await self._state(device_id)
        history = self._load_history(self._serial(device_id))
        latest: dict[str, str] = {}
        for e in history["entries"]:
            if e.get("ok"):
                latest[e["package"]] = e["action"]
        device = self.devices.get(device_id).to_dict()
        packages = [
            {"package": pkg, "action": "uninstall" if apps[pkg].state == "uninstalled" else "disable"}
            for pkg, action in sorted(latest.items())
            if action != "restore" and pkg in apps and apps[pkg].state != "enabled"
        ]
        return {
            "format": "droidxfer-apps/1",
            "exported_at": time.time(),
            "device": {"name": device["name"], "model": device["model"], "android_version": device["android_version"]},
            "packages": packages,
        }

    async def import_plan(self, device_id: str, document: dict, *, expert: bool = False) -> list[dict]:
        if not isinstance(document, dict) or document.get("format") != "droidxfer-apps/1":
            raise ValueError("not a droidxfer app list (expected format droidxfer-apps/1)")
        entries = document.get("packages")
        if not isinstance(entries, list):
            raise ValueError("the app list has no packages")
        steps = []
        for action in ("disable", "uninstall"):
            pkgs = [e["package"] for e in entries if isinstance(e, dict) and e.get("action") == action
                    and isinstance(e.get("package"), str)]
            if pkgs:
                steps += await self.plan(device_id, action, pkgs, expert=expert)
        return steps


def _check(command: str, out: str) -> tuple[bool, str]:
    first = out.splitlines()[0] if out else ""
    if command.startswith("pm disable-user"):
        ok = "new state: disabled-user" in out
    elif command.startswith("pm enable"):
        ok = "new state: enabled" in out or "new state: default" in out
    elif command.startswith("pm uninstall"):
        ok = out.strip() == "Success"
    else:  # install-existing
        ok = "installed for user" in out
    if ok:
        return True, first
    # e.g. "Failure [DELETE_FAILED_INTERNAL_ERROR]" or "java.lang.SecurityException: Shell cannot change ..."
    message = first or "the phone gave no answer"
    if "SecurityException" in out or "DELETE_FAILED_INTERNAL_ERROR" in out or "not allowed" in out.lower():
        message = f"the phone refused: this package is protected ({first})"
    return False, message
