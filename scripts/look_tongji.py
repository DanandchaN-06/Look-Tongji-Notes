#!/usr/bin/env python3
"""CLI for Tongji Look (look.tongji.edu.cn): setup, list, transcribe.

This script is designed to be used by an agent skill:
- Credentials are stored in the skill root `.env` (ignored by `.gitignore`).
- Auth cache is stored in `<skill>/state/`.
- Lecture artifacts are written to the configured course-wiki workspace by default.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import getpass
import hashlib
import json
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse
from tongji_backend.progress import emit as emit_progress
from tongji_backend.checkpoints import path_for as checkpoint_path

from tongji_backend.workspace import (
    MaterialInput,
    WorkspaceConfig,
    index_workspace_wiki,
    build_workspace_wiki,
    ensure_workspace_config,
    import_materials,
    load_workspace_config,
    prepare_lecture_workspace,
    sanitize_path_component,
    serve_workspace_wiki,
    write_manifest,
)

if False:  # pragma: no cover - typing only without import-time dependency
    from tongji_backend.client import TongjiClient


def _skill_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _env_path() -> Path:
    return _skill_root() / ".env"


def _state_dir() -> Path:
    return _skill_root() / "state"


def _auth_session_file() -> Path:
    return _state_dir() / "auth_session.json"


def _last_course_file() -> Path:
    return _state_dir() / "last_course.json"


def _output_dir(output_dir: str | None) -> Path:
    return (Path(output_dir).expanduser().resolve() if output_dir else (Path.cwd() / "tongji-output"))


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


def _normalize_agent_name(value: str) -> str:
    raw = re.sub(r"[\s_-]+", "", str(value or "").strip().lower())
    if not raw:
        return ""
    mapping = {
        "codex": "codex",
        "codexcli": "codex",
        "claude": "claude",
        "claudecode": "claude",
        "claudecodecli": "claude",
        "copilot": "copilot",
        "githubcopilot": "copilot",
        "copilotchat": "copilot",
        "copilotcli": "copilot",
        "gemini": "gemini-cli",
        "geminicli": "gemini-cli",
        "openclaw": "openclaw",
        "opencode": "opencode",
        "hermes": "hermes-agent",
        "hermesagent": "hermes-agent",
    }
    return mapping.get(raw, raw)


def _detect_current_agent() -> str:
    explicit = _normalize_agent_name(os.environ.get("LOOK_TONGJI_AGENT", ""))
    if explicit:
        return explicit
    env_map = (
        ("CLAUDE_CODE", "claude"),
        ("CLAUDECODE", "claude"),
        ("CODEX_CLI", "codex"),
        ("CURSOR_AGENT", "cursor"),
        ("COPILOT_HOME", "copilot"),
        ("GEMINI_CLI", "gemini-cli"),
        ("OPENCLAW", "openclaw"),
        ("OPENCODE", "opencode"),
        ("HERMES_AGENT", "hermes-agent"),
    )
    for env_var, agent in env_map:
        if os.environ.get(env_var):
            return agent
    if os.environ.get("CODEX_THREAD_ID") or os.environ.get("CODEX_MANAGED_BY_NPM"):
        return "codex"
    return "codex"


def _merge_agent_sequence(existing: Any, current_agent: str) -> list[str]:
    raw_items: list[str] = []
    if isinstance(existing, list):
        raw_items.extend(str(item or "") for item in existing)
    elif isinstance(existing, str) and existing.strip():
        raw_items.extend(part.strip() for part in existing.split(","))
    if current_agent:
        raw_items.append(current_agent)
    seen: set[str] = set()
    merged: list[str] = []
    for item in raw_items:
        agent = _normalize_agent_name(item)
        if agent and agent not in seen:
            seen.add(agent)
            merged.append(agent)
    return merged


def _merge_material_entries(
    existing: Any,
    incoming: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    for source in (existing if isinstance(existing, list) else [], incoming or []):
        if not isinstance(source, dict):
            continue
        name = sanitize_path_component(str(source.get("name") or ""), fallback="")
        dedupe_key = name or str(source.get("copied_path") or source.get("source_path") or "")
        if not dedupe_key or dedupe_key in seen_names:
            continue
        seen_names.add(dedupe_key)
        merged.append(source)
    return merged


def _print_err(msg: str) -> None:
    print(f"[ERROR] {msg}", file=sys.stderr)


def _format_hms(total_seconds: int) -> str:
    sec = max(0, int(total_seconds))
    h = sec // 3600
    m = (sec % 3600) // 60
    s = sec % 60
    return f"{h:02d}-{m:02d}-{s:02d}"


def _safe_filename_part(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return cleaned.strip("._-") or "item"


def _guess_ext_from_url(url: str) -> str:
    path = urlparse(url).path or ""
    suffix = Path(path).suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".webp", ".bmp"}:
        return suffix
    return ".jpg"


def _trash_dir() -> Path:
    return _skill_root() / ".trash"


def _move_to_trash(path: Path) -> None:
    if not path.exists():
        return
    _trash_dir().mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d-%H%M%S")
    target = _trash_dir() / f"{path.name}.{ts}"
    try:
        path.replace(target)
    except Exception:
        try:
            shutil.move(str(path), str(target))
        except Exception:
            pass


def _parse_env_lines(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        if value.startswith('"') and value.endswith('"') and len(value) >= 2:
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        result[key] = value
    return result


def _quote_env_value(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f"\"{escaped}\""


def _write_env_file(path: Path, pairs: dict[str, str]) -> None:
    lines = ["# Auto-generated by look-tongji-notes", f"# Updated: {_now_iso()}"]
    for key, value in pairs.items():
        lines.append(f"{key}={_quote_env_value(value)}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class AuthSession:
    username: str
    jwt_token: str


def _save_auth_session(session: AuthSession) -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    _auth_session_file().write_text(
        json.dumps(
            {"username": session.username, "jwt_token": session.jwt_token, "updated_at": _now_iso()},
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )


def _load_auth_session() -> AuthSession | None:
    path = _auth_session_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        username = (data.get("username") or "").strip()
        jwt_token = (data.get("jwt_token") or "").strip()
        if not jwt_token:
            return None
        return AuthSession(username=username, jwt_token=jwt_token)
    except Exception:
        return None


def _clear_auth_session() -> None:
    _move_to_trash(_auth_session_file())


def _build_client_from_jwt(jwt_token: str) -> TongjiClient | None:
    from tongji_backend.auth import TongjiAuth
    from tongji_backend.client import TongjiClient

    auth = TongjiAuth()
    auth.jwt_token = jwt_token
    auth._setup_bearer_auth()
    auth.logged_in = True
    if not auth.check_alive():
        return None
    return TongjiClient(auth)


def _ensure_authenticated_client(force_login: bool = False) -> tuple[TongjiClient, str]:
    from tongji_backend.auth import TongjiAuth
    from tongji_backend.client import TongjiClient

    # 1) Try cached JWT first
    if not force_login:
        cached = _load_auth_session()
        if cached:
            client = _build_client_from_jwt(cached.jwt_token)
            if client is not None:
                return client, cached.username

    # 2) Login using env vars (loaded by tongji_backend.config)
    try:
        from tongji_backend import config
    except Exception as e:
        raise RuntimeError(f"Failed to import config: {e}") from e

    username = (config.TONGJI_USERNAME or "").strip()
    password = (config.TONGJI_PASSWORD or "").strip()
    if not username or not password:
        raise RuntimeError(
            "Missing credentials. Run `setup` to create .env, "
            "or set TONGJI_USERNAME/TONGJI_PASSWORD in the environment."
        )

    auth = TongjiAuth()
    auth.login(username=username, password=password)
    jwt_token = auth.get_jwt_token() or ""
    if jwt_token:
        _save_auth_session(AuthSession(username=username, jwt_token=jwt_token))
    return TongjiClient(auth), username


def _check_deps() -> list[str]:
    missing: list[str] = []
    from tongji_backend.media_tools import resolve_ffmpeg, MediaToolError
    try:
        resolve_ffmpeg()
    except MediaToolError:
        missing.append("ffmpeg")
    for module in ("requests", "dotenv", "playwright.sync_api"):
        try:
            __import__(module)
        except Exception:
            missing.append(module)
    return missing


def cmd_setup(args: argparse.Namespace) -> int:
    missing = _check_deps()
    if missing:
        print("[Setup] Missing dependencies:")
        for item in missing:
            print(f"  - {item}")
        print("\n[Setup] Install Python deps with:")
        print(f"  pip install -r \"{_skill_root() / 'requirements.txt'}\"")
        print("[Setup] Install Playwright browser with:")
        print("  python -m playwright install chromium")
        print()

    # Node.js detection (needed for vision-support)
    node_path = shutil.which("node")
    if node_path:
        try:
            import subprocess
            result = subprocess.run([node_path, "--version"], capture_output=True, text=True, timeout=5)
            version_str = result.stdout.strip().lstrip("v")
            major = int(version_str.split(".")[0]) if version_str else 0
            print(f"[Setup] Node.js: detected ({node_path}), version {version_str}")
            if major < 18:
                print(f"[Setup] WARNING: Node.js version {major} is below 18. vision-support may not work correctly.")
                print(f"  Install Node.js >= 18 from https://nodejs.org/")
        except Exception:
            print(f"[Setup] Node.js: detected ({node_path}), version unknown")
    else:
        print("[Setup] Node.js: NOT found. vision-support requires Node.js. Install from https://nodejs.org/")

    # vision-support config detection (embedded + external)
    vs_embedded_config = _skill_root() / "vision-support" / "config.json"
    vs_paths = [
        vs_embedded_config,
        Path.home() / ".claude" / "skills" / "vision-support" / "config.json",
        Path.home() / ".agents" / "skills" / "vision-support" / "config.json",
    ]
    vs_configured = any(p.exists() for p in vs_paths)
    if vs_configured:
        print("[Setup] vision-support: 已配置")
        if vs_embedded_config.exists():
            print("  测试: node \"<SKILL_DIR>/vision-support/scripts/vision.mjs\" \"<SKILL_DIR>/../../komari.jpg\"")
    else:
        print("[Setup] vision-support: 未配置。运行以下命令初始化：")
        print(f"  cd \"{_skill_root() / 'vision-support'}\" && node scripts/vision.mjs init")

    # TeX detection (optional, for cheatsheet compilation)
    if shutil.which("xelatex"):
        print("[Setup] xelatex: detected")
    else:
        print("[Setup] xelatex: NOT found (optional). Install TeX Live for cheatsheet compilation:")
        print("  - Ubuntu/Debian: sudo apt install texlive-xetex")
        print("  - macOS: brew install mactex")
        print("  - Windows: Install MiKTeX from https://miktex.org/")

    # gh CLI detection (for /page deployment)
    if shutil.which("gh"):
        print("[Setup] gh CLI: detected")
    else:
        print("[Setup] gh CLI: NOT found. /page deployment requires gh CLI. Install from:")
        print("  - https://cli.github.com/")
        print("  - Or: sudo apt install gh / brew install gh")
    print()

    env_file = _env_path()
    if env_file.exists() and not args.overwrite:
        _print_err(f".env already exists at {env_file}. Re-run with --overwrite to replace it.")
        return 2

    username = (args.username or "").strip()
    password = (args.password or "").strip()

    if not username:
        username = input("Tongji username (student/staff ID): ").strip()
    if not password:
        password = getpass.getpass("Tongji password (input hidden): ").strip()

    if not username or not password:
        _print_err("Username/password cannot be empty.")
        return 2

    existing: dict[str, str] = {}
    if env_file.exists():
        try:
            existing = _parse_env_lines(env_file.read_text(encoding="utf-8"))
        except Exception:
            existing = {}

    existing["TONGJI_USERNAME"] = username
    existing["TONGJI_PASSWORD"] = password
    _write_env_file(env_file, existing)
    _clear_auth_session()
    try:
        workspace_config = ensure_workspace_config(
            workspace_root=args.workspace_root or None,
            owner_name=args.owner_name or None,
            site_name=args.site_name or None,
            interactive=True,
        )
        workspace_config.workspace_root.mkdir(parents=True, exist_ok=True)
        print(f"[Setup] Saved workspace config to: {workspace_config.config_path}")
        print(f"[Setup] Workspace root: {workspace_config.workspace_root}")
        print(f"[Setup] Site name: {workspace_config.site_name}")
    except Exception as e:
        _print_err(f"Workspace setup failed: {e}")
        return 1
    print(f"[Setup] Saved credentials to: {env_file}")
    print("[Setup] Done.")
    return 0


def _save_last_course(course: dict[str, Any]) -> None:
    _state_dir().mkdir(parents=True, exist_ok=True)
    _last_course_file().write_text(
        json.dumps({"course": course, "updated_at": _now_iso()}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def _load_last_course_id() -> str | None:
    path = _last_course_file()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        course = data.get("course") or {}
        course_id = (course.get("course_id") or "").strip()
        return course_id or None
    except Exception:
        return None


def cmd_list(args: argparse.Namespace) -> int:
    try:
        client, username = _ensure_authenticated_client(force_login=args.force_login)
    except Exception as e:
        _print_err(str(e))
        return 1

    query = (args.query or "").strip().lower()

    if args.all_courses:
        courses = client.get_all_courses()
    else:
        courses = client.get_recent_courses(per_page=max(1, int(args.limit)))

    if not courses:
        _print_err("No courses found.")
        return 1

    print(f"[List] Logged in as: {username or '(unknown)'}")

    def _course_title(course: dict[str, Any]) -> str:
        return str(course.get("title") or course.get("course_title") or "").strip()

    def _course_teacher(course: dict[str, Any]) -> str:
        return str(course.get("teacher") or course.get("realname") or "").strip()

    if query:
        courses = [
            c for c in courses
            if query in _course_title(c).lower() or query in _course_teacher(c).lower()
        ]

    if not courses:
        _print_err("No courses matched the query.")
        return 1

    header = "All courses" if args.all_courses else "Recent courses"
    if query:
        header += f" (query: {args.query})"

    limit = int(args.limit)
    shown = courses if limit <= 0 else courses[:limit]

    if getattr(args, "json_output", False):
        payload = {
            "ok": True,
            "username": username or "",
            "source": "all" if args.all_courses else "recent",
            "query": args.query or "",
            "courses": [
                {
                    "index": idx,
                    "course_id": str(c.get("course_id") or "").strip(),
                    "title": _course_title(c),
                    "teacher": _course_teacher(c),
                }
                for idx, c in enumerate(shown, 1)
            ],
        }
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print(f"[List] {header}:")
    for idx, c in enumerate(shown, 1):
        title = _course_title(c)
        teacher = _course_teacher(c)
        cid = str(c.get("course_id") or "").strip()
        label = f"{title} ({cid})"
        if teacher:
            label += f" / {teacher}"
        print(f"  {idx}. {label}")

    if getattr(args, "no_prompt", False):
        return 0

    choose = args.choose
    if choose is None:
        raw = input("\nChoose a course number (or press Enter to skip): ").strip()
        if not raw:
            return 0
        try:
            choose = int(raw)
        except ValueError:
            _print_err(f"Invalid selection: {raw}")
            return 2

    if choose < 1 or choose > len(shown):
        _print_err(f"Invalid selection: {choose}")
        return 2

    selected = shown[choose - 1]
    _save_last_course(selected)
    print(f"[List] Selected: {_course_title(selected)} ({selected.get('course_id', '')})")
    return 0


def cmd_lectures(args: argparse.Namespace) -> int:
    """List the lectures (sections) of one course. JSON-first, never prompts."""
    try:
        client, _username = _ensure_authenticated_client(force_login=args.force_login)
    except Exception as e:
        _print_err(str(e))
        return 1

    course_id = (args.course_id or "").strip()
    if not course_id and args.lecture_url:
        cid, _sub = _extract_ids_from_url(args.lecture_url)
        course_id = (cid or "").strip()
    if not course_id:
        _print_err("Missing --course-id (or a lecture URL that contains course_id)")
        return 2

    try:
        detail = client.get_course_detail(course_id)
    except Exception as e:
        _print_err(f"Failed to get course detail: {e}")
        return 1

    items: list[dict[str, Any]] = []
    for lec in detail.get("lectures") or []:
        if not isinstance(lec, dict):
            continue
        items.append({
            "sub_id": str(lec.get("sub_id") or "").strip(),
            "sub_title": str(lec.get("sub_title") or "").strip(),
            "lecturer_name": str(lec.get("lecturer_name") or "").strip(),
            "date": str(lec.get("date") or "").strip(),
            "has_playback": lec.get("has_playback") is True,
        })
    items.sort(key=lambda x: (x["date"], x["sub_id"]), reverse=True)

    if getattr(args, "json_output", False):
        print(json.dumps({
            "ok": True,
            "course_id": course_id,
            "title": str(detail.get("title") or "").strip(),
            "teacher": str(detail.get("teacher") or "").strip(),
            "lecture_count": len(items),
            "lectures": items,
        }, ensure_ascii=False))
        return 0

    print(f"[Lectures] {str(detail.get('title') or '').strip()} ({course_id})")
    for idx, it in enumerate(items, 1):
        suffix = "" if it["has_playback"] else "  [no playback]"
        print(f"  {idx}. {it['date']}  {it['sub_title']} ({it['sub_id']}){suffix}")
    return 0


def _cheatsheet_template_dir() -> Path | None:
    root = _skill_root()
    candidates = [
        root / "CheatingSheetTemplate",
        root / ".mock-wiki" / "CheatingSheetTemplate",
    ]
    return next((c for c in candidates if (c / "CheatingSheet.tex").exists()), None)


def _detect_vision_config(root: Path) -> Path | None:
    candidates = [
        root / "vision-support" / "config.json",
        Path.home() / ".claude" / "skills" / "vision-support" / "config.json",
        Path.home() / ".agents" / "skills" / "vision-support" / "config.json",
    ]
    return next((p for p in candidates if p.exists()), None)


def cmd_status(args: argparse.Namespace) -> int:
    """Report environment + configuration state. Never prints secrets."""
    root = _skill_root()
    env_file = _env_path()

    env_values: dict[str, str] = {}
    if env_file.exists():
        try:
            env_values = _parse_env_lines(env_file.read_text(encoding="utf-8"))
        except Exception:
            env_values = {}

    workspace = load_workspace_config()
    vision_cfg = _detect_vision_config(root)
    template_dir = _cheatsheet_template_dir()

    tools: dict[str, str] = {}
    for name in ("ffmpeg", "node", "npm", "xelatex", "gh", "git"):
        tools[name] = shutil.which(name) or ""
    from tongji_backend.media_tools import resolve_ffmpeg, MediaToolError
    ffmpeg_error = ""
    try:
        tools['ffmpeg'] = resolve_ffmpeg()
    except MediaToolError as exc:
        tools['ffmpeg'] = ""
        ffmpeg_error = str(exc)

    workspace_info: dict[str, Any] | None = None
    if workspace is not None:
        ws_root = workspace.workspace_root
        manifests: list[str] = []
        if ws_root.exists():
            try:
                manifests = [str(p) for p in sorted(ws_root.glob("raw/*/*/原始数据/manifest.json"))]
            except Exception:
                manifests = []
        workspace_info = {
            "workspace_root": str(ws_root),
            "owner_name": workspace.owner_name,
            "site_name": workspace.site_name,
            "exists": ws_root.exists(),
            "site_built": (ws_root / "site" / "index.html").exists(),
            "lecture_count": len(manifests),
            "manifests": manifests[:500],
            "config_path": str(workspace.config_path),
        }

    missing = _check_deps()
    payload = {
        "ok": True,
        "repo_root": str(root),
        "env_path": str(env_file),
        "env_exists": env_file.exists(),
        "credentials": {
            "configured": bool(env_values.get("TONGJI_USERNAME") and env_values.get("TONGJI_PASSWORD")),
            "username_set": bool(env_values.get("TONGJI_USERNAME")),
            "password_set": bool(env_values.get("TONGJI_PASSWORD")),
        },
        "workspace": workspace_info,
        "last_course_id": _load_last_course_id() or "",
        "vision_support": {
            "configured": vision_cfg is not None,
            "config_path": str(vision_cfg) if vision_cfg else "",
        },
        "tools": tools,
        "ffmpeg_error": ffmpeg_error,
        "missing_deps": missing,
        "deps_ok": not missing,
        "cheatsheet_template": str(template_dir) if template_dir else "",
    }

    if getattr(args, "json_output", False):
        print(json.dumps(payload, ensure_ascii=False))
        return 0

    print(f"[Status] repo root      : {payload['repo_root']}")
    print(f"[Status] .env           : {payload['env_path']} (exists={payload['env_exists']})")
    print(f"[Status] credentials    : {'configured' if payload['credentials']['configured'] else 'NOT configured'}")
    print(f"[Status] workspace      : {(workspace_info or {}).get('workspace_root', 'NOT configured')}")
    if workspace_info:
        print(f"[Status] site built     : {workspace_info['site_built']}")
        print(f"[Status] lectures       : {workspace_info['lecture_count']}")
    print(f"[Status] vision-support : {'configured' if vision_cfg else 'NOT configured'}")
    print(f"[Status] missing deps   : {missing if missing else 'none'}")
    for name, path in tools.items():
        print(f"[Status] tool {name:<8}: {path or 'NOT found'}")
    return 0


def _extract_ids_from_url(url: str) -> tuple[str | None, str | None]:
    parsed = urlparse(url)

    query_parts: list[str] = []
    if parsed.query:
        query_parts.append(parsed.query)
    if parsed.fragment and "?" in parsed.fragment:
        query_parts.append(parsed.fragment.split("?", 1)[1])

    params: dict[str, str] = {}
    for part in query_parts:
        for k, v in parse_qs(part).items():
            if not v:
                continue
            params[k.lower()] = v[0]

    course_id = (
        params.get("course_id")
        or params.get("courseid")
        or params.get("cid")
        or params.get("course")
    )
    sub_id = (
        params.get("sub_id")
        or params.get("subid")
        or params.get("sid")
        or params.get("sub")
    )

    # Best-effort: try to find `sub_id` in fragment path (e.g. "#/play/12345")
    if not sub_id and parsed.fragment:
        m = re.search(r"/play/(\d+)", parsed.fragment)
        if m:
            sub_id = m.group(1)

    return course_id, sub_id


def _choose_lecture_from_course(client: TongjiClient, course_id: str, limit: int) -> tuple[str, dict[str, Any] | None]:
    detail = client.get_course_detail(course_id)
    lectures = detail.get("lectures") or []
    if not isinstance(lectures, list) or not lectures:
        raise RuntimeError("No lectures found for this course.")

    # Prefer playable lectures; keep fallback to show something
    playable = [l for l in lectures if l.get("has_playback") is True]
    candidates = playable or lectures

    def _sort_key(item: dict[str, Any]) -> str:
        return str(item.get("date") or "")

    candidates = sorted(candidates, key=_sort_key, reverse=True)[:limit]

    print("[Select] Lectures (latest first):")
    for idx, lec in enumerate(candidates, 1):
        sub_id = str(lec.get("sub_id") or "")
        title = str(lec.get("sub_title") or "").strip()
        date = str(lec.get("date") or "").strip()
        flag = "playback" if lec.get("has_playback") else "no-playback"
        display = " ".join([p for p in [date, title] if p]).strip()
        print(f"  {idx}. {display} ({sub_id}) [{flag}]")

    raw = input("\nChoose a lecture number: ").strip()
    if not raw:
        raise RuntimeError("No lecture selected.")
    choose = int(raw)
    if choose < 1 or choose > len(candidates):
        raise RuntimeError(f"Invalid lecture selection: {choose}")

    selected = candidates[choose - 1]
    return str(selected.get("sub_id") or ""), selected


def _find_lecture_info(detail: dict[str, Any], sub_id: str) -> dict[str, Any]:
    lectures = detail.get("lectures") or []
    for item in lectures:
        if str(item.get("sub_id") or "") == str(sub_id):
            return dict(item)
    return {"sub_id": sub_id, "sub_title": f"节次-{sub_id}", "date": ""}


def _prepare_workspace_for_lecture(
    client: TongjiClient,
    args: argparse.Namespace,
    *,
    course_id: str,
    sub_id: str,
) -> tuple[WorkspaceConfig, Any]:
    config = ensure_workspace_config(
        workspace_root=getattr(args, "workspace_root", "") or None,
        owner_name=getattr(args, "owner_name", "") or None,
        site_name=getattr(args, "site_name", "") or None,
        interactive=not getattr(args, "no_workspace_prompt", False),
    )
    detail = client.get_course_detail(course_id)
    lecture = _find_lecture_info(detail, sub_id)
    return config, prepare_lecture_workspace(config, course_detail=detail, lecture=lecture)


def _parse_material_arg(raw: str) -> MaterialInput | None:
    text = (raw or "").strip()
    if not text:
        return None
    if "=" in text:
        name, path = text.split("=", 1)
        return MaterialInput(name=sanitize_path_component(name, fallback=Path(path).stem), path=Path(path))
    path = Path(text)
    return MaterialInput(name=sanitize_path_component(path.stem, fallback="资料"), path=path)


def _collect_materials(args: argparse.Namespace) -> list[MaterialInput]:
    materials: list[MaterialInput] = []
    for raw in getattr(args, "material", []) or []:
        parsed = _parse_material_arg(raw)
        if parsed:
            materials.append(parsed)

    if getattr(args, "no_material_prompt", False) or not sys.stdin.isatty():
        return materials

    if materials:
        return materials

    raw = input("[Materials] 是否有补充资料需要加入来源索引？(y/N): ").strip().lower()
    if raw not in {"y", "yes", "1", "true", "是", "确认"}:
        return materials

    print("[Materials] 逐行输入：资料名=文件路径。直接回车结束。")
    while True:
        line = input("material> ").strip()
        if not line:
            break
        parsed = _parse_material_arg(line)
        if parsed:
            materials.append(parsed)
    return materials


def _write_lecture_manifest_from_outputs(
    *,
    workspace: Any,
    course_id: str,
    sub_id: str,
    lecture_url: str,
    transcript_output_dir: Path,
    slide_output_dir: Path,
    imported_materials: list[dict[str, Any]] | None = None,
    note_style: str = "standard",
    slides_regenerated: bool = False,
) -> None:
    base = f"{course_id}_{sub_id}"
    current_agent = _detect_current_agent()
    existing_manifest: dict[str, Any] = {}
    if getattr(workspace, "manifest_path", None) and workspace.manifest_path.exists():
        try:
            data = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                existing_manifest = data
        except Exception:
            existing_manifest = {}
    meta_path = transcript_output_dir / f"{base}.json"
    meta = {}
    if meta_path.exists():
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except Exception:
            meta = {}

    artifacts = dict(existing_manifest["artifacts"]) if isinstance(existing_manifest.get("artifacts"), dict) else {}
    artifacts.update({
        "transcript_txt": str(transcript_output_dir / f"{base}.txt"),
        "subtitle_srt": str(transcript_output_dir / f"{base}.srt"),
        "transcript_meta": str(meta_path),
    })
    artifacts.setdefault("slides_dir", str(slide_output_dir))
    artifacts.setdefault("slide_index", str(slide_output_dir / "index.json"))
    if slides_regenerated:
        artifacts.update(slides_dir=str(slide_output_dir), slide_index=str(slide_output_dir / "index.json"))
    artifacts.setdefault("timeline_txt", str(transcript_output_dir / f"{base}_timeline.txt"))
    merged_agents = _merge_agent_sequence(existing_manifest.get("agents") or existing_manifest.get("agent"), current_agent)
    merged_materials = _merge_material_entries(existing_manifest.get("materials"), imported_materials)
    manifest = {**existing_manifest,
        "course_id": course_id,
        "sub_id": sub_id,
        "course_title": workspace.course_title,
        "session_title": workspace.session_title,
        "agent": current_agent,
        "agents": merged_agents or [current_agent],
        "lecture_url": lecture_url or existing_manifest.get("lecture_url", ""),
        "video_url": meta.get("video_url", existing_manifest.get("video_url", "")),
        "base_name": base,
        "generated_at": _now_iso(),
        "duration_seconds": int(meta.get("duration_seconds") or existing_manifest.get("duration_seconds") or 0),
        "subtitle_word_count": int(meta.get("word_count") or meta.get("subtitle_word_count") or existing_manifest.get("subtitle_word_count") or 0),
        "note_style": note_style or existing_manifest.get("note_style") or "standard",
        "artifacts": artifacts,
        "materials": merged_materials,
    }
    dur = manifest["duration_seconds"]
    manifest.pop("duration_warning", None)
    if 0 < dur < 3600:
        manifest["duration_warning"] = True
    write_manifest(workspace, manifest)


def _resolve_course_sub(
    client: TongjiClient,
    *,
    lecture_url: str,
    course_id: str,
    sub_id: str,
    lecture_limit: int,
    tag: str,
) -> tuple[str, str] | None:
    course_id = (course_id or "").strip()
    sub_id = (sub_id or "").strip()

    if lecture_url:
        parsed_course_id, parsed_sub_id = _extract_ids_from_url(lecture_url)
        course_id = course_id or (parsed_course_id or "")
        sub_id = sub_id or (parsed_sub_id or "")

    if not course_id:
        last = _load_last_course_id()
        if last:
            print(f"[{tag}] Using last selected course_id from state: {last}")
            course_id = last

    if not course_id:
        _print_err("Missing course_id. Provide --course-id or --lecture-url that contains it.")
        return None

    if not sub_id:
        try:
            sub_id, _ = _choose_lecture_from_course(client, course_id, limit=lecture_limit)
        except Exception as e:
            _print_err(str(e))
            return None
    return course_id, sub_id


def _saved_transcript_ready(out_dir: Path, course_id: str, sub_id: str, username: str) -> bool:
    base = f"{course_id}_{sub_id}"
    try:
        meta = json.loads((out_dir / f"{base}.json").read_text(encoding="utf-8"))
        if not isinstance(meta, dict) or str(meta.get("course_id")) != course_id or str(meta.get("sub_id")) != sub_id:
            return False
        if not username or meta.get("user") != username:
            return False
        text = out_dir / f"{base}.txt"
        if not text.is_file() or text.stat().st_size == 0:
            return False
        artifacts = meta.get("artifacts") or {}
        if not isinstance(artifacts, dict):
            return False
        subtitle = out_dir / f"{base}.srt"
        if artifacts.get("subtitle_srt") and (not subtitle.is_file() or subtitle.stat().st_size == 0):
            return False
        hashes = meta.get("artifact_sha256")
        if meta.get("complete") is True:
            if not isinstance(hashes, dict) or hashes.get("transcript_txt") != hashlib.sha256(text.read_bytes()).hexdigest():
                return False
            return not artifacts.get("subtitle_srt") or hashes.get("subtitle_srt") == hashlib.sha256(subtitle.read_bytes()).hexdigest()
        # Older upstream results have no commit hashes. Their ASR segments
        # still let us validate the complete text and timestamps exactly.
        utterances = meta.get("utterances")
        if not isinstance(utterances,list) or not utterances or any(not isinstance(u,dict) or not isinstance(u.get("transcript"),str) for u in utterances):
            return False
        if text.read_text(encoding="utf-8").strip() != " ".join(u["transcript"] for u in utterances).strip():
            return False
        if artifacts.get("subtitle_srt"):
            from tongji_backend.transcriber import _format_srt_time
            expected = "\n".join(f"{i}\n{_format_srt_time(u.get('start_time',0))} --> {_format_srt_time(u.get('end_time',0))}\n{u['transcript'].strip()}\n" for i,u in enumerate(utterances,1) if u['transcript'].strip())
            return subtitle.read_text(encoding="utf-8").strip() == expected.strip()
        return True
    except (OSError, ValueError, TypeError):
        return False


def _transcript_audio_cache(username: str, course_id: str, sub_id: str) -> Path:
    identity = json.dumps([username,course_id,sub_id],ensure_ascii=False)
    cache_dir = _state_dir()/"media"/hashlib.sha256(identity.encode("utf-8")).hexdigest()
    if not cache_dir.resolve().is_relative_to(_state_dir().resolve()):
        raise ValueError("音频缓存路径必须位于本机状态目录内")
    for name in ('audio.mp3','audio.part.mp3','video_raw.tmp','video_raw.tmp.part','job.lock'):
        if not (cache_dir/name).resolve().is_relative_to(cache_dir.resolve()):
            raise ValueError("音频缓存包含外部文件链接")
    return cache_dir/"audio.mp3"


def _run_transcript_job(*, client: TongjiClient, username: str, course_id: str, sub_id: str,
                        lecture_url: str, output_dir: str, tag: str = "Transcript", force_transcribe: bool = False) -> int:
    from tongji_backend.media_cache import lecture_lock
    if not re.fullmatch(r"[A-Za-z0-9_-]+",course_id) or not re.fullmatch(r"[A-Za-z0-9_-]+",sub_id):
        raise ValueError("课程和课次标识格式无效")
    cache = _transcript_audio_cache(username,course_id,sub_id)
    output_identity = json.dumps([str(_output_dir(output_dir).resolve()),course_id,sub_id],ensure_ascii=False)
    results_dir = _state_dir()/"transcript_locks"/hashlib.sha256(output_identity.encode("utf-8")).hexdigest()
    if not results_dir.resolve().is_relative_to(_state_dir().resolve()):
        raise ValueError("字幕写入锁必须位于本机状态目录内")
    # Output identity is independent of account; media stays account-scoped.
    with lecture_lock(results_dir/"job.lock"), lecture_lock(cache.parent/"job.lock"):
        return _run_transcript_job_locked(client=client,username=username,course_id=course_id,sub_id=sub_id,
            lecture_url=lecture_url,output_dir=output_dir,tag=tag,force_transcribe=force_transcribe)


def _run_transcript_job_locked(
    *,
    client: TongjiClient,
    username: str,
    course_id: str,
    sub_id: str,
    lecture_url: str,
    output_dir: str,
    tag: str = "Transcript",
    force_transcribe: bool = False,
) -> int:
    from tongji_backend.transcriber import NoAudioStreamError, Transcriber, TranscriptionError
    from tongji_backend.media_tools import MediaToolError
    emit_progress("transcript", "prepare")
    out_dir = _output_dir(output_dir)
    if not force_transcribe and _saved_transcript_ready(out_dir, course_id, sub_id, username):
        print(f"[{tag}] Reusing existing TXT/SRT; skipping video download and ASR.")
        emit_progress("transcript", "reuse_transcript")
        return 0

    print(f"[{tag}] Logged in as: {username or '(unknown)'}")
    print(f"[{tag}] course_id={course_id} sub_id={sub_id}")
    video_url = client.get_video_url(course_id, sub_id)
    if not video_url:
        _print_err("Failed to resolve video URL. The lecture may not have playback enabled.")
        return 1

    stream_url, http_headers = client.get_stream_params(video_url)
    transcriber = Transcriber()

    try:
        cache = _transcript_audio_cache(username,course_id,sub_id)
        cache_dir = cache.parent
        if force_transcribe:
            for path in (cache, cache_dir / "video_raw.tmp"):
                if not path.resolve().is_relative_to(cache_dir.resolve()):
                    raise ValueError("音频缓存包含外部文件链接")
                path.unlink(missing_ok=True)
        transcript, srt_content, utterances = transcriber.transcribe_url(
            stream_url, http_headers=http_headers, audio_cache_path=cache
        )
    except NoAudioStreamError as e:
        _print_err(f"No audio stream: {e}")
        return 1
    except MediaToolError as e:
        _print_err(f"音频工具不可用，已停止转写：{e}")
        return 2
    except TranscriptionError as e:
        _print_err(f"Transcription failed: {e}")
        return 1
    except Exception as e:
        _print_err(f"Unexpected error: {type(e).__name__}: {e}")
        return 1

    out_dir.mkdir(parents=True, exist_ok=True)

    base = f"{course_id}_{sub_id}"
    txt_path = out_dir / f"{base}.txt"
    srt_path = out_dir / f"{base}.srt"
    meta_path = out_dir / f"{base}.json"

    # Compute duration_seconds from utterance end_time (milliseconds)
    duration_seconds = 0
    if utterances:
        max_end = max(u.get("end_time", 0) for u in utterances)
        duration_seconds = max_end // 1000

    emit_progress("transcript", "write")
    # The final metadata is the commit marker: interruption cannot leave
    # mixed old/new subtitle files looking like a completed result.
    meta_path.unlink(missing_ok=True)
    txt_staging = txt_path.with_suffix(".txt.part")
    txt_staging.write_text(transcript.strip() + "\n", encoding="utf-8")
    os.replace(txt_staging,txt_path)
    if srt_content:
        srt_staging = srt_path.with_suffix(".srt.part")
        srt_staging.write_text(srt_content.strip() + "\n", encoding="utf-8")
        os.replace(srt_staging,srt_path)
    else:
        srt_path.unlink(missing_ok=True)
    meta_staging = meta_path.with_suffix(".json.part")
    meta_staging.write_text(
        json.dumps(
            {
                "course_id": course_id,
                "sub_id": sub_id,
                "lecture_url": lecture_url or "",
                "video_url": video_url,
                "generated_at": _now_iso(),
                "user": username or "",
                "complete": True,
                "artifact_sha256": {"transcript_txt": hashlib.sha256(txt_path.read_bytes()).hexdigest(),
                                    "subtitle_srt": hashlib.sha256(srt_path.read_bytes()).hexdigest() if srt_content else ""},
                "duration_seconds": duration_seconds,
                "artifacts": {
                    "transcript_txt": str(txt_path),
                    "subtitle_srt": str(srt_path) if srt_content else "",
                },
                "utterances": utterances or [],
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    os.replace(meta_staging,meta_path)

    print(f"[{tag}] Done. Files written:")
    emit_progress("transcript", "done")
    print(f"  - {txt_path}")
    if srt_content:
        print(f"  - {srt_path}")
    print(f"  - {meta_path}")
    return 0


def _download_one_slide(
    client: TongjiClient,
    item: dict[str, Any],
    out_dir: Path,
    index: int,
    timeout: int,
    retries: int,
) -> tuple[dict[str, Any], str | None]:
    created_sec = int(item.get("created_sec") or 0)
    image_url = str(item.get("image_url") or "").strip()
    if not image_url:
        return item, "missing image_url"

    stamp = _format_hms(created_sec)
    ext = _guess_ext_from_url(image_url)
    filename = f"{index:04d}_t{stamp}_s{created_sec:06d}{ext}"
    path = out_dir / _safe_filename_part(filename)

    last_err = ""
    for attempt in range(1, max(1, retries) + 1):
        try:
            resp = client.session.get(image_url, timeout=timeout)
            if resp.status_code == 200 and resp.content:
                path.write_bytes(resp.content)
                item["filename"] = path.name
                item["filepath"] = str(path)
                item["downloaded_at"] = _now_iso()
                item["bytes"] = len(resp.content)
                return item, None

            last_err = f"http {resp.status_code}"
            if resp.status_code in (429, 500, 502, 503, 504):
                time.sleep(0.5 * attempt)
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(0.5 * attempt)

    return item, last_err or "download failed"


def _run_slide_job(
    *,
    client: TongjiClient,
    username: str,
    course_id: str,
    sub_id: str,
    lecture_url: str,
    output_dir: str,
    per_page: int,
    max_pages: int,
    max_items: int,
    concurrency: int,
    retries: int,
    timeout: int,
    tag: str = "Slide",
) -> int:
    print(f"[{tag}] Logged in as: {username or '(unknown)'}")
    emit_progress("slides", "prepare")
    print(f"[{tag}] course_id={course_id} sub_id={sub_id}")
    try:
        snapshots = client.get_ppt_snapshots(
            course_id,
            sub_id,
            per_page=max(1, int(per_page)),
            max_pages=max(1, int(max_pages)),
        )
    except Exception as e:
        _print_err(f"Failed to list slide snapshots: {e}")
        return 1

    if not snapshots:
        _print_err("No slide snapshots found for this lecture.")
        return 1

    if max_items and max_items > 0:
        snapshots = snapshots[: max_items]

    out_dir = (
        Path(output_dir).expanduser().resolve()
        if output_dir
        else (Path.cwd() / "tongji-output" / f"slide_{course_id}_{sub_id}").resolve()
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    concurrency = max(1, min(int(concurrency), 16))
    retries = max(1, min(int(retries), 8))
    timeout = max(5, int(timeout))

    print(
        f"[{tag}] Found {len(snapshots)} snapshots. Downloading with "
        f"concurrency={concurrency}, retries={retries}, timeout={timeout}s ..."
    )

    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    emit_progress("slides", "download", completed=0, total=len(snapshots), unit="张")

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as executor:
        fut_map = {
            executor.submit(
                _download_one_slide,
                client,
                item,
                out_dir,
                idx,
                timeout,
                retries,
            ): item
            for idx, item in enumerate(snapshots, 1)
        }

        done_count = 0
        total = len(fut_map)
        for fut in concurrent.futures.as_completed(fut_map):
            item, err = fut.result()
            done_count += 1
            if err:
                failures.append({"item": item, "error": err})
            else:
                results.append(item)
            emit_progress("slides", "download", completed=done_count, total=total, failed=len(failures), unit="张")
            if done_count % 10 == 0 or done_count == total:
                print(f"[{tag}] Progress: {done_count}/{total}")

    results.sort(key=lambda x: int(x.get("created_sec") or 0))

    meta_path = out_dir / "index.json"
    meta = {
        "course_id": course_id,
        "sub_id": sub_id,
        "lecture_url": lecture_url or "",
        "generated_at": _now_iso(),
        "user": username or "",
        "download": {
            "requested": len(snapshots),
            "succeeded": len(results),
            "failed": len(failures),
            "concurrency": concurrency,
            "retries": retries,
            "timeout_seconds": timeout,
        },
        "items": results,
        "failures": failures,
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"[{tag}] Done.")
    print(f"  - output_dir: {out_dir}")
    print(f"  - success: {len(results)}")
    print(f"  - failed: {len(failures)}")
    print(f"  - index: {meta_path}")
    if failures:
        print(f"[{tag}] Some downloads failed. You can re-run with lower --concurrency (e.g. 2) or higher --retries.")
    return 0 if not failures else 3


def _atomic_write_json(path: Path, data: Any) -> None:
    """Write JSON atomically: write to tmp file then rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp.json")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _create_batch_state(course_id: str, lectures: list[dict[str, Any]]) -> dict[str, Any]:
    now = _now_iso()
    return {
        "course_id": course_id,
        "started_at": now,
        "updated_at": now,
        "lectures": [
            {
                "sub_id": str(l.get("sub_id", "")),
                "sub_title": str(l.get("sub_title", "")),
                "status": "pending",
                "attempts": 0,
                "error": None,
            }
            for l in lectures
        ],
    }


def cmd_batch_transcribe(args: argparse.Namespace) -> int:
    try:
        client, username = _ensure_authenticated_client(force_login=args.force_login)
    except Exception as e:
        _print_err(str(e))
        return 1

    course_id = (args.course_id or "").strip()
    if not course_id:
        _print_err("Missing --course-id")
        return 2

    print(f"[Batch] Fetching course detail for course_id={course_id} ...")
    try:
        detail = client.get_course_detail(course_id)
    except Exception as e:
        _print_err(f"Failed to get course detail: {e}")
        return 1

    lectures_raw = detail.get("lectures") or []
    lectures = [l for l in lectures_raw if l.get("has_playback") is True]
    if not lectures:
        _print_err("No playable lectures found for this course.")
        return 1

    print(f"[Batch] Found {len(lectures)} lecture(s) with playback enabled.")

    try:
        config = ensure_workspace_config(
            workspace_root=args.workspace_root or None,
            owner_name=args.owner_name or None,
            site_name=args.site_name or None,
            interactive=not args.no_workspace_prompt,
        )
    except Exception as e:
        _print_err(f"Workspace config failed: {e}")
        return 1

    batch_state_path = checkpoint_path(config.workspace_root, course_id)
    legacy_state_path = config.workspace_root / "batch_state.json"
    if not batch_state_path.exists() and legacy_state_path.is_file():
        try:
            legacy = json.loads(legacy_state_path.read_text(encoding="utf-8"))
            if isinstance(legacy, dict) and legacy.get("course_id") == course_id:
                _atomic_write_json(batch_state_path, legacy)
        except (OSError, ValueError):
            pass
    max_retries = max(1, int(args.max_retries))

    # Load or create batch state (interrupt-safe resume)
    if batch_state_path.exists():
        try:
            state = json.loads(batch_state_path.read_text(encoding="utf-8"))
            if state.get("course_id") != course_id:
                print(f"[Batch] Existing state is for course '{state.get('course_id')}', starting fresh.")
                state = _create_batch_state(course_id, lectures)
            else:
                # Reset any in_progress items back to pending
                for item in state.get("lectures", []):
                    if item.get("status") == "in_progress":
                        item["status"] = "pending"
                done = sum(1 for l in state.get("lectures", []) if l.get("status") == "done")
                print(f"[Batch] Resuming from existing batch_state.json ({done}/{len(lectures)} done)")
        except Exception:
            print("[Batch] Could not read batch_state.json, starting fresh.")
            state = _create_batch_state(course_id, lectures)
    else:
        state = _create_batch_state(course_id, lectures)
        _atomic_write_json(batch_state_path, state)
        print(f"[Batch] Created batch_state.json at {batch_state_path}")

    # Ensure the lectures list in state covers all current lectures
    state_sub_ids = {l["sub_id"] for l in state.get("lectures", [])}
    for lec in lectures:
        sid = str(lec.get("sub_id", ""))
        if sid not in state_sub_ids:
            state.setdefault("lectures", []).append({
                "sub_id": sid,
                "sub_title": str(lec.get("sub_title", "")),
                "status": "pending",
                "attempts": 0,
                "error": None,
            })

    def _save_state():
        state["updated_at"] = _now_iso()
        _atomic_write_json(batch_state_path, state)
        _atomic_write_json(legacy_state_path, state)
        done = sum(item.get("status") == "done" for item in state["lectures"])
        failed = sum(item.get("status") == "failed" for item in state["lectures"])
        emit_progress("batch", "recognize", completed=done + failed, total=len(state["lectures"]), failed=failed, unit="节")

    if getattr(args, "retry_failed", False):
        for item in state["lectures"]:
            if item.get("status") == "failed":
                item.update(status="pending", attempts=0, error=None)
    _save_state()

    cancel_file = os.environ.get("LOOK_TONGJI_BATCH_CANCEL_FILE", "").strip()
    cancel_path = Path(cancel_file) if cancel_file else None

    try:
        while True:
            if cancel_path is not None and cancel_path.exists():
                print("\n[Batch] Cancel requested. Saving current state...")
                for item in state.get("lectures", []):
                    if item.get("status") == "in_progress":
                        item["status"] = "pending"
                _save_state()
                print(f"[Batch] State saved to {batch_state_path}. Re-run to continue.")
                return 130

            # NOTE: only 'pending' is taken here. An item is set back to 'pending'
            # while it still has retries left (see below), so 'failed' means
            # retries are exhausted and must NOT be re-queued.
            pending = [l for l in state.get("lectures", []) if l["status"] == "pending"]
            if not pending:
                break

            item = pending[0]
            sub_id = item["sub_id"]
            sub_title = item["sub_title"] or sub_id
            done = sum(1 for l in state["lectures"] if l["status"] == "done")
            total = len(state["lectures"])

            print(f"[Batch] [{done + 1}/{total}] Processing: {sub_title} ({sub_id})")

            item["status"] = "in_progress"
            item["attempts"] = int(item.get("attempts", 0)) + 1
            _save_state()

            output_dir_str = args.output_dir
            workspace = None
            if not output_dir_str:
                try:
                    _config, workspace = _prepare_workspace_for_lecture(
                        client, args, course_id=course_id, sub_id=sub_id,
                    )
                    output_dir_str = str(workspace.raw_root)
                except Exception as e:
                    if item["attempts"] >= max_retries:
                        item["status"] = "failed"
                        item["error"] = (
                            f"Workspace setup failed after {item['attempts']} attempt(s): {e}"
                        )
                    else:
                        item["status"] = "pending"
                        item["error"] = (
                            f"Workspace setup attempt {item['attempts']}/{max_retries} failed: {e}"
                        )
                    _save_state()
                    continue

            result = _run_transcript_job(
                client=client,
                username=username,
                course_id=course_id,
                sub_id=sub_id,
                lecture_url="",
                output_dir=output_dir_str,
                tag="Batch",
                force_transcribe=getattr(args, "force_transcribe", False),
            )
            if result == 0 and workspace is not None:
                try:
                    _write_lecture_manifest_from_outputs(workspace=workspace, course_id=course_id, sub_id=sub_id,
                        lecture_url="", transcript_output_dir=Path(output_dir_str), slide_output_dir=workspace.raw_slides_dir,
                        note_style="")
                except Exception as exc:
                    _print_err(f"Failed to register transcript: {exc}")
                    result = 1

            if result == 0:
                item["status"] = "done"
                item["error"] = None
            else:
                if item["attempts"] >= max_retries:
                    item["status"] = "failed"
                    item["error"] = f"Failed after {item['attempts']} attempt(s), last code={result}"
                else:
                    item["status"] = "pending"
                    item["error"] = f"Attempt {item['attempts']}/{max_retries} failed"

            _save_state()

    except KeyboardInterrupt:
        print("\n[Batch] Interrupted by user. Saving current state...")
        for item in state.get("lectures", []):
            if item["status"] == "in_progress":
                item["status"] = "pending"
        _save_state()
        print(f"[Batch] State saved to {batch_state_path}. Re-run to continue.")
        return 130

    # Summary
    done_count = sum(1 for l in state["lectures"] if l["status"] == "done")
    failed_count = sum(1 for l in state["lectures"] if l["status"] == "failed")
    print(f"[Batch] Summary: {done_count} done, {failed_count} failed")
    if failed_count > 0:
        print("[Batch] Failed lectures:")
        for l in state["lectures"]:
            if l["status"] == "failed":
                print(f"  - {l['sub_title']} ({l['sub_id']}): {l.get('error', 'unknown')}")
    return 0 if failed_count == 0 else 3


def cmd_transcript(args: argparse.Namespace) -> int:
    try:
        client, username = _ensure_authenticated_client(force_login=args.force_login)
    except Exception as e:
        _print_err(str(e))
        return 1
    resolved = _resolve_course_sub(
        client,
        lecture_url=args.lecture_url,
        course_id=args.course_id,
        sub_id=args.sub_id,
        lecture_limit=args.lecture_limit,
        tag="Transcript",
    )
    if not resolved:
        return 2
    course_id, sub_id = resolved
    workspace = None
    if not args.output_dir or args.slide:
        try:
            _config, workspace = _prepare_workspace_for_lecture(client, args, course_id=course_id, sub_id=sub_id)
            if not args.output_dir:
                args.output_dir = str(workspace.raw_root)
        except Exception as e:
            _print_err(f"Workspace setup failed: {e}")
            return 1

    if args.slide:
        slide_output_dir = workspace.raw_slides_dir
        jwt_token = client.auth.get_jwt_token() or ""
        t_client = _build_client_from_jwt(jwt_token) if jwt_token else client
        s_client = _build_client_from_jwt(jwt_token) if jwt_token else client
        t_client = t_client or client
        s_client = s_client or client

        print("[Transcript] Running transcript and slide jobs in parallel...")
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
            fut_transcript = executor.submit(
                _run_transcript_job,
                client=t_client,
                username=username,
                course_id=course_id,
                sub_id=sub_id,
                lecture_url=args.lecture_url,
                output_dir=args.output_dir,
                tag="Transcript",
                force_transcribe=getattr(args, "force_transcribe", False),
            )
            fut_slide = executor.submit(
                _run_slide_job,
                client=s_client,
                username=username,
                course_id=course_id,
                sub_id=sub_id,
                lecture_url=args.lecture_url,
                output_dir=str(slide_output_dir),
                per_page=getattr(args, "per_page", 100),
                max_pages=getattr(args, "max_pages", 20),
                max_items=getattr(args, "max_items", 0),
                concurrency=getattr(args, "concurrency", 4),
                retries=getattr(args, "retries", 3),
                timeout=getattr(args, "timeout", 30),
                tag="Slide",
            )
            transcript_code = int(fut_transcript.result())
            slide_code = int(fut_slide.result())

        if transcript_code != 0:
            return transcript_code
        if slide_code != 0:
            print("[Transcript] Slide download failed or incomplete; transcript artifacts are still kept.")
        if workspace is not None:
            _write_lecture_manifest_from_outputs(workspace=workspace, course_id=course_id, sub_id=sub_id,
                lecture_url=args.lecture_url, transcript_output_dir=Path(args.output_dir),
                slide_output_dir=workspace.raw_slides_dir, note_style="", slides_regenerated=True)
        return 0

    result = _run_transcript_job(
        client=client,
        username=username,
        course_id=course_id,
        sub_id=sub_id,
        lecture_url=args.lecture_url,
        output_dir=args.output_dir,
        tag="Transcript",
        force_transcribe=getattr(args, "force_transcribe", False),
    )
    if result == 0 and workspace is not None:
        _write_lecture_manifest_from_outputs(workspace=workspace, course_id=course_id, sub_id=sub_id,
            lecture_url=args.lecture_url, transcript_output_dir=Path(args.output_dir),
            slide_output_dir=workspace.raw_slides_dir, note_style="")
    return result


def cmd_slide(args: argparse.Namespace) -> int:
    workspace = None
    try:
        client, username = _ensure_authenticated_client(force_login=args.force_login)
    except Exception as e:
        _print_err(str(e))
        return 1
    resolved = _resolve_course_sub(
        client,
        lecture_url=args.lecture_url,
        course_id=args.course_id,
        sub_id=args.sub_id,
        lecture_limit=args.lecture_limit,
        tag="Slide",
    )
    if not resolved:
        return 2
    course_id, sub_id = resolved
    if not args.output_dir:
        try:
            _config, workspace = _prepare_workspace_for_lecture(client, args, course_id=course_id, sub_id=sub_id)
            args.output_dir = str(workspace.raw_slides_dir)
        except Exception as e:
            _print_err(f"Workspace setup failed: {e}")
            return 1
    result = _run_slide_job(
        client=client,
        username=username,
        course_id=course_id,
        sub_id=sub_id,
        lecture_url=args.lecture_url,
        output_dir=args.output_dir,
        per_page=args.per_page,
        max_pages=args.max_pages,
        max_items=args.max_items,
        concurrency=args.concurrency,
        retries=args.retries,
        timeout=args.timeout,
        tag="Slide",
    )
    if result == 0 and workspace is not None:
        # A slide-only lecture has no transcript filename from which the GUI
        # can infer IDs. Record identity without replacing existing artifacts.
        manifest = {}
        if workspace.manifest_path.is_file():
            try:
                saved = json.loads(workspace.manifest_path.read_text(encoding="utf-8"))
                manifest = saved if isinstance(saved, dict) else {}
            except (OSError, ValueError):
                pass
        manifest.update({"course_id": course_id, "sub_id": sub_id,
                         "course_title": workspace.course_title, "session_title": workspace.session_title})
        manifest.setdefault("base_name", f"{course_id}_{sub_id}")
        manifest.setdefault("generated_at", _now_iso())
        manifest.setdefault("lecture_url", args.lecture_url or "")
        recorded = manifest.get("artifacts")
        recorded = dict(recorded) if isinstance(recorded, dict) else {}
        recorded.update({"slides_dir": str(Path(args.output_dir).resolve()),
                         "slide_index": str(Path(args.output_dir).resolve() / "index.json")})
        manifest["artifacts"] = recorded
        write_manifest(workspace, manifest)
    return result


def cmd_note(args: argparse.Namespace) -> int:
    try:
        client, username = _ensure_authenticated_client(force_login=args.force_login)
    except Exception as e:
        _print_err(str(e))
        return 1
    resolved = _resolve_course_sub(
        client,
        lecture_url=args.lecture_url,
        course_id=args.course_id,
        sub_id=args.sub_id,
        lecture_limit=args.lecture_limit,
        tag="Note",
    )
    if not resolved:
        return 2
    course_id, sub_id = resolved

    try:
        config, workspace = _prepare_workspace_for_lecture(client, args, course_id=course_id, sub_id=sub_id)
    except Exception as e:
        _print_err(f"Workspace setup failed: {e}")
        return 1

    transcript_output_dir = Path(args.output_dir).expanduser().resolve() if args.output_dir else workspace.raw_root
    slide_output_dir = (
        Path(args.slide_output_dir).expanduser().resolve()
        if args.slide_output_dir
        else workspace.raw_slides_dir
    )
    args.output_dir = str(transcript_output_dir)
    args.slide_output_dir = str(slide_output_dir)

    imported_materials: list[dict[str, Any]] = []
    materials = _collect_materials(args)
    if materials:
        imported_materials = import_materials(workspace, materials)
        print(f"[Materials] Imported {len(imported_materials)} material(s) into: {workspace.raw_materials_dir}")

    jwt_token = client.auth.get_jwt_token() or ""
    t_client = _build_client_from_jwt(jwt_token) if jwt_token else client
    s_client = _build_client_from_jwt(jwt_token) if jwt_token else client
    t_client = t_client or client
    s_client = s_client or client

    print("[Note] Running transcript and slide jobs in parallel...")
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        fut_transcript = executor.submit(
            _run_transcript_job,
            client=t_client,
            username=username,
            course_id=course_id,
            sub_id=sub_id,
            lecture_url=args.lecture_url,
            output_dir=args.output_dir,
            tag="Transcript",
            force_transcribe=getattr(args, "force_transcribe", False),
        )
        fut_slide = None
        if not args.no_slide:
            fut_slide = executor.submit(
                _run_slide_job,
                client=s_client,
                username=username,
                course_id=course_id,
                sub_id=sub_id,
                lecture_url=args.lecture_url,
                output_dir=args.slide_output_dir,
                per_page=args.per_page,
                max_pages=args.max_pages,
                max_items=args.max_items,
                concurrency=args.concurrency,
                retries=args.retries,
                timeout=args.timeout,
                tag="Slide",
            )

        transcript_code = int(fut_transcript.result())
        slide_code = int(fut_slide.result()) if fut_slide is not None else 0

    if transcript_code != 0:
        return transcript_code
    if slide_code != 0:
        print("[Note] Slide download failed or was incomplete; transcript artifacts are still kept.")
    _write_lecture_manifest_from_outputs(
        workspace=workspace,
        course_id=course_id,
        sub_id=sub_id,
        lecture_url=args.lecture_url,
        transcript_output_dir=transcript_output_dir,
        slide_output_dir=slide_output_dir,
        imported_materials=imported_materials,
        note_style=args.note_style,
        slides_regenerated=not args.no_slide,
    )

    # Duration check: warn if < 1 hour (non-blocking)
    meta_path = transcript_output_dir / f"{course_id}_{sub_id}.json"
    try:
        meta = json.loads(meta_path.read_text(encoding="utf-8"))
        duration_seconds = int(meta.get("duration_seconds") or 0)
        if 0 < duration_seconds < 3600:
            print(f"[Warning] 课时不足1小时({duration_seconds}秒)，转录可能不完整，建议重试")
    except Exception:
        pass

    try:
        site_dir = build_workspace_wiki(config)
        print(f"[Wiki] Built course site: {site_dir}")
    except Exception as e:
        _print_err(f"Course site build failed: {e}")
        return 1
    return 0


def cmd_add(args: argparse.Namespace) -> int:
    try:
        client, username = _ensure_authenticated_client(force_login=args.force_login)
    except Exception as e:
        _print_err(str(e))
        return 1
    resolved = _resolve_course_sub(
        client,
        lecture_url=args.lecture_url,
        course_id=args.course_id,
        sub_id=args.sub_id,
        lecture_limit=args.lecture_limit,
        tag="Add",
    )
    if not resolved:
        return 2
    course_id, sub_id = resolved

    try:
        config, workspace = _prepare_workspace_for_lecture(client, args, course_id=course_id, sub_id=sub_id)
    except Exception as e:
        _print_err(f"Workspace setup failed: {e}")
        return 1

    materials = _collect_materials(args)
    imported_materials: list[dict[str, Any]] = []
    if materials:
        imported_materials = import_materials(workspace, materials)
        print(f"[Add] Imported {len(imported_materials)} material(s) into: {workspace.raw_materials_dir}")
    else:
        print("[Add] No materials to import.")

    try:
        synced = index_workspace_wiki(config)
        print(f"[Add] Indexed {len(synced)} lesson manifest(s) into llm-wiki sources.")
    except Exception as e:
        _print_err(f"Index failed: {e}")
        return 1

    print("[Add] Done.")
    return 0


def cmd_cheatsheet(args: argparse.Namespace) -> int:
    template_dir = _cheatsheet_template_dir()
    if template_dir is None:
        _print_err("CheatingSheetTemplate/CheatingSheet.tex not found.")
        _print_err(
            "Searched: "
            + ", ".join(str(p) for p in (
                _skill_root() / "CheatingSheetTemplate",
                _skill_root() / ".mock-wiki" / "CheatingSheetTemplate",
            ))
        )
        return 1
    tex_path = template_dir / "CheatingSheet.tex"
    readme_path = template_dir / "README.md"

    output_format = (args.format or "html").strip().lower()
    if output_format not in ("tex", "html"):
        _print_err(f"Unsupported format: {output_format}. Use 'tex' or 'html'.")
        return 2

    xelatex_path = shutil.which("xelatex")
    if output_format == "tex":
        if xelatex_path is None:
            _print_err("xelatex not found. Install TeX Live to compile .tex files:")
            _print_err("  - Ubuntu/Debian: sudo apt install texlive-xetex")
            _print_err("  - macOS: brew install mactex")
            _print_err("  - Windows: Install MiKTeX from https://miktex.org/")
            return 1
        print(f"[Cheatsheet] xelatex found: {xelatex_path}")

    output_path = args.output or str(Path.cwd() / f"cheatsheet.{output_format}")
    print(f"[Cheatsheet] Format: {output_format}")
    print(f"[Cheatsheet] Output: {output_path}")
    print(f"[Cheatsheet] Template: {tex_path}")
    print(f"[Cheatsheet] Template directory: {template_dir}")
    if readme_path.exists():
        print(f"[Cheatsheet] Reference README: {readme_path}")
    print()
    print("[Cheatsheet] Note: CLI only checks the environment. Content generation is handled by the Agent.")
    if output_format == "html":
        print("[Cheatsheet] HTML output path ready. Pass this to the Agent for content generation.")
    return 0


def cmd_wiki(args: argparse.Namespace) -> int:
    return cmd_build(args)


def cmd_index(args: argparse.Namespace) -> int:
    try:
        config = ensure_workspace_config(
            workspace_root=args.workspace_root or None,
            owner_name=args.owner_name or None,
            site_name=args.site_name or None,
            interactive=not args.no_workspace_prompt,
        )
        synced = index_workspace_wiki(config)
    except Exception as e:
        _print_err(str(e))
        return 1
    if synced:
        print(f"[Index] Indexed {len(synced)} lesson manifest(s) into llm-wiki sources.")
    else:
        print("[Index] No lesson manifest found yet. Seeded an empty llm-wiki source page.")
    return 0


def cmd_build(args: argparse.Namespace) -> int:
    try:
        config = ensure_workspace_config(
            workspace_root=args.workspace_root or None,
            owner_name=args.owner_name or None,
            site_name=args.site_name or None,
            interactive=not args.no_workspace_prompt,
        )
        site_dir = build_workspace_wiki(config)
    except Exception as e:
        _print_err(str(e))
        return 1
    # Detect stub vs real wiki build
    site_index = site_dir / "index.html"
    if site_index.exists():
        content = site_index.read_text(encoding="utf-8")
        if "No lectures indexed yet" in content:
            _print_err("[Build] WARNING: No lectures indexed yet — site is a stub.")
            _print_err("[Build] Run /trans or /note to transcribe a lecture first.")
    print(f"[Build] Built course site: {site_dir}")
    print(f"[Build] Open: {site_dir / 'index.html'}")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    try:
        config = ensure_workspace_config(
            workspace_root=args.workspace_root or None,
            owner_name=args.owner_name or None,
            site_name=args.site_name or None,
            interactive=not args.no_workspace_prompt,
        )
    except Exception as e:
        _print_err(str(e))
        return 1
    return serve_workspace_wiki(
        config,
        port=args.port,
        host=args.host,
        open_browser=args.open,
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="look_tongji.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_setup = sub.add_parser("setup", help="Check deps and write <skill>/.env")
    p_setup.add_argument("--username", default="", help="Tongji username (optional)")
    p_setup.add_argument("--password", default="", help="Tongji password (optional; avoid CLI if possible)")
    p_setup.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_setup.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_setup.add_argument("--site-name", default="", help="Course wiki site name")
    p_setup.add_argument("--overwrite", action="store_true", help="Overwrite existing .env")
    p_setup.set_defaults(func=cmd_setup)

    p_list = sub.add_parser("list", help="Login and list recent courses")
    p_list.add_argument("--limit", type=int, default=0, help="Number of courses to show (0 = all)")
    p_list.add_argument(
        "--all",
        dest="all_courses",
        action="store_true",
        help="List all courses (slower but more complete)",
    )
    p_list.add_argument(
        "--query",
        default="",
        help="Filter courses by keyword in title/teacher (case-insensitive)",
    )
    p_list.add_argument("--choose", type=int, default=None, help="Auto-select course number (1-based)")
    p_list.add_argument("--force-login", action="store_true", help="Ignore cached JWT and login again")
    p_list.add_argument("--json", dest="json_output", action="store_true", help="Emit machine-readable JSON")
    p_list.add_argument("--no-prompt", action="store_true", help="Never prompt; just print the list")
    p_list.set_defaults(func=cmd_list)

    p_lectures = sub.add_parser("lectures", help="List lectures (sections) of a course as JSON")
    p_lectures.add_argument("--course-id", default="", help="Course ID")
    p_lectures.add_argument("--lecture-url", default="", help="Lecture page URL (course_id parsed from it)")
    p_lectures.add_argument("--force-login", action="store_true", help="Ignore cached JWT and login again")
    p_lectures.add_argument("--json", dest="json_output", action="store_true", help="Emit machine-readable JSON")
    p_lectures.set_defaults(func=cmd_lectures)

    p_status = sub.add_parser("status", help="Report environment and configuration state")
    p_status.add_argument("--json", dest="json_output", action="store_true", help="Emit machine-readable JSON")
    p_status.set_defaults(func=cmd_status)

    p_transcript = sub.add_parser("transcribe", aliases=["transcript", "trans"], help="Transcribe one lecture to SRT/TXT")
    p_transcript.add_argument("--lecture-url", default="", help="Tongji lecture page URL (best-effort parsing)")
    p_transcript.add_argument("--course-id", default="", help="Course ID")
    p_transcript.add_argument("--sub-id", default="", help="Lecture sub_id")
    p_transcript.add_argument("--lecture-limit", type=int, default=20, help="Max lectures shown for interactive choice")
    p_transcript.add_argument("--output-dir", default="", help="Output directory (default: ./tongji-output)")
    p_transcript.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_transcript.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_transcript.add_argument("--site-name", default="", help="Course wiki site name")
    p_transcript.add_argument("--no-workspace-prompt", action="store_true", help="Do not prompt for workspace config")
    p_transcript.add_argument("--force-login", action="store_true", help="Ignore cached JWT and login again")
    p_transcript.add_argument("--force-transcribe", action="store_true", help="Ignore saved subtitles and audio; download and transcribe again")
    p_transcript.add_argument("--slide", action="store_true", help="Also download slides in parallel with transcription")
    p_transcript.add_argument("--per-page", type=int, default=100, help="search-ppt per_page parameter (slide download)")
    p_transcript.add_argument("--max-pages", type=int, default=20, help="Max pages to request from search-ppt (slide download)")
    p_transcript.add_argument("--max-items", type=int, default=0, help="Download at most N slide snapshots (0 means all)")
    p_transcript.add_argument("--concurrency", type=int, default=4, help="Concurrent slide download workers (1-16)")
    p_transcript.add_argument("--retries", type=int, default=3, help="Retry attempts per slide image")
    p_transcript.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds per slide image request")
    p_transcript.set_defaults(func=cmd_transcript)

    p_batch = sub.add_parser("batch-transcribe", help="Batch transcribe all playable lectures for a course")
    p_batch.add_argument("--course-id", required=True, help="Course ID")
    p_batch.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_batch.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_batch.add_argument("--site-name", default="", help="Course wiki site name")
    p_batch.add_argument("--no-workspace-prompt", action="store_true", help="Do not prompt for workspace config")
    p_batch.add_argument("--output-dir", default="", help="Output directory for all lectures (default: per-lecture workspace)")
    p_batch.add_argument("--max-retries", type=int, default=3, help="Max retries per failed lecture")
    p_batch.add_argument("--force-login", action="store_true", help="Ignore cached JWT and login again")
    p_batch.add_argument("--force-transcribe", action="store_true", help="Ignore saved subtitles/audio for pending lectures")
    p_batch.add_argument("--retry-failed", action="store_true", help="Retry exhausted failures; completed lectures stay completed")
    p_batch.set_defaults(func=cmd_batch_transcribe)

    p_slide = sub.add_parser("slide", help="Download lecture slide snapshots for one lecture")
    p_slide.add_argument("--lecture-url", default="", help="Tongji lecture page URL (best-effort parsing)")
    p_slide.add_argument("--course-id", default="", help="Course ID")
    p_slide.add_argument("--sub-id", default="", help="Lecture sub_id")
    p_slide.add_argument("--lecture-limit", type=int, default=20, help="Max lectures shown for interactive choice")
    p_slide.add_argument(
        "--output-dir",
        default="",
        help="Output directory (default: ./tongji-output/slide_<course_id>_<sub_id>)",
    )
    p_slide.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_slide.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_slide.add_argument("--site-name", default="", help="Course wiki site name")
    p_slide.add_argument("--no-workspace-prompt", action="store_true", help="Do not prompt for workspace config")
    p_slide.add_argument("--per-page", type=int, default=100, help="search-ppt per_page parameter")
    p_slide.add_argument("--max-pages", type=int, default=20, help="Max pages to request from search-ppt")
    p_slide.add_argument("--max-items", type=int, default=0, help="Download at most N snapshots (0 means all)")
    p_slide.add_argument("--concurrency", type=int, default=4, help="Concurrent download workers (1-16)")
    p_slide.add_argument("--retries", type=int, default=3, help="Retry attempts per image")
    p_slide.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds per image request")
    p_slide.add_argument("--force-login", action="store_true", help="Ignore cached JWT and login again")
    p_slide.set_defaults(func=cmd_slide)

    p_note = sub.add_parser("note", help="Run transcript + slide in parallel for one lecture")
    p_note.add_argument("--lecture-url", default="", help="Tongji lecture page URL (best-effort parsing)")
    p_note.add_argument("--course-id", default="", help="Course ID")
    p_note.add_argument("--sub-id", default="", help="Lecture sub_id")
    p_note.add_argument("--lecture-limit", type=int, default=20, help="Max lectures shown for interactive choice")
    p_note.add_argument("--output-dir", default="", help="Transcript output directory (default: ./tongji-output)")
    p_note.add_argument(
        "--slide-output-dir",
        default="",
        help="Slide output directory (default: ./tongji-output/slide_<course_id>_<sub_id>)",
    )
    p_note.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_note.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_note.add_argument("--site-name", default="", help="Course wiki site name")
    p_note.add_argument("--material", action="append", default=[], help="Supplementary material as name=path or path")
    p_note.add_argument("--no-material-prompt", action="store_true", help="Do not ask for supplementary materials")
    p_note.add_argument("--no-workspace-prompt", action="store_true", help="Do not prompt for workspace config")
    p_note.add_argument("--no-slide", action="store_true", help="Skip slide download and run transcript only")
    p_note.add_argument("--per-page", type=int, default=100, help="search-ppt per_page parameter")
    p_note.add_argument("--max-pages", type=int, default=20, help="Max pages to request from search-ppt")
    p_note.add_argument("--max-items", type=int, default=0, help="Download at most N slide snapshots (0 means all)")
    p_note.add_argument("--concurrency", type=int, default=4, help="Concurrent slide download workers (1-16)")
    p_note.add_argument("--retries", type=int, default=3, help="Retry attempts per slide image")
    p_note.add_argument("--timeout", type=int, default=30, help="HTTP timeout seconds per slide image request")
    p_note.add_argument(
        "--note-style",
        default="standard",
        choices=["standard", "dialogue"],
        help="Note writing style: standard (lecture notes) or dialogue (Q&A format)",
    )
    p_note.add_argument("--force-login", action="store_true", help="Ignore cached JWT and login again")
    p_note.add_argument("--force-transcribe", action="store_true", help="Ignore saved subtitles and audio; download and transcribe again")
    p_note.set_defaults(func=cmd_note)

    p_add = sub.add_parser("add", help="Import materials into a lecture workspace without transcription")
    p_add.add_argument("--lecture-url", default="", help="Tongji lecture page URL (best-effort parsing)")
    p_add.add_argument("--course-id", default="", help="Course ID")
    p_add.add_argument("--sub-id", default="", help="Lecture sub_id")
    p_add.add_argument("--lecture-limit", type=int, default=20, help="Max lectures shown for interactive choice")
    p_add.add_argument("--material", action="append", default=[], help="Supplementary material as name=path or path")
    p_add.add_argument("--no-material-prompt", action="store_true", help="Do not ask for supplementary materials")
    p_add.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_add.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_add.add_argument("--site-name", default="", help="Course wiki site name")
    p_add.add_argument("--no-workspace-prompt", action="store_true", help="Do not prompt for workspace config")
    p_add.add_argument("--force-login", action="store_true", help="Ignore cached JWT and login again")
    p_add.set_defaults(func=cmd_add)

    p_cheatsheet = sub.add_parser("cheatsheet", help="Check environment for cheatsheet generation")
    p_cheatsheet.add_argument("--course-id", default="", help="Course ID (for context, not used for generation)")
    p_cheatsheet.add_argument("--format", default="html", choices=["tex", "html"], help="Output format")
    p_cheatsheet.add_argument("--output", default="", help="Output file path")
    p_cheatsheet.set_defaults(func=cmd_cheatsheet)

    p_index = sub.add_parser("index", help="Index lesson manifests into the llm-wiki workspace")
    p_index.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_index.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_index.add_argument("--site-name", default="", help="Course wiki site name")
    p_index.add_argument("--no-workspace-prompt", action="store_true", help="Do not prompt for workspace config")
    p_index.set_defaults(func=cmd_index)

    p_build = sub.add_parser("build", help="Build the static course wiki from the configured workspace")
    p_build.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_build.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_build.add_argument("--site-name", default="", help="Course wiki site name")
    p_build.add_argument("--no-workspace-prompt", action="store_true", help="Do not prompt for workspace config")
    p_build.set_defaults(func=cmd_build)

    p_wiki = sub.add_parser("wiki", help="Compatibility alias of build for rebuilding the static course wiki")
    p_wiki.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_wiki.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_wiki.add_argument("--site-name", default="", help="Course wiki site name")
    p_wiki.add_argument("--no-workspace-prompt", action="store_true", help="Do not prompt for workspace config")
    p_wiki.set_defaults(func=cmd_wiki)

    p_serve = sub.add_parser("serve", help="Start local HTTP server for the generated course wiki")
    p_serve.add_argument("--workspace-root", default="", help="Persistent course wiki workspace root")
    p_serve.add_argument("--owner-name", default="", help="Owner name shown in the course wiki")
    p_serve.add_argument("--site-name", default="", help="Course wiki site name")
    p_serve.add_argument("--no-workspace-prompt", action="store_true", help="Do not prompt for workspace config")
    p_serve.add_argument("--port", type=int, default=8765)
    p_serve.add_argument("--host", default="127.0.0.1")
    p_serve.add_argument("--open", action="store_true", help="Open browser after starting")
    p_serve.set_defaults(func=cmd_serve)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
