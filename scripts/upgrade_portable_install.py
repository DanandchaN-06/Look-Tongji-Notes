"""Safely upgrade an existing portable install while preserving user data."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import subprocess
import time
import urllib.error
import urllib.request

APP_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = APP_ROOT / "project"
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from console.upgrade import install_program_files


EXCLUDED_PARTS = {
    ".git", ".venv", "venv", "node_modules", "__pycache__", "state", "runtime",
    "browsers", "data", "raw", "courses", "site", "tests", "test",
}
EXCLUDED_NAMES = {".env", "config.json", "python.local", "direct_url.json", "auth_session.json"}


def stop_idle_manager(target: Path) -> None:
    record_path = target / "data/manager-service.json"
    try:
        record = json.loads(record_path.read_text(encoding="utf-8"))
        port = int(record["port"])
        token = str(record["token"])
        pid = int(record["pid"])
    except (OSError, ValueError, TypeError, KeyError):
        return
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    base = f"http://127.0.0.1:{port}"
    headers = {"X-Console-Token": token}
    try:
        request = urllib.request.Request(base + "/__desktop_health__", headers=headers)
        with opener.open(request, timeout=2) as response:
            health = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        return
    if health.get("pid") != pid or health.get("token") != token or health.get("app") not in {
        "look-tongji-original-reader-v1", "look-tongji-manager-v2"
    }:
        return

    active: list[str] = []
    for endpoint, key in (("/api/tasks", "tasks"), ("/api/v2/workflows", "data")):
        try:
            request = urllib.request.Request(base + endpoint, headers=headers)
            with opener.open(request, timeout=2) as response:
                payload = json.load(response)
            values = payload.get(key, [])
            active.extend(
                str(item.get("id", "task")) for item in values
                if isinstance(item, dict) and item.get("state") in {"queued", "running"}
            )
        except (OSError, ValueError, urllib.error.URLError):
            continue
    if active:
        raise RuntimeError("旧管理器仍有运行或排队任务，请等待或取消任务后再升级。")
    stopped = subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, text=True, timeout=15, check=False,
    )
    if stopped.returncode:
        time.sleep(0.5)
        try:
            request = urllib.request.Request(base + "/__desktop_health__", headers=headers)
            with opener.open(request, timeout=1) as response:
                health_after = json.load(response)
        except (OSError, ValueError, urllib.error.URLError):
            return
        if health_after.get("pid") == pid and health_after.get("token") == token:
            raise RuntimeError("无法安全停止旧管理器服务，请重启电脑后再运行升级。")


def package_files() -> list[str]:
    files: list[str] = []
    for path in APP_ROOT.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(APP_ROOT)
        lower_parts = {part.lower() for part in relative.parts}
        if lower_parts & EXCLUDED_PARTS or path.name.lower() in EXCLUDED_NAMES:
            continue
        if path.suffix.lower() in {".pyc", ".pyo"}:
            continue
        files.append(relative.as_posix())
    return sorted(files)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target-app", required=True, type=Path, help="Existing installation's app directory")
    args = parser.parse_args()
    target = args.target_app.expanduser().resolve(strict=True)
    if not target.is_dir() or not (target / "launcher.py").is_file() or not (target / "runtime/python.exe").is_file():
        parser.error("Target must be an existing Look-Tongji portable app directory containing launcher.py and runtime/python.exe")
    if target == APP_ROOT.resolve() or target in APP_ROOT.resolve().parents or APP_ROOT.resolve() in target.parents:
        parser.error("Extract the new package to a separate folder before upgrading the existing app")
    backup_root = target.parent / "manager-program-backups"
    stop_idle_manager(target)
    backup = install_program_files(APP_ROOT, target, package_files(), backup_root)
    print(f"管理器程序已更新。原程序文件备份：{backup}")
    print("账号、设置、任务数据库、浏览器资料和课程资料保持原位。请从旧版唯一入口重新打开管理器。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
