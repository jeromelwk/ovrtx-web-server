"""Server-side filesystem browsing for the "Open File / Open Folder" dialog.

The frontend has no access to the server's filesystem beyond what this module
exposes, so it drives a small remote file browser instead of relying on the
browser's native (sandboxed) file picker.
"""

from __future__ import annotations

import os
import string
from dataclasses import dataclass, field

USD_EXTENSIONS = {".usd", ".usda", ".usdc", ".usdz"}


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool
    is_usd: bool = False


@dataclass
class BrowseResult:
    path: str
    parent: str | None
    entries: list[Entry] = field(default_factory=list)


def _is_usd_file(name: str) -> bool:
    return os.path.splitext(name)[1].lower() in USD_EXTENSIONS


def list_drives() -> list[Entry]:
    """Windows drive letters (used as the browser's root listing)."""
    drives = []
    for letter in string.ascii_uppercase:
        root = f"{letter}:\\"
        if os.path.exists(root):
            drives.append(Entry(name=root, path=root, is_dir=True))
    return drives


def browse(path: str | None) -> BrowseResult:
    """List a directory's contents, or the drive list when path is empty."""
    if not path or path == "":
        return BrowseResult(path="", parent=None, entries=list_drives())

    path = os.path.abspath(path)
    if not os.path.isdir(path):
        raise NotADirectoryError(path)

    entries: list[Entry] = []
    try:
        with os.scandir(path) as it:
            for de in it:
                try:
                    is_dir = de.is_dir()
                except OSError:
                    continue
                if is_dir:
                    entries.append(Entry(name=de.name, path=de.path, is_dir=True))
                elif _is_usd_file(de.name):
                    entries.append(Entry(name=de.name, path=de.path, is_dir=False, is_usd=True))
    except PermissionError as exc:
        raise PermissionError(path) from exc

    entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))

    parent = os.path.dirname(path.rstrip("\\/"))
    is_root = parent == path or parent == ""
    return BrowseResult(path=path, parent=None if is_root else parent, entries=entries)


def resolve_open_target(path: str) -> tuple[str | None, list[Entry]]:
    """Resolve a user-picked path to a single USD root file to open.

    Returns (resolved_file_path, candidates). If the path is already a USD
    file, it is returned directly. If it is a folder, this looks for a root
    USD file directly inside it: prefer one whose stem matches the folder
    name, otherwise fall back to the only candidate. When several candidates
    exist with no unambiguous match, ``resolved_file_path`` is None and
    ``candidates`` lists the choices for the caller (frontend) to present.
    """
    path = os.path.abspath(path)
    if os.path.isfile(path):
        if not _is_usd_file(path):
            raise ValueError(f"Not a USD file: {path}")
        return path, []

    if not os.path.isdir(path):
        raise FileNotFoundError(path)

    folder_name = os.path.basename(path.rstrip("\\/"))
    candidates: list[Entry] = []
    with os.scandir(path) as it:
        for de in it:
            if de.is_file() and _is_usd_file(de.name):
                candidates.append(Entry(name=de.name, path=de.path, is_dir=False, is_usd=True))

    if not candidates:
        raise FileNotFoundError(f"No .usd/.usda/.usdc/.usdz file found in folder: {path}")

    if len(candidates) == 1:
        return candidates[0].path, []

    for c in candidates:
        stem = os.path.splitext(c.name)[0]
        if stem.lower() == folder_name.lower():
            return c.path, []

    # Ambiguous: let the caller choose.
    return None, candidates
