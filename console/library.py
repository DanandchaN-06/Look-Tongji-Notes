"""Read-only library summaries, batch recovery and publishing preparation."""
import json
import os
import stat
import re
import shutil
import subprocess
from pathlib import Path

from . import config_store
from .core import REPO_ROOT, secret_values
from tongji_backend.checkpoints import path_for


def batch_state(course_id: str) -> dict:
    config = config_store.load_workspace_config()
    result = {"available": False, "course_id": course_id, "total": 0, "done": 0, "failed": 0, "pending": 0, "running": 0}
    if config is None or not course_id:
        return result
    for path in (path_for(config.workspace_root, course_id), config.workspace_root / "batch_state.json"):
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(data, dict) or data.get("course_id") != course_id or not isinstance(data.get("lectures"), list):
            continue
        rows = [row for row in data["lectures"] if isinstance(row, dict)]
        result.update(available=True, total=len(rows), path=str(path), updated_at=str(data.get("updated_at") or ""))
        for key, state in (("done", "done"), ("failed", "failed"), ("pending", "pending"), ("running", "in_progress")):
            result[key] = sum(row.get("status") == state for row in rows)
        return result
    return result


def publish_preflight() -> dict:
    workspace = config_store.public_workspace() or {}
    root = Path(workspace["workspace_root"]) if workspace.get("workspace_root") else None
    site = root / "site" if root else None
    repository = config_store.public_credentials().get("gh_pages_repo", "")
    gh = shutil.which("gh")
    authenticated = False
    if gh:
        try:
            result = subprocess.run([gh, "auth", "status"], cwd=REPO_ROOT, stdin=subprocess.DEVNULL,
                                    capture_output=True, timeout=30)
            authenticated = result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            pass
    findings = []
    scanned = 0
    values = [value for value in secret_values() if len(value) >= 4]
    def linked(path):
        try:
            attributes = getattr(path.lstat(), "st_file_attributes", 0)
            return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 1024))
        except OSError:
            return True
    def entries(root):
        for parent, directories, filenames in os.walk(root, followlinks=False):
            for name in list(directories):
                path = Path(parent) / name
                if linked(path):
                    directories.remove(name)
                    yield path
            for name in filenames:
                yield Path(parent) / name
    if site and linked(site):
        findings.append({"path": "site/", "reason": "阅读网页目录是外部目录链接，需要人工确认"})
    elif site and site.is_dir():
        for path in entries(site):
            relative = path.relative_to(site).as_posix()
            if linked(path):
                findings.append({"path": relative, "reason": "包含外部目录或文件链接"})
                continue
            if not path.is_file():
                continue
            scanned += 1
            if scanned > 20000:
                findings.append({"path": "site/", "reason": "文件过多，需人工检查后再发布"})
                break
            if path.is_symlink() or path.name == ".env" or any(part in ("state", ".git", ".venv") for part in path.relative_to(site).parts):
                findings.append({"path": relative, "reason": "包含本机配置、状态或外部链接"})
                continue
            if path.suffix.lower() in (".html", ".md", ".json", ".js", ".txt", ".xml"):
                try:
                    if path.stat().st_size > 5 * 1024 * 1024:
                        findings.append({"path": relative, "reason": "大文件需人工确认内容"})
                        continue
                    text = path.read_text(encoding="utf-8", errors="replace")
                    if any(value in text for value in values) or re.search(r"(?i)[?&](?:access_token|token|jwt|api_key|signature)=", text):
                        findings.append({"path": relative, "reason": "可能含凭据或带访问签名的链接"})
                except OSError:
                    findings.append({"path": relative, "reason": "无法读取，需要人工检查"})
    site_ready = bool(site and (site / "index.html").is_file() and (site / "index.html").stat().st_size)
    repository_ok = bool(re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository))
    checks = [{"label": "阅读网页已生成", "ok": site_ready}, {"label": "GitHub 目标仓库已填写", "ok": repository_ok},
              {"label": "gh 工具已安装", "ok": bool(gh)}, {"label": "GitHub 已登录", "ok": authenticated},
              {"label": "未发现明显私密文件", "ok": not findings}]
    return {"ready": all(item["ok"] for item in checks), "checks": checks, "findings": findings[:100],
            "repository": repository, "site_directory": str(site) if site else "", "scanned_files": scanned,
            "notice": "这是发布准备检查；材料公开授权与上线结果仍需确认。"}
