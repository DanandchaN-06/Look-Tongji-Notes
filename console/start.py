"""Launcher for the Look Tongji Notes console.

Usage:
    python console/start.py [--port 8766] [--no-window] [--print-url]

It will:
1. make sure it runs on an interpreter that has the CLI dependencies,
2. bind a loopback-only HTTP server with a random session token,
3. open an Edge/Chrome app window (falls back to the default browser).
"""

from __future__ import annotations

import argparse
import os
import secrets
import socket
import subprocess
import sys
import time
from pathlib import Path

CONSOLE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CONSOLE_DIR.parent
if str(CONSOLE_DIR.parent) not in sys.path:
    sys.path.insert(0, str(CONSOLE_DIR.parent))

REQUIRED_MODULES = ("requests", "dotenv", "playwright")

# Standard project environments; no dependency on a particular Agent vendor.
VENV_CANDIDATES = [
    REPO_ROOT / ".venv" / "Scripts" / "python.exe",
    REPO_ROOT / ".venv" / "bin" / "python",
    REPO_ROOT / "venv" / "Scripts" / "python.exe",
    REPO_ROOT / "venv" / "bin" / "python",
]


def interpreter_candidates() -> list[Path]:
    candidates = list(VENV_CANDIDATES)
    configured = os.environ.get("LOOK_TONGJI_PYTHON", "").strip()
    local = CONSOLE_DIR / "python.local"
    if not configured and local.is_file():
        configured = local.read_text(encoding="utf-8-sig").strip()
    if configured:
        candidates.insert(0, Path(configured).expanduser())
    return candidates

BROWSER_CANDIDATES = [
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Microsoft" / "Edge" / "Application" / "msedge.exe",
    Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
    / "Google" / "Chrome" / "Application" / "chrome.exe",
    Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    / "Google" / "Chrome" / "Application" / "chrome.exe",
]


def missing_modules(python: str) -> list[str]:
    code = (
        "import importlib.util,sys;"
        f"mods={REQUIRED_MODULES!r};"
        "print('|'.join(m for m in mods if importlib.util.find_spec(m) is None))"
    )
    try:
        proc = subprocess.run(
            [python, "-c", code],
            capture_output=True, text=True, timeout=30, stdin=subprocess.DEVNULL,
        )
    except Exception:
        return list(REQUIRED_MODULES)
    if proc.returncode != 0:
        return list(REQUIRED_MODULES)
    out = (proc.stdout or "").strip()
    return [m for m in out.split("|") if m]


def ensure_interpreter() -> None:
    """Restart under an interpreter that can run the CLI, if needed.

    Deliberately does NOT use `os.execve`: on Windows it is emulated and has
    been observed to segfault, which crashed the launcher before the server
    ever started. A plain child process works identically on every platform and
    keeps stdout/stderr and Ctrl+C attached to the same console.

    Re-execution happens at most ONCE. Without that guard, a candidate that is
    reachable but still missing dependencies would loop forever.
    """
    current = sys.executable
    if not missing_modules(current):
        return

    if os.environ.get("LOOK_TONGJI_CONSOLE_REEXEC") == "1":
        print("[console] 已切换过解释器，但仍缺少依赖，继续使用当前解释器。", flush=True)
        print("[console] 缺少: " + ", ".join(missing_modules(current)), flush=True)
        return

    here = os.path.abspath(__file__)
    for candidate in interpreter_candidates():
        path = str(candidate)
        if not candidate.exists():
            continue
        if os.path.abspath(path) == os.path.abspath(current):
            continue
        if missing_modules(path):
            continue
        env = os.environ.copy()
        env["LOOK_TONGJI_CONSOLE_REEXEC"] = "1"
        print(f"[console] 切换到已配置的 Python 环境: {candidate}", flush=True)
        try:
            completed = subprocess.run([path, here, *sys.argv[1:]], env=env)
        except KeyboardInterrupt:
            raise SystemExit(130)
        raise SystemExit(completed.returncode)

    print("[console] 警告：当前 Python 环境缺少依赖: " + ", ".join(missing_modules(current)),
          flush=True)
    print("[console] 请先运行：python -m pip install -r console/requirements.txt", flush=True)


def _port_is_free(port: int) -> bool:
    """Probe whether a loopback port can actually be bound.

    Note: do NOT set SO_REUSEADDR here. On Windows it permits binding a port
    that another process is already listening on, so the probe would report a
    busy port as free.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def pick_port(preferred: int | None) -> int:
    if preferred:
        if _port_is_free(preferred):
            return preferred
        print(f"[console] 端口 {preferred} 被占用，改用随机端口。")
    for _ in range(30):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]
        if port and _port_is_free(port):
            return port
    raise RuntimeError("无法分配端口")


def open_window(url: str) -> None:
    for browser in BROWSER_CANDIDATES:
        if browser.exists():
            try:
                subprocess.Popen(  # noqa: S603 - fixed argv, no shell
                    [str(browser), f"--app={url}", "--no-first-run",
                     "--disable-features=Translate"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                print(f"[console] 已用 {browser.name} 打开应用窗口。")
                return
            except Exception:
                continue
    import webbrowser
    webbrowser.open(url)
    print("[console] 已用默认浏览器打开。")


def main() -> int:
    parser = argparse.ArgumentParser(description="Look Tongji Notes 本地控制台")
    parser.add_argument("--port", type=int, default=None, help="指定端口（默认自动选择）")
    parser.add_argument("--no-window", action="store_true", help="不自动打开窗口")
    parser.add_argument("--print-url", action="store_true", help="只打印访问地址")
    args = parser.parse_args()
    if args.port is not None and not 1 <= args.port <= 65535:
        parser.error("端口必须介于 1 和 65535 之间")

    ensure_interpreter()

    from console.server import make_server

    token = secrets.token_urlsafe(24)
    port = pick_port(args.port)
    httpd = make_server("127.0.0.1", port, token)
    url = f"http://127.0.0.1:{port}/?token={token}"

    print()
    print("  Look Tongji Notes 控制台", flush=True)
    print(f"  地址: {url}", flush=True)
    print("  仅本机可访问；在此终端按 Ctrl+C 停止服务。", flush=True)
    print(flush=True)

    if args.print_url:
        print(f"CONSOLE_URL={url}", flush=True)

    if not args.no_window:
        import threading
        threading.Thread(target=lambda: (time.sleep(0.4), open_window(url)), daemon=True).start()

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[console] 正在停止...")
    finally:
        httpd.shutdown()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
