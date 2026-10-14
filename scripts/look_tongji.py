Warning: truncated output (original token count: 22483)
Total output lines: 2204

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
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
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

    from tongji_backend.auth import TongjiAuth
    from tongji_backend.client import TongjiClient

    with _account_auth_refresh_lock(username):
        # Another process may have refreshed the same account while this
        # process was checking the previous cached token.
        if not force_login:
            cached = _load_auth_session()
            if cached:
                client = _build_client_from_jwt(cached.jwt_token)
                if client is not None:
                    return client, cached.username
        auth = TongjiAuth()
        auth.login(username=username, password=password)
        jwt_token = auth.get_jwt_token() or ""
        if jwt_token:
            _save_auth_session(AuthSession(username=username, jwt_token=jwt_token))
        return TongjiClient(auth), username


def _account_auth_refresh_lock(username: str):
    from tongji_backend.media_cache import account_auth_refresh_lock

    return account_auth_refresh_lock(_state_dir(), username)


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

    if not courses and not getattr(args, "json_output", False):
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
    detail = {**detail, 'course_id': course_id}
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


def _write_lecture_manifest_from_outputs(**kwargs: Any) -> None:
    workspace = kwargs["workspace"]
    from tongji_backend.media_cache import lecture_lock
    with lecture_lock(workspace.raw_root / ".manifest.lock"):
        _write_lecture_manifest_from_outputs_unlocked(**kwargs)


def _write_lecture…7483 tokens truncated…_transcribe", False),
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
                slide_output_dir=workspace.raw_slides_dir, note_style="", slides_regenerated=True,
                defer_site_build=getattr(args, "defer_site_build", False))
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
            slide_output_dir=workspace.raw_slides_dir, note_style="",
            defer_site_build=getattr(args, "defer_site_build", False))
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
        from tongji_backend.media_cache import lecture_lock
        with lecture_lock(workspace.raw_root / ".manifest.lock"):
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
            write_manifest(workspace, manifest, defer_site_build=getattr(args, "defer_site_build", False))
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
        defer_site_build=getattr(args, "defer_site_build", False),
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

    return _build_workspace_if_requested(config, defer=getattr(args, "defer_site_build", False))


def _build_workspace_if_requested(config: WorkspaceConfig, *, defer: bool) -> int:
    if defer:
        print("[Wiki] Site build deferred; workspace index will be refreshed by the build workflow.")
        return 0
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
    p_transcript.add_argument("--defer-site-build", action="store_true", help="Defer shared workspace indexing and site build")
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
    p_slide.add_argument("--defer-site-build", action="store_true", help="Defer shared workspace indexing and site build")
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
    p_note.add_argument("--defer-site-build", action="store_true", help="Defer shared workspace indexing and site build")
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
