"""Removing preinstalled apps: listing, safety guards, actions, undo log, export/import, ratings."""

import io
import json

import pytest

from droidxfer.core.appcatalog import AppCatalog, parse_uad
from droidxfer.core.apps import parse_listing

from test_api import authed, client  # noqa: F401  (fixtures)

TIPS = "com.google.android.apps.tips"


def _by_pkg(listing):
    return {a["package"]: a for a in listing["apps"]}


async def test_list_states_ratings_and_guards(ctx):
    apps = _by_pkg(await ctx.apps.list("FAKE123"))
    tips = apps[TIPS]
    assert (tips["name"], tips["rating"], tips["state"], tips["system"], tips["updated"]) == (
        "Pixel Tips", "safe", "enabled", True, True)
    assert apps["com.android.systemui"]["rating"] == "unsafe" and apps["com.android.systemui"]["core"]
    assert apps["com.google.android.apps.nexuslauncher"]["in_use"] == "home"
    assert apps["com.google.android.inputmethod.latin"]["in_use"] == "ime"
    assert apps["com.example.unrated"]["rating"] == "unknown"
    assert apps["com.example.game"]["system"] is False


async def test_disable_and_restore(ctx, fake_adb):
    plan = await ctx.apps.plan("FAKE123", "disable", [TIPS])
    assert plan[0]["commands"] == [f"pm disable-user --user 0 {TIPS}"]
    assert fake_adb.packages[TIPS]["enabled"]  # planning changes nothing
    results = await ctx.apps.apply("FAKE123", "disable", [TIPS])
    assert results[0]["ok"] and not fake_adb.packages[TIPS]["enabled"]
    tips = _by_pkg(await ctx.apps.list("FAKE123"))[TIPS]
    assert tips["state"] == "disabled" and tips["changed_by_droidxfer"]
    assert (await ctx.apps.apply("FAKE123", "disable", [TIPS]))[0]["message"] == "already disabled"
    results = await ctx.apps.apply("FAKE123", "restore", [TIPS])
    assert results[0]["ok"] and fake_adb.packages[TIPS]["enabled"]
    history = ctx.apps.history("FAKE123")
    assert [(e["action"], e["ok"]) for e in history["entries"]] == [("disable", True), ("restore", True)]


async def test_uninstall_for_user_and_restore(ctx, fake_adb):
    results = await ctx.apps.apply("FAKE123", "uninstall", ["com.google.android.youtube"])
    assert results[0]["ok"] and not fake_adb.packages["com.google.android.youtube"]["installed"]
    assert _by_pkg(await ctx.apps.list("FAKE123"))["com.google.android.youtube"]["state"] == "uninstalled"
    plan = await ctx.apps.plan("FAKE123", "restore", ["com.google.android.youtube"])
    assert plan[0]["commands"][0].startswith("cmd package install-existing --user 0")
    assert (await ctx.apps.apply("FAKE123", "restore", ["com.google.android.youtube"]))[0]["ok"]
    assert fake_adb.packages["com.google.android.youtube"]["installed"]


async def test_user_app_uninstall_warns(ctx):
    plan = await ctx.apps.plan("FAKE123", "uninstall", ["com.example.game"])
    assert "removed completely" in plan[0]["warning"]


async def test_samsung_removable_preloads(ctx):
    apps = _by_pkg(await ctx.apps.list("FAKE123"))
    assert apps["com.samsung.android.app.tips"]["preloaded_by"] == "Samsung"
    assert apps["com.samsung.android.app.tips"]["system"] is False
    assert apps["com.example.game"]["preloaded_by"] is None  # from the Play Store
    plan = await ctx.apps.plan("FAKE123", "uninstall", ["com.samsung.android.app.tips"])
    assert "preloaded by Samsung" in plan[0]["warning"] and "Galaxy Store" in plan[0]["warning"]


async def test_safety_guards(ctx, fake_adb):
    steps = {s["package"]: s for s in await ctx.apps.plan(
        "FAKE123", "disable",
        ["com.android.systemui", "com.google.android.apps.nexuslauncher", "com.google.android.gms", "bad name;rm", "no.such.pkg"],
    )}
    assert "core part of Android" in steps["com.android.systemui"]["skip"]
    assert "home screen" in steps["com.google.android.apps.nexuslauncher"]["skip"]
    assert "expert mode" in steps["com.google.android.gms"]["skip"]
    assert steps["bad name;rm"]["skip"] == "not a valid package name"
    assert steps["no.such.pkg"]["skip"] == "not on this phone"
    # Expert mode lifts the rating block but never the core/in-use blocks.
    expert = {s["package"]: s for s in await ctx.apps.plan(
        "FAKE123", "disable", ["com.android.systemui", "com.google.android.gms"], expert=True)}
    assert expert["com.android.systemui"]["skip"] and not expert["com.google.android.gms"]["skip"]
    # ...and the phone itself can still refuse.
    result = (await ctx.apps.apply("FAKE123", "disable", ["com.google.android.gms"], expert=True))[0]
    assert result["ok"] is False and "protected" in result["message"]
    assert fake_adb.packages["com.google.android.gms"]["enabled"]
    assert all(f["enabled"] and f["installed"] for f in fake_adb.packages.values())


async def test_restore_all(ctx, fake_adb):
    await ctx.apps.apply("FAKE123", "disable", [TIPS, "com.facebook.appmanager"])
    await ctx.apps.apply("FAKE123", "uninstall", ["com.google.android.youtube"])
    results = await ctx.apps.restore_all("FAKE123")
    assert sorted(r["package"] for r in results if r["ok"]) == sorted(
        [TIPS, "com.facebook.appmanager", "com.google.android.youtube"])
    assert all(f["enabled"] and f["installed"] for f in fake_adb.packages.values())


async def test_export_and_import(ctx, fake_adb):
    await ctx.apps.apply("FAKE123", "disable", [TIPS])
    await ctx.apps.apply("FAKE123", "uninstall", ["com.facebook.appmanager"])
    doc = await ctx.apps.export("FAKE123")
    assert doc["format"] == "droidxfer-apps/1"
    assert doc["packages"] == [
        {"package": "com.facebook.appmanager", "action": "uninstall"},
        {"package": TIPS, "action": "disable"},
    ]
    await ctx.apps.restore_all("FAKE123")  # pretend it's another phone in its factory state
    steps = await ctx.apps.import_plan("FAKE123", json.loads(json.dumps(doc)))
    assert sorted((s["package"], s["action"], s["skip"]) for s in steps) == [
        ("com.facebook.appmanager", "uninstall", None), (TIPS, "disable", None)]
    with pytest.raises(ValueError):
        await ctx.apps.import_plan("FAKE123", {"format": "something-else"})


def test_parse_listing_handles_odd_paths():
    text = "\n".join([
        "@@all",
        "package:/data/app/~~E4Hd-8==/com.a-F9==/base.apk=com.a versionCode:5 uid:10001",
        "package:com.b",
        "@@installed", "package:com.a", "@@system", "package:com.a", "package:com.b",
        "@@disabled", "@@home", "com.a", "@@ime", "null", "@@end",
    ])
    apps, in_use = parse_listing(text)
    assert apps["com.a"].path.endswith("base.apk") and apps["com.a"].updated and apps["com.a"].version_code == 5
    assert apps["com.b"].state == "uninstalled"
    assert in_use == {"com.a": "home"}


# ratings ---------------------------------------------------------------------------------
UAD = {f"pkg.n{i}": {"removal": "Recommended", "description": "x", "list": "Oem"} for i in range(150)}
UAD |= {
    "com.example.unrated": {"removal": "Expert", "description": "Community says careful", "list": "Oem"},
    TIPS: {"removal": "Unsafe", "description": "community disagrees", "list": "Google"},
}


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_curated_catalog_is_valid():
    catalog = AppCatalog()
    assert len(catalog.curated) > 150
    assert {e["rating"] for e in catalog.curated.values()} == {"safe", "probably_safe", "advanced", "unsafe"}
    assert all(e["name"] and e["description"] for e in catalog.curated.values())


def test_community_list_fetch_and_merge(tmp_path):
    catalog = AppCatalog(cache=tmp_path)
    assert catalog.rate("com.example.unrated").rating == "unknown"
    status = catalog.fetch_community(opener=lambda req, timeout: _Response(json.dumps(UAD).encode()))
    assert status["community_count"] == 152 and status["community_fetched_at"]
    rated = catalog.rate("com.example.unrated")
    assert (rated.rating, rated.source, rated.description) == ("advanced", "community", "Community says careful")
    tips = catalog.rate(TIPS)
    assert (tips.rating, tips.source, tips.community_rating) == ("safe", "droidxfer", "Unsafe")  # curated wins
    assert AppCatalog(cache=tmp_path).community_fetched_at == status["community_fetched_at"]  # cached on disk
    with pytest.raises(ValueError):
        parse_uad({"too": "small"})
    with pytest.raises(ValueError):
        catalog.fetch_community(opener=lambda req, timeout: _Response(b"<html>not json</html>"))


# API -------------------------------------------------------------------------------------
def test_apps_api(authed, fake_adb, monkeypatch):  # noqa: F811
    base = "/api/devices/FAKE123/apps"
    listing = authed.get(base).json()
    assert _by_pkg(listing)[TIPS]["state"] == "enabled" and listing["catalog"]["curated_count"] > 150
    plan = authed.post(f"{base}/plan", json={"action": "disable", "packages": [TIPS]}).json()["steps"]
    assert plan[0]["commands"] and fake_adb.packages[TIPS]["enabled"]
    results = authed.post(f"{base}/apply", json={"action": "disable", "packages": [TIPS]}).json()["results"]
    assert results[0]["ok"] and not fake_adb.packages[TIPS]["enabled"]
    doc = authed.get(f"{base}/export").json()
    assert doc["packages"] == [{"package": TIPS, "action": "disable"}]
    assert authed.get(f"{base}/history").json()["entries"][0]["package"] == TIPS
    assert authed.post(f"{base}/restore-all").json()["results"][0]["ok"]
    assert fake_adb.packages[TIPS]["enabled"]
    steps = authed.post(f"{base}/import-plan", json={"document": doc}).json()["steps"]
    assert steps[0]["package"] == TIPS and not steps[0]["skip"]
    assert authed.post(f"{base}/apply", json={"action": "explode", "packages": [TIPS]}).status_code == 422

    def offline(*a, **k):
        raise OSError("network unreachable")

    monkeypatch.setattr(AppCatalog, "fetch_community", offline)
    r = authed.post("/api/apps/catalog/fetch")
    assert r.status_code == 502 and "network unreachable" in r.json()["detail"]


def test_cli_dry_run_changes_nothing(monkeypatch, fake_adb, mac_home):
    from typer.testing import CliRunner

    from droidxfer import cli

    original = cli.AppContext
    monkeypatch.setattr(cli, "AppContext", lambda: original(adb_port=fake_adb.port, local_root=mac_home))
    result = CliRunner().invoke(cli.app, ["apps", "uninstall", TIPS, "com.android.systemui", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "pm uninstall --user 0" in result.output and "core part of Android" in result.output
    assert fake_adb.packages[TIPS]["installed"]


def test_catalog_patterns(tmp_path):
    catalog = AppCatalog(cache=tmp_path)
    voice = catalog.rate("com.samsung.SMT.lang_de_de_f00")
    assert (voice.rating, voice.name) == ("safe", "Samsung text-to-speech voice")
    assert catalog.rate("com.samsung.android.wifi.resources.lls").rating == "advanced"
    assert catalog.rate("com.google.android.overlay.modules.mediaprovider").name == "System overlay"
    assert catalog.rate("com.android.virtualization.terminal").name == "Linux terminal"  # exact entry
    assert catalog.rate("com.example.resourcesless").rating == "unknown"
    # per-package community knowledge beats a generic pattern
    catalog.community = {"com.samsung.SMT.lang_de_de_f00": {"removal": "Expert", "description": "d", "list": "Oem"}}
    assert catalog.rate("com.samsung.SMT.lang_de_de_f00").source == "community"
