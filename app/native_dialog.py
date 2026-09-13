"""Native Windows "Open File" / "Select Folder" dialogs, launched from the
server so the browser-based UI can drive the real Explorer-style picker
instead of a custom in-app file browser.

Uses Tkinter's file dialogs, which call the real Win32 common-dialog APIs
directly in-process. A previous version shelled out to a PowerShell +
System.Windows.Forms subprocess for this, which was correct but slow to
appear (PowerShell startup plus JIT-loading the WinForms assembly each time);
Tkinter only needs to spin up a lightweight Tcl/Tk interpreter in-process, so
the dialog shows up close to instantly.

Blocks the calling thread until the user closes the dialog, so callers must
invoke these off the asyncio event loop (e.g. via ``asyncio.to_thread``).
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog
from typing import Optional

_FILE_TYPES = [
    ("Fichiers USD", "*.usd *.usda *.usdc *.usdz"),
    ("Tous les fichiers", "*.*"),
]


def _new_root() -> tk.Tk:
    root = tk.Tk()
    root.withdraw()
    root.attributes("-topmost", True)
    return root


def pick_file(initial_dir: str = "") -> Optional[str]:
    """Show the native "Open File" dialog. Returns the chosen path, or None
    if the user cancelled."""
    root = _new_root()
    try:
        path = filedialog.askopenfilename(
            parent=root,
            title="Ouvrir un fichier USD",
            initialdir=initial_dir or None,
            filetypes=_FILE_TYPES,
        )
    finally:
        root.destroy()
    return path or None


def pick_folder(initial_dir: str = "") -> Optional[str]:
    """Show the native "Select Folder" dialog. Returns the chosen path, or
    None if the user cancelled."""
    root = _new_root()
    try:
        path = filedialog.askdirectory(
            parent=root,
            title="Choisir un dossier d'assets USD",
            initialdir=initial_dir or None,
        )
    finally:
        root.destroy()
    return path or None
