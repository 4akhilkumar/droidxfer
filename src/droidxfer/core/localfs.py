"""Mac file-system access confined to a root folder (the user's home by default)."""

from __future__ import annotations

import os
import shutil
import stat as statmod
from dataclasses import dataclass
from pathlib import Path

from .paths import PathError, unique_name, validate_name

SKIP_NAMES = frozenset({".DS_Store"})


@dataclass(slots=True)
class LocalFile:
    path: Path
    rel: str
    size: int
    mtime: int
    mode: int


class LocalFS:
    def __init__(self, root: Path | None = None) -> None:
        self.root = (root or Path.home()).resolve()
        self.trash_dir = self.root / ".Trash"

    def _inside(self, p: Path) -> bool:
        return p == self.root or self.root in p.parents

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
            raise PermissionError(f"{raw} is outside {self.root}")
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
        parent = str(directory.parent) if directory != self.root else None
        return {"path": str(directory), "parent": parent, "root": str(self.root), "entries": entries}

    def exists(self, path: Path) -> bool:
        return os.path.lexists(path)

    def mkdir(self, parent: str, name: str) -> str:
        target = self.resolve(parent) / validate_name(name)
        target.mkdir()
        return str(target)

    def rename(self, path: str, new_name: str) -> str:
        src = self.resolve(path, follow=False)
        if src == self.root:
            raise PathError("cannot rename the root folder")
        dst = src.parent / validate_name(new_name)
        if self.exists(dst):
            raise FileExistsError(str(dst))
        src.rename(dst)
        return str(dst)

    def trash(self, paths: list[str]) -> list[str]:
        """Move items to ~/.Trash instead of deleting them permanently."""
        self.trash_dir.mkdir(exist_ok=True)
        moved = []
        for raw in paths:
            src = self.resolve(raw, follow=False)
            if src in (self.root, self.trash_dir):
                raise PathError(f"refusing to delete {src}")
            name = unique_name(src.name, lambda n: self.exists(self.trash_dir / n), is_dir=src.is_dir())
            shutil.move(str(src), str(self.trash_dir / name))
            moved.append(str(src))
        return moved

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
