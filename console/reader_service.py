"""Coalesced, crash-recoverable builds for the local reading site."""

from __future__ import annotations

import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Callable

from .storage import ConsoleStorage


Build = Callable[[Path], None]


class ReaderBuildService:
    def __init__(
        self,
        storage: ConsoleStorage,
        workspace_root: str | Path,
        *,
        build: Build | None = None,
        auto_start: bool = True,
    ) -> None:
        self.storage = storage
        self.workspace_root = Path(workspace_root).expanduser().resolve()
        self.site_dir = self.workspace_root / "site"
        self.build = build or self._build_workspace
        self._condition = threading.Condition()
        self._stopping = False
        self._thread: threading.Thread | None = None
        state = self.storage.get_reader_build_state()
        if state["state"] == "building":
            state = self.storage.set_reader_build_state(state="queued")
        if auto_start and state["requested_generation"] > state["published_generation"]:
            self.start()

    def _build_workspace(self, target: Path) -> None:
        from scripts.tongji_backend.workspace import build_workspace_wiki, load_workspace_config

        config = load_workspace_config()
        if config is None or config.workspace_root.resolve() != self.workspace_root:
            raise RuntimeError("阅读资料库配置已变化；请重新打开控制台后重试构建。")
        build_workspace_wiki(config, out_dir=target)

    def start(self) -> None:
        with self._condition:
            if self._thread and self._thread.is_alive():
                return
            self._stopping = False
            self._thread = threading.Thread(target=self._run, name="reader-build", daemon=True)
            self._thread.start()

    def request_build(self) -> dict:
        state = self.storage.request_reader_build()
        self.start()
        with self._condition:
            self._condition.notify_all()
        return state

    def status(self) -> dict:
        return self.storage.get_reader_build_state()

    def open_reader(self) -> dict:
        state = self.status()
        if (self.site_dir / "index.html").is_file():
            return {**state, "ready": True, "url_path": "/reader/"}
        if state["state"] not in {"queued", "building"}:
            state = self.request_build()
        return {**state, "ready": False, "url_path": ""}

    def wait_for_generation(self, generation: int, *, timeout: float = 5.0) -> bool:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            state = self.status()
            if state["published_generation"] >= generation:
                return True
            if state["state"] == "failed" and state["requested_generation"] <= generation:
                return False
            with self._condition:
                self._condition.wait(min(0.05, max(0, deadline - time.monotonic())))
        return self.status()["published_generation"] >= generation

    def wait_for_state(self, states: set[str], *, timeout: float = 5.0) -> bool:
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.status()["state"] in states:
                return True
            with self._condition:
                self._condition.wait(min(0.05, max(0, deadline - time.monotonic())))
        return self.status()["state"] in states

    def shutdown(self) -> None:
        with self._condition:
            self._stopping = True
            self._condition.notify_all()
            thread = self._thread
        if thread:
            thread.join(timeout=3)

    def _run(self) -> None:
        try:
            while True:
                with self._condition:
                    while not self._stopping:
                        state = self.storage.get_reader_build_state()
                        self.storage.close_thread_connection()
                        if state["requested_generation"] > state["published_generation"]:
                            break
                        self._condition.wait()
                    if self._stopping:
                        return
                generation = int(state["requested_generation"])
                self.storage.set_reader_build_state(state="building", last_error="")
                try:
                    self._build_and_publish()
                except Exception as error:
                    latest = self.storage.get_reader_build_state()
                    changed_while_building = latest["requested_generation"] > generation
                    self.storage.set_reader_build_state(
                        state="queued" if changed_while_building else "failed",
                        last_error=str(error)[:1000],
                    )
                else:
                    latest = self.storage.get_reader_build_state()
                    changed_while_building = latest["requested_generation"] > generation
                    self.storage.set_reader_build_state(
                        published_generation=generation,
                        state="queued" if changed_while_building else "ready",
                        last_error="",
                        site_url="/",
                    )
                with self._condition:
                    self._condition.notify_all()
                self.storage.close_thread_connection()
        finally:
            self.storage.close_thread_connection()

    def _build_and_publish(self) -> None:
        from scripts.tongji_backend.workspace import _workspace_mutation_lock

        with _workspace_mutation_lock(self.workspace_root):
            self._build_and_publish_locked()

    def _build_and_publish_locked(self) -> None:
        self.workspace_root.mkdir(parents=True, exist_ok=True)
        temporary = self.workspace_root / f".site-build-{uuid.uuid4().hex}"
        backup = self.workspace_root / f".site-backup-{uuid.uuid4().hex}"
        moved_old = False
        try:
            self.build(temporary)
            index = temporary / "index.html"
            if not index.is_file() or index.stat().st_size == 0:
                raise RuntimeError("阅读站点构建未生成有效的 index.html")
            if self.site_dir.exists():
                os.replace(self.site_dir, backup)
                moved_old = True
            try:
                os.replace(temporary, self.site_dir)
            except BaseException:
                if moved_old and backup.exists() and not self.site_dir.exists():
                    os.replace(backup, self.site_dir)
                    moved_old = False
                raise
            if moved_old:
                shutil.rmtree(backup, ignore_errors=True)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
