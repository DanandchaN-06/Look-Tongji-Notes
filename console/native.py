"""Native OS dialogs, run in a separate interpreter.

Why this module exists:
- A browser can never hand the server a real local path (the File API exposes
  `C:\\fakepath\\...`), so directory/file selection has to happen natively.
- The console runs under the project venv, and that interpreter has **no
  tkinter**. The system Python on this machine does. So the dialog is executed
  by whichever interpreter actually provides tkinter, discovered once and
  cached.
- The dialog runs as its own short-lived process, which keeps a modal loop out
  of the HTTP handler threads entirely.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path
from typing import Any

from .core import CONSOLE_DIR, child_popen_kwargs

PICKER_SCRIPT = CONSOLE_DIR / "picker.py"

# Only one modal dialog at a time; a second request would be confusing.
DIALOG_LOCK = threading.Lock()

_probe_lock = threading.Lock()
_probe_done = False
_cached_python: str | None = None


def _has_tkinter(python: str) -> bool:
    try:
        proc = subprocess.run(
            [python, "-c", "import tkinter"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=25,
        )
    except Exception:
        return False
    return proc.returncode == 0


def _default_py_launcher_python() -> str | None:
    """Resolve what `py -3` points at, without assuming a version."""
    launcher = shutil.which("py")
    if not launcher:
        return None
    try:
        proc = subprocess.run(
            [launcher, "-3", "-c", "import sys;print(sys.executable)"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=25,
        )
    except Exception:
        return None
    if proc.returncode != 0:
        return None
    value = (proc.stdout or "").strip()
    return value or None


def _candidates() -> list[str]:
    raw: list[str | None] = [sys.executable]
    for name in ("python", "python3"):
        raw.append(shutil.which(name))
    raw.append(_default_py_launcher_python())

    seen: set[str] = set()
    ordered: list[str] = []
    for item in raw:
        if not item:
            continue
        key = os.path.normcase(os.path.abspath(item))
        if key in seen:
            continue
        seen.add(key)
        ordered.append(item)
    return ordered


def find_picker_python(force: bool = False) -> str | None:
    """Return an interpreter that can open tkinter dialogs, or None."""
    global _probe_done, _cached_python
    with _probe_lock:
        if _probe_done and not force:
            return _cached_python
        found: str | None = None
        for candidate in _candidates():
            try:
                exists = Path(candidate).exists()
            except OSError:
                exists = False
            if not exists and not shutil.which(candidate):
                continue
            if _has_tkinter(candidate):
                found = candidate
                break
        _cached_python = found
        _probe_done = True
        return found


def picker_available() -> bool:
    return find_picker_python() is not None


def _parse_payload(stdout: str) -> dict[str, Any] | None:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            parsed = json.loads(line)
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def run_picker(
    mode: str,
    *,
    initial: str = "",
    title: str = "请选择",
    timeout: float = 600.0,
) -> dict[str, Any]:
    """Open a native dialog and return `{path|paths, cancelled}`.

    Raises RuntimeError when no dialog-capable interpreter exists, or when the
    helper fails; the caller turns that into a user-facing message.
    """
    if mode not in ("dir", "files"):
        raise ValueError("mode 必须是 dir 或 files")

    python = find_picker_python()
    if python is None:
        raise RuntimeError(
            "本机未找到带 tkinter 的 Python 解释器，无法弹出系统选择框，请手动填写路径。"
        )

    args = [python, str(PICKER_SCRIPT), "--mode", mode, "--title", title]
    if initial:
        args += ["--initial", initial]
    # Also used by headless/remote sessions where no window can appear.
    if os.environ.get("LOOK_TONGJI_CONSOLE_NO_DIALOG") == "1":
        args.append("--dry-run")

    try:
        proc = subprocess.run(
            args,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=timeout,
            **child_popen_kwargs(),
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError("选择框等待超时，已关闭。请重试或手动填写路径。")

    payload = _parse_payload(proc.stdout or "")
    if payload is None:
        detail = (proc.stderr or proc.stdout or "").strip()[-300:]
        raise RuntimeError(f"选择框未返回结果（退出码 {proc.returncode}）。{detail}")
    if payload.get("error"):
        raise RuntimeError(str(payload["error"]))
    return payload
