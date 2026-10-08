"""Task execution: run CLI commands in the background and stream their output.

Everything here is whitelisted. The browser can only ask for a known task kind
with known parameters; it can never supply a raw command line.
"""

from __future__ import annotations

import os
import json
import subprocess
import threading
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable
from . import progress

from .core import (
    REPO_ROOT,
    base_command,
    child_popen_kwargs,
    cli_env,
    kill_process_tree,
    redact,
    redact_env_values,
    secret_values,
)

MAX_LOG_LINES = 20000

# --------------------------------------------------------------------------
# Task definitions
# --------------------------------------------------------------------------

# kind -> (cli args, extra env keys allowed)
TASK_KINDS: dict[str, dict[str, Any]] = {
    "transcribe": {"cli": ["transcribe"], "label": "单节转写"},
    "slide": {"cli": ["slide"], "label": "课件截图下载"},
    "note": {"cli": ["note"], "label": "采集字幕与课件"},
    "add": {"cli": ["add"], "label": "导入补充材料"},
    "batch": {"cli": ["batch-transcribe"], "label": "整门课批量转写"},
    "index": {"cli": ["index"], "label": "建立知识库索引"},
    "build": {"cli": ["build"], "label": "构建知识库站点"},
    "cheatsheet": {"cli": ["cheatsheet"], "label": "检查速查表环境"},
    "serve": {"cli": ["serve"], "label": "本地预览知识库服务"},
}

LONG_RUNNING = {"serve"}


@dataclass
class Task:
    id: str
    kind: str
    params: dict[str, Any]
    argv: list[str]
    status: str = "queued"  # queued | running | succeeded | failed | cancelled
    returncode: int | None = None
    created_at: float = field(default_factory=time.time)
    started_at: float | None = None
    ended_at: float | None = None
    error: str = ""
    log: list[str] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _proc: subprocess.Popen | None = field(default=None, repr=False)
    _cancel: threading.Event = field(default_factory=threading.Event, repr=False)
    _dropped: int = field(default=0, repr=False)
    _progress: dict[str, dict[str, Any]] = field(default_factory=dict, repr=False)

    def consume_progress(self, line: str) -> bool:
        event = progress.parse_event(line)
        if event is None:
            return False
        with self._lock:
            self._progress[event["track"]] = event
        return True

    def append(self, line: str) -> None:
        with self._lock:
            self.log.append(line)
            if len(self.log) > MAX_LOG_LINES:
                excess = len(self.log) - MAX_LOG_LINES
                del self.log[:excess]
                # Track dropped lines so absolute offsets stay monotonic even
                # after trimming; otherwise a client cursor silently drifts.
                self._dropped += excess

    def tail(self, offset: int = 0) -> dict[str, Any]:
        """Return lines after `offset`, an absolute line counter."""
        with self._lock:
            total_abs = self._dropped + len(self.log)
            start_abs = max(self._dropped, min(offset, total_abs))
            start = start_abs - self._dropped
            return {
                "offset": total_abs,
                "total": total_abs,
                "dropped": self._dropped,
                # True when the client's cursor fell off the retained window.
                "truncated": offset < self._dropped,
                "lines": self.log[start:],
            }

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            total = self._dropped + len(self.log)
            start = self.started_at or self.created_at
            elapsed = max(0, int((self.ended_at or time.time()) - start))
            current_progress = progress.snapshot(self.kind, self.status, self._progress, elapsed, self._cancel.is_set())
        return {
            "id": self.id,
            "kind": self.kind,
            "label": TASK_KINDS.get(self.kind, {}).get("label", self.kind),
            "params": self.params,
            "status": self.status,
            "returncode": self.returncode,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "ended_at": self.ended_at,
            "error": self.error,
            "log_total": total,
            "cancel_requested": self._cancel.is_set(),
            "progress": current_progress,
        }


# --------------------------------------------------------------------------
# Argument building (whitelist)
# --------------------------------------------------------------------------


def _s(params: dict[str, Any], key: str) -> str:
    value = params.get(key, "")
    return str(value).strip() if value is not None else ""


def build_argv(kind: str, params: dict[str, Any]) -> list[str]:
    """Translate a validated parameter dict into CLI arguments."""
    spec = TASK_KINDS.get(kind)
    if spec is None:
        raise ValueError(f"未知任务类型: {kind}")

    argv: list[str] = list(spec["cli"])
    materials = params.get("materials") or []
    if not isinstance(materials, list) or any(not isinstance(item, str) for item in materials):
        raise ValueError("补充材料必须是路径字符串列表")

    if kind in ("transcribe", "slide", "note", "add"):
        course_id, sub_id = _s(params, "course_id"), _s(params, "sub_id")
        if not course_id or not sub_id:
            raise ValueError("必须同时提供课程 ID 与节次 ID")
        argv += ["--course-id", course_id, "--sub-id", sub_id]
        argv.append("--no-workspace-prompt")
    elif kind == "batch":
        course_id = _s(params, "course_id")
        if not course_id:
            raise ValueError("批量转写必须提供课程 ID")
        argv += ["--course-id", course_id, "--no-workspace-prompt"]
        retries = params.get("max_retries")
        if retries not in (None, ""):
            argv += ["--max-retries", str(max(1, min(10, int(retries))))]

    if kind in ("transcribe", "slide", "note", "add", "batch") and params.get("force_login"):
        argv.append("--force-login")
    if kind in ("transcribe", "note", "batch") and params.get("force_transcribe"):
        argv.append("--force-transcribe")
    if kind in ("slide", "note"):
        for key, flag, low, high in (("limit", "--max-items", 0, 5000), ("concurrency", "--concurrency", 1, 16),
                                      ("retries", "--retries", 1, 8), ("timeout", "--timeout", 5, 300)):
            value = params.get(key)
            if value not in (None, ""):
                argv += [flag, str(max(low, min(high, int(value))))]
    if kind == "batch" and params.get("retry_failed"):
        argv.append("--retry-failed")
    if kind == "note":
        style = _s(params, "note_style") or "standard"
        if style not in ("standard", "dialogue"):
            raise ValueError("笔记风格只能是 standard 或 dialogue")
        argv += ["--note-style", style, "--no-material-prompt"]
        if params.get("no_slide"):
            argv.append("--no-slide")
        for material in params.get("materials") or []:
            name, _, path = str(material).partition("=")
            if path:
                argv += ["--material", f"{name}={path}"]
            elif name:
                argv += ["--material", name]
    elif kind == "add":
        argv.append("--no-material-prompt")
        for material in params.get("materials") or []:
            name, _, path = str(material).partition("=")
            if path:
                argv += ["--material", f"{name}={path}"]
            elif name:
                argv += ["--material", name]
    elif kind == "cheatsheet":
        fmt = _s(params, "format") or "html"
        if fmt not in ("tex", "html"):
            raise ValueError("速查表格式只能是 tex 或 html")
        argv += ["--format", fmt]
        output = _s(params, "output")
        if output:
            argv += ["--output", output]
    elif kind == "serve":
        port = params.get("port")
        if port not in (None, ""):
            argv += ["--port", str(max(1, min(65535, int(port))))]

    if kind in ("index", "build", "serve"):
        argv.append("--no-workspace-prompt")

    return argv


# --------------------------------------------------------------------------
# Process control
# --------------------------------------------------------------------------


def _kill_tree(proc: subprocess.Popen) -> None:
    """Kept as a thin alias so call sites read naturally."""
    kill_process_tree(proc)


class TaskManager:
    def __init__(self) -> None:
        self._tasks: dict[str, Task] = {}
        self._order: deque[str] = deque()
        self._lock = threading.Lock()
        self._runtimes: dict[str, Path] = {}
        self._queue_condition = threading.Condition()
        self._pending_jobs: deque[str] = deque()
        self._active_job: str | None = None
        self._threads: dict[str, threading.Thread] = {}
        self._closing = threading.Event()

    # -- lifecycle ---------------------------------------------------------

    def start(self, kind: str, params: dict[str, Any]) -> Task:
        if self._closing.is_set():
            raise ValueError("控制台正在停止，请重新启动后再创建任务")
        if kind == "serve":
            from .start import pick_port
            params = {**params, "port": pick_port(int(params["port"]) if params.get("port") else None)}
        argv = build_argv(kind, params)
        task = Task(
            id=uuid.uuid4().hex[:12],
            kind=kind,
            params={k: v for k, v in params.items() if k in {
                "course_id", "sub_id", "note_style", "materials", "no_slide",
                "force_login", "force_transcribe", "max_retries", "retry_failed", "limit", "concurrency", "retries", "timeout", "format", "output", "port"}},
            argv=argv,
        )
        env_extra: dict[str, str] = {}
        cancel_path = None
        if kind == "batch":
            cancel_path = REPO_ROOT / "state" / f"batch_cancel_{task.id}.flag"
            cancel_path.parent.mkdir(parents=True, exist_ok=True)
            if cancel_path.exists():
                cancel_path.unlink()
            env_extra["LOOK_TONGJI_BATCH_CANCEL_FILE"] = str(cancel_path)

        thread = threading.Thread(
            target=self._run, args=(task, env_extra), daemon=True, name=f"task-{task.id}"
        )
        with self._lock:
            if self._closing.is_set():
                raise ValueError("控制台正在停止")
            self._tasks[task.id] = task
            self._order.append(task.id)
            self._threads[task.id] = thread
            if cancel_path is not None:
                self._runtimes[task.id] = cancel_path
            if kind not in LONG_RUNNING:
                with self._queue_condition:
                    self._pending_jobs.append(task.id)
                    self._queue_condition.notify_all()
            try:
                thread.start()
            except RuntimeError as exc:
                self._tasks.pop(task.id, None)
                self._order.remove(task.id)
                self._threads.pop(task.id, None)
                self.cleanup(task.id)
                with self._queue_condition:
                    if task.id in self._pending_jobs:
                        self._pending_jobs.remove(task.id)
                    self._queue_condition.notify_all()
                raise ValueError("无法启动后台任务线程") from exc
        return task

    def _run(self, task: Task, env_extra: dict[str, str]) -> None:
        # Workspace writes are serialized; preview servers use their own lane.
        acquired = False
        try:
            if task.kind not in LONG_RUNNING:
                with self._queue_condition:
                    while (not task._cancel.is_set() and
                           (self._active_job is not None or self._pending_jobs[0] != task.id)):
                        self._queue_condition.wait(timeout=.2)
                    self._pending_jobs.remove(task.id)
                    if not task._cancel.is_set():
                        self._active_job = task.id
                        acquired = True
                    self._queue_condition.notify_all()
            if task._cancel.is_set():
                task.status = "cancelled"
                task.returncode = 130
                task.ended_at = time.time()
                task.append("[console] 排队任务已取消")
                self.cleanup(task.id)
                return
            self._execute(task, env_extra)
        finally:
            if acquired:
                with self._queue_condition:
                    self._active_job = None
                    self._queue_condition.notify_all()

    def _execute(self, task: Task, env_extra: dict[str, str]) -> None:
        task.status = "running"
        task.started_at = time.time()
        if task.kind in ("transcribe", "slide", "batch", "note"):
            tracks = ["transcript", "slides"] if task.kind == "note" else [{"transcribe": "transcript", "slide": "slides", "batch": "batch"}[task.kind]]
            for track in tracks:
                task.consume_progress(progress.PREFIX + json.dumps({"track": track, "stage": "auth"}))
        if task.kind in ("index", "build", "serve", "add", "cheatsheet"):
            task.consume_progress(progress.PREFIX + json.dumps({"track": "workspace", "stage": {"add": "import", "cheatsheet": "prepare"}.get(task.kind, task.kind)}))
        task.append(f"$ {' '.join(self._display_argv(task.argv))}")
        env_values = self._secret_values()

        try:
            proc = subprocess.Popen(
                base_command() + task.argv,
                cwd=str(REPO_ROOT),
                env=cli_env(env_extra),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                # Give the child its own process group/session so a cancel can
                # target its whole tree without touching the console.
                **child_popen_kwargs(),
            )
        except Exception as exc:  # pragma: no cover - spawn failure
            task.status = "failed"
            task.error = f"无法启动任务: {exc}"
            task.append(task.error)
            task.ended_at = time.time()
            self.cleanup(task.id)
            return

        task._proc = proc
        if task._cancel.is_set() or self._closing.is_set():
            _kill_tree(proc)
        try:
            assert proc.stdout is not None
            for raw in proc.stdout:
                line = redact_env_values(redact(raw.rstrip("\r\n")), env_values)
                if line and not task.consume_progress(line):
                    task.append(line)
            proc.wait()
        except Exception as exc:
            task.append(f"[console] 读取输出失败: {exc}")
        finally:
            if proc.stdout is not None:
                proc.stdout.close()
            if proc.poll() is None:
                _kill_tree(proc)
            task.returncode = proc.returncode
            task.ended_at = time.time()
            if task._cancel.is_set():
                task.status = "cancelled"
                task.append("[console] 任务已取消")
            elif proc.returncode == 0:
                task.status = "succeeded"
            else:
                task.status = "failed"
                if not task.error:
                    task.error = f"进程退出码 {proc.returncode}"
            # Remove the per-run cancel flag so `state/` does not accumulate.
            self.cleanup(task.id)

    @staticmethod
    def _secret_values() -> list[str]:
        """Literal secret strings that must never reach the log.

        Only true secrets belong here. The username is deliberately excluded:
        it is already visible in the settings UI, and masking it would make the
        logs much harder to use when diagnosing a login failure.
        """
        return secret_values()

    @staticmethod
    def _display_argv(argv: Iterable[str]) -> list[str]:
        """Quote args for display only. No secrets ever reach argv."""
        out = []
        for arg in argv:
            if any(ch.isspace() for ch in arg) or '"' in arg:
                out.append('"' + arg.replace('"', '\\"') + '"')
            else:
                out.append(arg)
        return out

    # -- queries -----------------------------------------------------------

    def get(self, task_id: str) -> Task | None:
        with self._lock:
            return self._tasks.get(task_id)

    def list(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            ids = list(self._order)[-limit:]
            with self._queue_condition:
                positions = {task_id: index + 1 for index, task_id in enumerate(self._pending_jobs)}
            return [{**self._tasks[i].snapshot(), "queue_position": positions.get(i)} for i in reversed(ids) if i in self._tasks]

    # -- cancel ------------------------------------------------------------

    def cancel(self, task_id: str) -> dict[str, Any]:
        task = self.get(task_id)
        if task is None:
            raise KeyError(task_id)
        if task.status not in ("queued", "running"):
            return {"id": task_id, "status": task.status, "message": "任务已结束"}

        task._cancel.set()
        with self._queue_condition:
            self._queue_condition.notify_all()

        # Batch transcription polls this flag between lectures, which lets it
        # persist `batch_state.json` and stay resumable.
        runtime = self._runtimes.get(task_id)
        if runtime is not None:
            try:
                runtime.write_text("cancel", encoding="utf-8")
            except OSError:
                pass
            task.append("[console] 已请求取消，等待当前节次收尾...")
            return {"id": task_id, "status": "cancelling", "message": "已请求取消"}

        proc = task._proc
        if proc is not None:
            task.append("[console] 正在终止进程树...")
            _kill_tree(proc)
        return {"id": task_id, "status": "cancelling", "message": "已终止"}

    def cleanup(self, task_id: str) -> None:
        runtime = self._runtimes.pop(task_id, None)
        if runtime is not None and runtime.exists():
            try:
                runtime.unlink()
            except OSError:
                pass

    def shutdown(self) -> None:
        """Cancel queued jobs and terminate all children when the server stops."""
        self._closing.set()
        with self._lock:
            tasks = list(self._tasks.values())
        for task in tasks:
            if task.status in ("queued", "running"):
                self.cancel(task.id)
        for task in tasks:
            if task._proc is not None and task._proc.poll() is None:
                _kill_tree(task._proc)
        deadline = time.monotonic() + 5
        for thread in list(self._threads.values()):
            thread.join(max(0, deadline - time.monotonic()))
