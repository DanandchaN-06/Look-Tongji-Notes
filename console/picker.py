#!/usr/bin/env python3
"""Standalone native file/directory dialog. Standard library only.

This script is deliberately dependency-free: the console runs under the
project venv, which has no tkinter, so the dialog has to be executed by
whichever interpreter on this machine does have it.

It prints exactly one line of JSON to stdout:

    {"path": "C:/...", "cancelled": false}     --mode dir
    {"paths": ["C:/a.pdf"], "cancelled": false} --mode files
    {"cancelled": true}                         user cancelled, or --dry-run
    {"error": "..."}                            tkinter unavailable

Nothing else may be written to stdout, or the caller cannot parse the result.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

FILE_TYPES = [
    ("课程材料", "*.pdf *.pptx *.ppt *.docx *.doc *.xlsx *.xls *.md *.txt *.csv *.json"),
    ("所有文件", "*.*"),
]


def _emit(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="native picker helper")
    parser.add_argument("--mode", choices=["dir", "files"], required=True)
    parser.add_argument("--title", default="请选择")
    parser.add_argument("--initial", default="")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not open a window; report a cancellation. Used by tests and "
             "on machines without a desktop session.",
    )
    args = parser.parse_args()

    if args.dry_run:
        _emit({"cancelled": True, "dry_run": True})
        return 0

    try:
        import tkinter as tk
        from tkinter import filedialog
    except Exception as exc:  # pragma: no cover - depends on the interpreter
        _emit({"error": f"tkinter 不可用: {exc}"})
        return 2

    root = tk.Tk()
    root.withdraw()
    # Without this the dialog can open behind the browser window.
    try:
        root.attributes("-topmost", True)
    except Exception:
        pass
    try:
        root.update()
    except Exception:
        pass

    kwargs: dict[str, object] = {"title": args.title}
    if args.initial:
        candidate = args.initial.strip().strip('"')
        if os.path.isdir(candidate):
            kwargs["initialdir"] = candidate

    try:
        if args.mode == "dir":
            chosen = filedialog.askdirectory(**kwargs)
            _emit({"path": chosen or "", "cancelled": not chosen})
        else:
            chosen = filedialog.askopenfilenames(filetypes=FILE_TYPES, **kwargs)
            paths = [str(item) for item in (chosen or [])]
            _emit({"paths": paths, "cancelled": not paths})
    except Exception as exc:  # pragma: no cover
        _emit({"error": f"选择框执行失败: {exc}"})
        return 1
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
