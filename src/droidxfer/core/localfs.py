"""Mac file-system access confined to the user's home folder (plus, optionally, external drives)."""

from __future__ import annotations

import logging
import os
import shutil
import stat as statmod
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable

from .paths import PathError, unique_name, validate_name

log = logging.getLogger(__name__)

SKIP_NAMES = frozenset({".DS_Store"})
VOLUMES = Path("/Volumes")
TRASH_TOOL = "/usr/bin/trash"  # ships with macOS 15+; uses the system Trash (per-volume, Finder-aware)


@dataclass(slots=True)
class LocalFile:
    path: Path
    rel: str
    size: int
    mtime: int
    mode: int


class LocalFS:
    def __init__(
        self,
        root: Path | None = None,
        *,
        extra_roots: Iterable[Path] = (),
        extras_enabled: Callable[[], bool] = lambda: True,
    ) -> None:
        self.root = (root or Path.home()).resolve()
        self.trash_dir = self.root / ".Trash"
        self.extra_roots = tuple(Path(p) for p in extra_roots)
        self._extras_enabled = extras_enabled

    def roots(self) -> tuple[Path, ...]:
        return (self.root, *self.extra_roots) if self._extras_enabled() else (self.root,)

    def _root_of(self, p: Path) -> Path | None:
        for root in self.roots():
            if p == root or root in p.parents:
                return root
        return None

    def _inside(self, p: Path) -> bool:
        return self._root_of(p) is not None

    def resolve(self, path: str | os.PathLike[str], *, follow: bool = True) -> Path:
        raw = os.fspath(path)
        if not raw or "\x00" in raw:
            raise PathError("invalid path")
        if raw == "~" or raw.startswith("~/"):
            p = self.root / raw[2:]
        else:
            p = Path(raw)
        if not p.is_absolute():
            p = self.root / p
        if follow or p.name in ("", ".", ".."):
            resolved = p.resolve()
        else:
            resolved = p.parent.resolve() / p.name
        if not self._inside(resolved):
            where = " or ".join(str(r) for r in self.roots())
            raise PermissionError(f"{raw} is outside {where}")
        return resolved

    def info(self, p: Path) -> dict:
        lst = p.lstat()
        is_link = statmod.S_ISLNK(lst.st_mode)
        st = lst
        if is_link:
            try:
                st = p.stat()
            except OSError:
                pass
        is_dir = statmod.S_ISDIR(st.st_mode)
        hidden = p.name.startswith(".") or bool(getattr(lst, "st_flags", 0) & statmod.UF_HIDDEN)
        return {
            "name": p.name,
            "path": str(p),
            "is_dir": is_dir,
            "is_link": is_link,
            "size": 0 if is_dir else st.st_size,
            "mtime": int(st.st_mtime),
            "hidden": hidden,
        }

    def list(self, path: str, *, show_hidden: bool = False) -> dict:
        directory = self.resolve(path)
        if not directory.is_dir():
            raise NotADirectoryError(str(directory))
        entries = []
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    info = self.info(Path(entry.path))
                except OSError:
                    continue
                if info["hidden"] and not show_hidden:
                    continue
                entries.append(info)
        entries.sort(key=lambda e: (not e["is_dir"], e["name"].casefold()))
        root = self._root_of(directory) or self.root
        parent = str(directory.parent) if directory != root else None
        return {"path": str(directory), "parent": parent, "root": str(root), "entries": entries}

    def storage(self, path: str) -> dict:
        usage = shutil.disk_usage(self.resolve(path))
        return {"total": usage.total, "free": usage.free}

    def volumes(self) -> list[dict]:
        """External drives under /Volumes (the boot volume link to / is left out)."""
        result: list[dict] = []
        for extra in self.extra_roots if self._extras_enabled() else ():
            try:
                children = sorted(extra.iterdir())
            except OSError:
                continue
            for child in children:
                try:
                    if child.is_dir() and self._inside(child.resolve()) and not child.name.startswith("."):
                        result.append({"name": child.name, "path": str(child.resolve())})
                except OSError:
                    continue
        return result

    def _protected(self, p: Path) -> bool:
        """Roots, the Trash and whole drives (/Volumes/X) can't be renamed or deleted."""
        return p in self.roots() or p == self.trash_dir or any(p.parent == r for r in self.extra_roots)

    def exists(self, path: Path) -> bool:
        return os.path.lexists(path)

    def mkdir(self, parent: str, name: str) -> str:
        target = self.resolve(parent) / validate_name(name)
        target.mkdir()
        return str(target)

    def rename(self, path: str, new_name: str) -> str:
        src = self.resolve(path, follow=False)
        if self._protected(src):
            raise PathError(f"cannot rename {src}")
        dst = src.parent / validate_name(new_name)
        if self.exists(dst):
            raise FileExistsError(str(dst))
        src.rename(dst)
        return str(dst)

    def trash(self, paths: list[str]) -> list[str]:
        """Move items to the Trash instead of deleting them permanently."""
        sources = [self.resolve(raw, follow=False) for raw in paths]
        for src in sources:
            if self._protected(src):
                raise PathError(f"refusing to delete {src}")
        moved = []
        for src in sources:
            if not self.exists(src):
                raise FileNotFoundError(str(src))
            if not self._system_trash(src):
                self._manual_trash(src)
            moved.append(str(src))
        return moved

    def _system_trash(self, src: Path) -> bool:
        """Use macOS's own Trash (handles external drives); False if unavailable."""
        if not os.access(TRASH_TOOL, os.X_OK):
            return False
        try:
            res = subprocess.run([TRASH_TOOL, str(src)], capture_output=True, text=True, timeout=60)
        except (OSError, subprocess.SubprocessError) as exc:
            log.info("system trash failed for %s: %s", src, exc)
            return False
        if res.returncode == 0 and not self.exists(src):
            return True
        log.info("system trash failed for %s: %s", src, (res.stderr or res.stdout).strip())
        return False

    def _manual_trash(self, src: Path) -> None:
        self.trash_dir.mkdir(exist_ok=True)
        name = unique_name(src.name, lambda n: self.exists(self.trash_dir / n), is_dir=src.is_dir())
        shutil.move(str(src), str(self.trash_dir / name))

    def walk(self, top: Path) -> tuple[list[str], list[LocalFile]]:
        """Return (relative dirs, files) under `top`; skips symlinked dirs and links leaving the root."""
        dirs: list[str] = []
        files: list[LocalFile] = []
        for dirpath, dirnames, filenames in os.walk(top, followlinks=False):
            base = Path(dirpath)
            dirnames[:] = [d for d in dirnames if not (base / d).is_symlink()]
            for d in dirnames:
                dirs.append((base / d).relative_to(top).as_posix())
            for name in filenames:
                if name in SKIP_NAMES:
                    continue
                full = base / name
                if lf := self._file(full, full.relative_to(top).as_posix()):
                    files.append(lf)
        return dirs, files

    def file_entry(self, path: Path) -> LocalFile | None:
        return self._file(path, path.name)

    def _file(self, full: Path, rel: str) -> LocalFile | None:
        try:
            if full.is_symlink() and not self._inside(full.resolve()):
                return None
            st = full.stat()
        except OSError:
            return None
        if not statmod.S_ISREG(st.st_mode):
            return None
        return LocalFile(full, rel, st.st_size, int(st.st_mtime), st.st_mode)

    def move_into(self, sources: list[Path], dest_dir: str, conflict: str) -> list[str]:
        """Move staged upload items into a folder, applying the conflict policy to top-level names."""
        dest = self.resolve(dest_dir)
        if not dest.is_dir():
            raise NotADirectoryError(str(dest))
        placed = []
        for src in sources:
            target = dest / src.name
            if self.exists(target):
                if conflict == "skip":
                    continue
                if conflict == "overwrite":
                    self.trash([str(target)])
                else:
                    target = dest / unique_name(src.name, lambda n: self.exists(dest / n), is_dir=src.is_dir())
            shutil.move(str(src), str(target))
            placed.append(str(target))
        return placed
