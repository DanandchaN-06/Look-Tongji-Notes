"""Local-only HTTP server for the console.

Security posture:
- Binds to 127.0.0.1 only.
- Every API call needs a per-run random token (header `X-Console-Token`).
- `Host` and, when present, `Origin` must be a loopback origin.
- Only whitelisted static files are served; no path is taken from the client
  for reading arbitrary files.
- No `shell=True` anywhere; the browser can only select from known task kinds.
"""

from __future__ import annotations

import hmac
import json
import mimetypes
import os
import shutil
import subprocess
import socket
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie, CookieError
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from . import agent_bridge, config_store, native, library, features
from . import __version__
from .core import (
    REPO_ROOT,
    SCRIPTS_DIR,
    WEB_DIR,
    CliResult,
    child_popen_kwargs,
    extract_json,
    run_cli,
    redact,
)
from .runner import TASK_KINDS, TaskManager

STATIC_FILES = {"index.html", "app.css", "app.js", "task-progress.js"}

# Bodies are small JSON payloads; anything larger is refused rather than buffered.
MAX_BODY_BYTES = 4 * 1024 * 1024
# Longest a synchronous CLI call (login, course listing) may take.
CLI_TIMEOUT_SECONDS = 300
# Also used to cap subprocesses spawned directly by the server.
SUBPROCESS_TIMEOUT_SECONDS = 180


class ConsoleState:
    def __init__(self, token: str) -> None:
        self.token = token
        self.tasks = TaskManager()


class Handler(BaseHTTPRequestHandler):
    server_version = f"LookTongjiConsole/{__version__}"
    protocol_version = "HTTP/1.1"

    state: ConsoleState  # injected via subclass

    # -- helpers -----------------------------------------------------------

    def log_message(self, fmt: str, *args: Any) -> None:  # silence default logging
        return

    def handle_one_request(self) -> None:
        try:
            super().handle_one_request()
        except ConnectionError:
            # Closing/reloading the browser can abort an in-flight response.
            self.close_connection = True

    def _origin_ok(self) -> bool:
        try:
            host = urlparse("//" + (self.headers.get("Host") or ""))
            if host.hostname not in ("127.0.0.1", "localhost") or (host.port or 80) != self.server.server_port:
                return False
            origin = self.headers.get("Origin")
            if origin:
                parsed = urlparse(origin)
                if (parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost")
                        or (parsed.port or 80) != self.server.server_port):
                    return False
        except ValueError:
            return False
        return True

    def _cookie_name(self) -> str:
        return f"look_console_{self.server.server_port}"

    def _token_ok(self, query: dict[str, list[str]]) -> bool:
        expected = self.state.token
        supplied = self.headers.get("X-Console-Token") or ""
        if not supplied:
            values = query.get("token") or []
            supplied = values[0] if values else ""
        if not supplied:
            cookie = SimpleCookie()
            try:
                cookie.load(self.headers.get("Cookie") or "")
                value = cookie.get(self._cookie_name())
                supplied = value.value if value else ""
            except CookieError:
                return False
        return bool(supplied) and hmac.compare_digest(supplied.encode("utf-8"), expected.encode("utf-8"))

    def _send_json(self, payload: Any, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def _send_text(self, text: str, status: int = 200) -> None:
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _drain_request_body(self) -> bytes:
        """Read the request body exactly once, before any response is sent.

        With HTTP/1.1 keep-alive, responding before consuming the body leaves
        the unread bytes in the socket and the *next* request on the same
        connection is parsed from the wrong offset. So drain first, then decide
        whether to authorise.
        """
        cached = getattr(self, "_body_cache", None)
        if cached is not None:
            return cached
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            self.close_connection = True
            raise ValueError("Content-Length 必须是非负整数")
        if length < 0 or self.headers.get("Transfer-Encoding"):
            self.close_connection = True
            raise ValueError("不支持的请求体长度或编码")
        if length <= 0:
            self._body_cache = b""
            return self._body_cache
        if length > MAX_BODY_BYTES:
            # Refuse to buffer an oversized body; the connection cannot be
            # safely reused either, so let it close.
            self.close_connection = True
            self._body_cache = b""
            return self._body_cache
        self._body_cache = self.rfile.read(length)
        return self._body_cache

    def _read_json_body(self) -> dict[str, Any]:
        raw = self._drain_request_body()
        if not raw:
            return {}
        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ValueError("请求体不是合法 JSON")
        if not isinstance(parsed, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return parsed

    # -- HTTP verbs --------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        self._body_cache = None
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        # Read (and discard) any GET body so keep-alive stays in sync.
        try:
            self._drain_request_body()
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return

        if not self._origin_ok():
            self._send_json({"ok": False, "error": "拒绝非本地来源的请求"}, 403)
            return

        if parsed.path in ("/", "/index.html"):
            if not self._token_ok(query):
                self._send_text("无效或缺失的访问令牌。请从控制台启动器打开页面。", 403)
                return
            self._serve_static("index.html")
            return

        # Static assets carry no secrets and are referenced without a token by
        # the HTML. Every /api/* route still requires the token.
        if parsed.path in ("/app.css", "/app.js", "/task-progress.js"):
            self._serve_static(parsed.path.lstrip("/"))
            return

        if parsed.path.startswith("/api/"):
            if not self._token_ok(query):
                self._send_json({"ok": False, "error": "令牌无效"}, 401)
                return
            try:
                self._route_get(parsed.path, query, parsed)
            except ConnectionError:
                self.close_connection = True
            except ValueError as exc:
                self._send_json({"ok": False, "error": redact(str(exc))}, 400)
            except Exception as exc:
                self._send_json({"ok": False, "error": redact(f"内部错误: {exc}")}, 500)
            return

        self._send_json({"ok": False, "error": "未找到"}, 404)

    def do_POST(self) -> None:  # noqa: N802
        self._body_cache = None
        parsed = urlparse(self.path)
        query = parse_qs(parsed.query)

        # Must happen before any early return, see _drain_request_body.
        try:
            raw = self._drain_request_body()
        except ValueError as exc:
            self._send_json({"ok": False, "error": str(exc)}, 400)
            return
        if int(self.headers.get("Content-Length") or 0) > MAX_BODY_BYTES:
            self._send_json({"ok": False, "error": "请求体超过大小限制"}, 413)
            return

        if not self._origin_ok():
            self._send_json({"ok": False, "error": "拒绝非本地来源的请求"}, 403)
            return
        if not self._token_ok(query):
            self._send_json({"ok": False, "error": "令牌无效"}, 401)
            return
        if not parsed.path.startswith("/api/"):
            self._send_json({"ok": False, "error": "未找到"}, 404)
            return

        try:
            body: dict[str, Any] = {}
            if raw:
                parsed_body = json.loads(raw.decode("utf-8"))
                if not isinstance(parsed_body, dict):
                    raise ValueError("请求体必须是 JSON 对象")
                body = parsed_body
        except (ValueError, UnicodeDecodeError):
            self._send_json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return

        self._route_post(parsed.path, body)

    # -- static ------------------------------------------------------------

    def _serve_static(self, name: str) -> None:
        if name not in STATIC_FILES:
            self._send_text("forbidden", 403)
            return
        path = WEB_DIR / name
        if not path.exists():
            self._send_text(f"missing: {name}", 500)
            return
        body = path.read_bytes()
        ctype = mimetypes.guess_type(name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{ctype}; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'self' 'unsafe-inline'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        if name == "index.html":
            # A port-specific, HttpOnly session cookie lets the same tab reload
            # after JS has removed the one-time token from its address bar.
            self.send_header("Set-Cookie", f"{self._cookie_name()}={self.state.token}; Path=/; HttpOnly; SameSite=Strict")
        self.end_headers()
        self.wfile.write(body)

    # -- routing -----------------------------------------------------------

    def _route_get(self, path: str, query: dict[str, list[str]], parsed: Any) -> None:
        if path == "/api/status":
            self._api_status()
        elif path == "/api/config":
            self._send_json({
                "ok": True,
                "credentials": config_store.public_credentials(),
                "workspace": config_store.public_workspace(),
                "vision": config_store.public_vision(),
                "repo_root": str(REPO_ROOT),
            })
        elif path == "/api/features":
            self._send_json({"ok": True, "upstream_commit": features.UPSTREAM_COMMIT, "features": features.FEATURES})
        elif path == "/api/batch/state":
            self._send_json({"ok": True, **library.batch_state((query.get("course_id") or [""])[0])})
        elif path == "/api/publish/check":
            self._send_json({"ok": True, **library.publish_preflight()})
        elif path == "/api/courses":
            self._api_courses(query)
        elif path == "/api/lectures":
            self._api_lectures(query)
        elif path == "/api/artifacts":
            self._api_artifacts(query)
        elif path == "/api/agent/instruction":
            self._api_agent_instruction(query)
        elif path == "/api/agent/verify":
            q = lambda key, default="": (query.get(key) or [default])[0]
            self._send_json({"ok": True, **agent_bridge.verify_agent_output(
                kind=q("kind", "note"), course_id=q("course_id"), sub_id=q("sub_id"),
                note_style=q("note_style", "standard"),
                include_timeline=q("include_timeline", "1") in ("1", "true", "yes"),
                cheatsheet_format=q("cheatsheet_format", "html"), cheatsheet_scope=q("cheatsheet_scope", "lecture"),
                cheatsheet_output=q("cheatsheet_output"))})
        elif path == "/api/tasks":
            self._send_json({"ok": True, "tasks": self.state.tasks.list()})
        elif path.startswith("/api/tasks/"):
            self._api_task_detail(path, query)
        elif path == "/api/task-kinds":
            self._send_json({
                "ok": True,
                "kinds": [{"kind": k, "label": v["label"]} for k, v in TASK_KINDS.items()],
            })
        else:
            self._send_json({"ok": False, "error": "未找到"}, 404)

    def _route_post(self, path: str, body: dict[str, Any]) -> None:
        try:
            if path == "/api/config/credentials":
                self._api_save_credentials(body)
            elif path == "/api/lecture/resolve":
                url = str(body.get("url") or "").strip()
                parsed = urlparse(url)
                if len(url) > 4096 or parsed.scheme not in ("http", "https") or parsed.hostname != "look.tongji.edu.cn":
                    raise ValueError("请粘贴 look.tongji.edu.cn 的课程回放链接")
                from scripts.look_tongji import _extract_ids_from_url
                course_id, sub_id = _extract_ids_from_url(url)
                if not course_id:
                    raise ValueError("链接中没有课程标识，请从课程列表选择")
                self._send_json({"ok": True, "course_id": course_id, "sub_id": sub_id or ""})
            elif path == "/api/config/workspace":
                self._api_save_workspace(body)
            elif path == "/api/config/vision":
                self._api_save_vision(body)
            elif path == "/api/vision/test":
                self._api_vision_test(body)
            elif path == "/api/pick":
                self._api_pick(body)
            elif path == "/api/tasks":
                self._api_start_task(body)
            elif path == "/api/reveal":
                self._api_reveal(body)
            elif path.endswith("/cancel"):
                task_id = path.split("/")[3]
                self._send_json({"ok": True, **self.state.tasks.cancel(task_id)})
            else:
                self._send_json({"ok": False, "error": "未找到"}, 404)
        except ConnectionError:
            self.close_connection = True
        except KeyError:
            self._send_json({"ok": False, "error": "任务不存在"}, 404)
        except ValueError as exc:
            self._send_json({"ok": False, "error": redact(str(exc))}, 400)
        except Exception as exc:  # pragma: no cover - defensive
            self._send_json({"ok": False, "error": redact(f"内部错误: {exc}")}, 500)

    # -- api: status -------------------------------------------------------

    def _api_status(self) -> None:
        result: CliResult = run_cli(["status", "--json"], timeout=60)
        payload = extract_json(result.stdout)
        if payload is None:
            self._send_json({
                "ok": False,
                "error": "无法读取环境状态",
                "returncode": result.returncode,
                "stderr": result.stderr[-2000:],
            }, 502)
            return
        payload["console"] = {
            "version": __version__,
            "repo_root": str(REPO_ROOT),
            "scripts_dir": str(SCRIPTS_DIR),
            "python": sys.executable,
        }
        # Probing is cached after the first call, so this stays cheap.
        try:
            payload["console"]["picker_available"] = native.picker_available()
            payload["console"]["picker_python"] = native.find_picker_python() or ""
        except Exception:
            payload["console"]["picker_available"] = False
            payload["console"]["picker_python"] = ""
        payload["vision_model"] = config_store.public_vision()
        payload["workspace_config"] = config_store.public_workspace()
        payload["credentials"] = config_store.public_credentials()
        self._send_json(payload)

    # -- api: courses / lectures -------------------------------------------

    def _api_courses(self, query: dict[str, list[str]]) -> None:
        cli_args = ["list", "--json", "--no-prompt"]
        if (query.get("force_login") or ["0"])[0] == "1":
            cli_args.append("--force-login")
        if (query.get("all") or [""])[0] in ("1", "true", "yes"):
            cli_args.append("--all")
        keyword = (query.get("query") or [""])[0].strip()
        if keyword:
            cli_args += ["--query", keyword]

        result = run_cli(cli_args, timeout=CLI_TIMEOUT_SECONDS)
        payload = extract_json(result.stdout)
        if payload is None:
            self._send_json({
                "ok": False,
                "error": "获取课程列表失败，请检查账号配置与网络。",
                "returncode": result.returncode,
                "detail": (result.stderr or result.stdout)[-1500:],
            }, 502)
            return
        self._send_json(payload)

    def _api_lectures(self, query: dict[str, list[str]]) -> None:
        course_id = (query.get("course_id") or [""])[0].strip()
        if not course_id:
            self._send_json({"ok": False, "error": "缺少 course_id"}, 400)
            return
        result = run_cli(["lectures", "--json", "--course-id", course_id], timeout=180)
        payload = extract_json(result.stdout)
        if payload is None:
            self._send_json({
                "ok": False,
                "error": "获取节次列表失败。",
                "returncode": result.returncode,
                "detail": (result.stderr or result.stdout)[-1500:],
            }, 502)
            return
        self._send_json(payload)

    # -- api: artifacts / agent -------------------------------------------

    def _api_artifacts(self, query: dict[str, list[str]]) -> None:
        course_id = (query.get("course_id") or [""])[0]
        sub_id = (query.get("sub_id") or [""])[0]
        self._send_json({"ok": True, "artifacts": agent_bridge.detect_artifacts(course_id, sub_id)})

    def _api_agent_instruction(self, query: dict[str, list[str]]) -> None:
        def q(key: str, default: str = "") -> str:
            return (query.get(key) or [default])[0]

        course_id, sub_id = q("course_id"), q("sub_id")
        artifacts = agent_bridge.detect_artifacts(course_id, sub_id)
        payload = agent_bridge.build_agent_instruction(
            kind=q("kind", "note"),
            course_id=course_id,
            sub_id=sub_id,
            note_style=q("note_style", "standard"),
            include_timeline=q("include_timeline", "1") in ("1", "true", "yes"),
            cheatsheet_format=q("cheatsheet_format", "html"),
            cheatsheet_output=q("cheatsheet_output"),
            artifacts=artifacts,
            agent=q("agent", "generic"),
            course_context={key: q(key) for key in ("course_title", "teacher", "session_title", "date")},
            cheatsheet_scope=q("cheatsheet_scope", "lecture"),
            publish_repository=config_store.public_credentials().get("gh_pages_repo", ""),
        )
        payload["artifacts"] = artifacts
        self._send_json({"ok": True, **payload})

    # -- api: config writes ------------------------------------------------

    def _api_save_credentials(self, body: dict[str, Any]) -> None:
        username = str(body.get("username") or "").strip()
        password = body.get("password")
        updates: dict[str, str] = {}
        if username:
            updates["TONGJI_USERNAME"] = username
        if isinstance(password, str) and password:
            updates["TONGJI_PASSWORD"] = password
        repo = str(body.get("gh_pages_repo") or "").strip()
        if repo:
            updates["GH_PAGES_REPO"] = repo
        if not updates:
            raise ValueError("没有需要保存的内容")
        config_store.write_env(updates)
        self._send_json({"ok": True, "credentials": config_store.public_credentials()})

    def _api_save_workspace(self, body: dict[str, Any]) -> None:
        root = str(body.get("workspace_root") or "").strip()
        if not root:
            raise ValueError("必须填写知识库保存路径")
        migrate = bool(body.get("migrate"))
        result = config_store.save_workspace(
            workspace_root=root,
            owner_name=str(body.get("owner_name") or ""),
            site_name=str(body.get("site_name") or ""),
            migrate=migrate,
        )
        self._send_json({"ok": True, "workspace": config_store.public_workspace(), "detail": result})

    def _api_save_vision(self, body: dict[str, Any]) -> None:
        result = config_store.save_vision(
            provider_id=str(body.get("provider") or "").strip(),
            model=str(body.get("model") or "").strip(),
            api_key=str(body.get("api_key") or ""),
            base_url=str(body.get("base_url") or ""),
            api_format=str(body.get("api_format") or ""),
        )
        self._send_json({"ok": True, "detail": result, "vision": config_store.public_vision()})

    def _api_vision_test(self, body: dict[str, Any]) -> None:
        image = str(body.get("image") or "").strip()
        if not image:
            raise ValueError("缺少测试图片路径")
        target = Path(image).expanduser().resolve()
        allowed_roots = [REPO_ROOT.resolve()]
        workspace = config_store.public_workspace()
        if workspace:
            try:
                allowed_roots.append(Path(workspace["workspace_root"]).resolve())
            except OSError:
                pass
        if not any(root == target or root in target.parents for root in allowed_roots):
            raise ValueError("只能测试项目目录或知识库目录下的图片")
        if not target.exists():
            raise ValueError(f"图片不存在: {target}")

        script = REPO_ROOT / "vision-support" / "scripts" / "vision.mjs"
        if not script.exists():
            raise ValueError("未找到 vision-support/scripts/vision.mjs")

        node = shutil.which("node")
        if not node:
            raise ValueError(
                "未找到 Node.js，无法调用 vision-support。请先安装 Node.js 18 以上版本。"
            )

        try:
            proc = subprocess.run(
                [node, str(script), str(target)],
                cwd=str(REPO_ROOT / "vision-support"),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=SUBPROCESS_TIMEOUT_SECONDS,
                **child_popen_kwargs(),
            )
        except subprocess.TimeoutExpired:
            self._send_json({
                "ok": False,
                "returncode": -1,
                "stdout": "",
                "stderr": f"识图调用超时（{SUBPROCESS_TIMEOUT_SECONDS}s），已终止。",
            })
            return
        from .core import redact

        self._send_json({
            "ok": proc.returncode == 0,
            "returncode": proc.returncode,
            "stdout": redact((proc.stdout or "")[-4000:]),
            "stderr": redact((proc.stderr or "")[-2000:]),
        })

    # -- api: native dialog -------------------------------------------------

    def _api_pick(self, body: dict[str, Any]) -> None:
        """Open a native folder/file dialog and return the real local path.

        The browser cannot supply a genuine path (the File API only yields
        `C:\\fakepath\\...`), so the selection has to be made by a native
        dialog on the server side. The modal loop lives in a child process, so
        it never blocks this handler beyond the wait itself.
        """
        mode = str(body.get("mode") or "").strip()
        if mode not in ("dir", "files"):
            raise ValueError("mode 只能是 dir 或 files")

        if not native.DIALOG_LOCK.acquire(blocking=False):
            raise ValueError("已有一个选择框正在等待操作，请先完成或关闭它。")
        try:
            try:
                result = native.run_picker(
                    mode,
                    initial=str(body.get("initial") or ""),
                    title=str(body.get("title") or "请选择"),
                )
            except RuntimeError as exc:
                raise ValueError(str(exc))
        finally:
            native.DIALOG_LOCK.release()

        self._send_json({"ok": True, **result})

    # -- api: tasks --------------------------------------------------------

    def _api_start_task(self, body: dict[str, Any]) -> None:
        kind = str(body.get("kind") or "").strip()
        if kind not in TASK_KINDS:
            raise ValueError(f"未知任务类型: {kind}")
        params = body.get("params") or {}
        if not isinstance(params, dict):
            raise ValueError("params 必须是对象")
        task = self.state.tasks.start(kind, params)
        self._send_json({"ok": True, "task": task.snapshot()})

    def _task_snapshot(self, task: Any) -> dict[str, Any]:
        snapshot = task.snapshot()
        if task.kind == "serve" and task.status == "running":
            port = int(task.params["port"])
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=.3):
                    snapshot["preview_url"] = f"http://127.0.0.1:{port}/"
            except OSError:
                pass
        return snapshot

    def _api_task_detail(self, path: str, query: dict[str, list[str]]) -> None:
        parts = path.strip("/").split("/")
        if len(parts) < 3:
            self._send_json({"ok": False, "error": "未找到"}, 404)
            return
        task_id = parts[2]
        task = self.state.tasks.get(task_id)
        if task is None:
            self._send_json({"ok": False, "error": "任务不存在"}, 404)
            return
        if len(parts) == 4 and parts[3] == "log":
            offset = int((query.get("offset") or ["0"])[0] or 0)
            payload = task.tail(offset)
            self._send_json({"ok": True, "task": task.snapshot(), **payload})
            return
        self._send_json({"ok": True, "task": self._task_snapshot(task)})

    def _api_reveal(self, body: dict[str, Any]) -> None:
        raw = str(body.get("path") or "").strip()
        if not raw:
            raise ValueError("缺少路径")
        target = Path(raw).expanduser().resolve()
        allowed_roots = [REPO_ROOT.resolve()]
        workspace = config_store.public_workspace()
        if workspace:
            try:
                allowed_roots.append(Path(workspace["workspace_root"]).resolve())
            except OSError:
                pass
        if not any(root == target or root in target.parents for root in allowed_roots):
            raise ValueError("只能打开项目目录或知识库目录下的位置")
        if not target.exists():
            raise ValueError("路径不存在")

        if os.name == "nt":
            os.startfile(str(target))  # noqa: S606 - no shell, no args
        elif sys.platform == "darwin":
            subprocess.run(["open", str(target)], check=False)
        else:
            subprocess.run(["xdg-open", str(target)], check=False)
        self._send_json({"ok": True})


class ConsoleHTTPServer(ThreadingHTTPServer):
    def server_close(self) -> None:
        self.RequestHandlerClass.state.tasks.shutdown()
        super().server_close()


def make_server(host: str, port: int, token: str) -> ThreadingHTTPServer:
    if host != "127.0.0.1":
        raise ValueError("控制台只允许绑定 127.0.0.1")
    state = ConsoleState(token=token)

    class BoundHandler(Handler):
        pass

    BoundHandler.state = state  # type: ignore[attr-defined]

    httpd = ConsoleHTTPServer((host, port), BoundHandler)
    httpd.daemon_threads = True
    return httpd
