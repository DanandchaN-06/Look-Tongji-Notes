"""Bounded, resource-aware scheduling without one blocked thread per job."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import threading
from typing import Callable


@dataclass
class ScheduledJob:
    id: str
    lane: str
    lecture_key: str | None
    parallelism: int
    parallel_group: str | None
    exclusive: bool
    callback: Callable[[], None]
    on_cancel: Callable[[], None]
    on_error: Callable[[Exception], None]


class ResourceScheduler:
    """FIFO with safe bypass for jobs whose current resource is occupied."""

    DEFAULT_LIMITS = {"asr": 1, "download": 2, "write": 1, "build": 1, "legacy": 1, "preview": 1}

    def __init__(self, *, workers: int = 4, lane_limits: dict[str, int] | None = None) -> None:
        self.workers = max(1, min(4, int(workers)))
        self.lane_limits = {**self.DEFAULT_LIMITS, **(lane_limits or {})}
        self._condition = threading.Condition()
        self._pending: deque[ScheduledJob] = deque()
        self._active: dict[str, ScheduledJob] = {}
        self._threads: list[threading.Thread] = []
        self._closing = False

    def submit(self, job: ScheduledJob) -> None:
        with self._condition:
            if self._closing:
                raise RuntimeError("任务调度器正在停止")
            if job.id in self._active or any(item.id == job.id for item in self._pending):
                raise ValueError("调度任务 ID 已存在")
            self._ensure_workers_locked()
            self._pending.append(job)
            self._condition.notify_all()

    def _ensure_workers_locked(self) -> None:
        while len(self._threads) < self.workers:
            thread = threading.Thread(target=self._worker, name=f"workflow-scheduler-{len(self._threads) + 1}", daemon=True)
            self._threads.append(thread)
            try:
                thread.start()
            except RuntimeError:
                self._threads.remove(thread)
                raise

    def _eligible(self, job: ScheduledJob) -> bool:
        if job.exclusive and self._active:
            return False
        if any(active.exclusive for active in self._active.values()):
            return False
        if job.parallel_group and sum(active.parallel_group == job.parallel_group for active in self._active.values()) >= job.parallelism:
            return False
        if sum(active.lane == job.lane for active in self._active.values()) >= self.lane_limits.get(job.lane, 1):
            return False
        if job.lecture_key and any(active.lecture_key == job.lecture_key for active in self._active.values()):
            return False
        return True

    def _take_next_locked(self) -> ScheduledJob | None:
        for index, job in enumerate(self._pending):
            if self._eligible(job):
                del self._pending[index]
                self._active[job.id] = job
                return job
        return None

    def _worker(self) -> None:
        while True:
            with self._condition:
                job = None
                while job is None:
                    if self._closing and not self._pending:
                        return
                    job = self._take_next_locked()
                    if job is None:
                        self._condition.wait()
            try:
                job.callback()
            except Exception as error:
                try:
                    job.on_error(error)
                except Exception:
                    pass
            finally:
                with self._condition:
                    self._active.pop(job.id, None)
                    self._condition.notify_all()

    def cancel(self, job_id: str) -> bool:
        with self._condition:
            job = next((item for item in self._pending if item.id == job_id), None)
            if job is None:
                return False
            self._pending.remove(job)
            self._condition.notify_all()
        job.on_cancel()
        return True

    def queue_positions(self) -> dict[str, int]:
        with self._condition:
            return {job.id: position for position, job in enumerate(self._pending, 1)}

    def shutdown(self, *, cancel_pending: bool = True) -> None:
        with self._condition:
            self._closing = True
            pending = list(self._pending) if cancel_pending else []
            if cancel_pending:
                self._pending.clear()
            self._condition.notify_all()
        for job in pending:
            job.on_cancel()
        for thread in list(self._threads):
            if thread is not threading.current_thread():
                thread.join(timeout=5)
