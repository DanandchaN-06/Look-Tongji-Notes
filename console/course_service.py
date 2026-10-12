"""Account-scoped course cache and explicit platform synchronization."""

from __future__ import annotations

import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Protocol

from .core import extract_json, run_cli
from .contracts import CourseRecord, LectureRecord
from .storage import ConsoleStorage, class_date_sort_key, stable_account_id


class CourseServiceError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        retryable: bool = False,
        status: int = 502,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.status = status


class CourseAdapter(Protocol):
    def list_courses(self) -> list[dict[str, Any]]: ...

    def list_lectures(self, course_id: str) -> list[dict[str, Any]]: ...


class CliCourseAdapter:
    """Machine-readable adapter over the existing CLI, preserving its login flow."""

    def list_courses(self) -> list[dict[str, Any]]:
        result = run_cli(["list", "--json", "--no-prompt", "--all"], timeout=300)
        payload = extract_json(result.stdout)
        if result.returncode != 0 or not payload or payload.get("ok") is not True:
            raise CourseServiceError(
                "COURSE_SYNC_FAILED", "未能从平台读取课程，请检查账号设置和网络。", retryable=True
            )
        courses = payload.get("courses")
        if not isinstance(courses, list):
            raise CourseServiceError("COURSE_SYNC_FAILED", "平台返回的课程数据格式无效。")
        return [course for course in courses if isinstance(course, dict)]

    def list_lectures(self, course_id: str) -> list[dict[str, Any]]:
        result = run_cli(
            ["lectures", "--json", "--course-id", course_id], timeout=180
        )
        payload = extract_json(result.stdout)
        if result.returncode != 0 or not payload or payload.get("ok") is not True:
            raise CourseServiceError(
                "COURSE_SYNC_FAILED", "未能读取某门课程的课次，缓存保持不变。", retryable=True
            )
        lectures = payload.get("lectures")
        if not isinstance(lectures, list):
            raise CourseServiceError("COURSE_SYNC_FAILED", "平台返回的课次数据格式无效。")
        return [lecture for lecture in lectures if isinstance(lecture, dict)]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class CourseService:
    def __init__(
        self,
        storage: ConsoleStorage,
        *,
        adapter: CourseAdapter | None = None,
        account_identity: Callable[[], tuple[str, str] | None],
        workspace_root: Callable[[], Path | None],
    ) -> None:
        self.storage = storage
        self.adapter = adapter or CliCourseAdapter()
        self.account_identity = account_identity
        self.workspace_root = workspace_root
        self.local_account_id = stable_account_id("local", "workspace")

    def list_courses(self, *, source: str = "local", query: str = "") -> dict[str, Any]:
        source = source.strip().lower()
        query = query.strip()
        if source not in {"local", "all", "platform"}:
            raise ValueError("source must be local, all, or platform")
        if len(query) > 200:
            raise ValueError("course search query is too long")

        account_ids = [self.local_account_id]
        platform_account = self._ensure_platform_account()
        if platform_account and platform_account.id != self.local_account_id:
            account_ids.append(platform_account.id)

        courses = []
        latest_class_date: dict[str, tuple[int, int, int, int, int, int, int, int, str]] = {}
        for account_id in account_ids:
            account_source = "local" if account_id == self.local_account_id else "platform"
            if source not in {"all", account_source}:
                continue
            for course in self.storage.list_courses(account_id, source=account_source, query=query):
                item = asdict(course)
                lectures = self.storage.list_lectures(course.id)
                item["lecture_count"] = len(lectures)
                latest_class_date[course.id] = max(
                    (class_date_sort_key(lecture.class_date) for lecture in lectures),
                    default=class_date_sort_key(None),
                )
                courses.append(item)
        courses.sort(key=lambda item: (item["title"].casefold(), item["id"]))
        courses.sort(
            key=lambda item: (
                latest_class_date[item["id"]][0] >= 0,
                latest_class_date[item["id"]],
            ),
            reverse=True,
        )

        sync_status = None
        if platform_account:
            status = self.storage.get_course_sync_status(platform_account.id)
            sync_status = asdict(status) if status else None
        return {"source": source, "query": query, "courses": courses, "sync_status": sync_status}

    def list_lectures(self, course_id: str) -> dict[str, Any]:
        course, lectures = self.get_course_lectures(course_id)
        return {
            "course": asdict(course),
            "lectures": [asdict(lecture) for lecture in lectures],
        }

    def get_course_lectures(self, course_id: str) -> tuple[CourseRecord, list[LectureRecord]]:
        course = self.storage.get_course(course_id, self.local_account_id)
        if course is None:
            account = self._ensure_platform_account()
            if account:
                course = self.storage.get_course(course_id, account.id)
        if course is None:
            raise CourseServiceError("NOT_FOUND", "没有找到这门课程。", status=404)
        return course, self.storage.list_lectures(course_id)

    def find_local_lecture(
        self, platform_course_id: str, platform_sub_id: str
    ) -> tuple[CourseRecord, LectureRecord] | None:
        for course in self.storage.list_courses(self.local_account_id, source="local"):
            if course.platform_course_id != platform_course_id:
                continue
            lecture = next(
                (item for item in self.storage.list_lectures(course.id) if item.platform_sub_id == platform_sub_id),
                None,
            )
            if lecture:
                return course, lecture
        return None

    def get_owned_lecture(self, lecture_id: str) -> tuple[CourseRecord, LectureRecord]:
        lecture = self.storage.get_lecture(lecture_id)
        if lecture is None:
            raise CourseServiceError("NOT_FOUND", "没有找到这个课次。", status=404)
        course = self.storage.get_course(lecture.course_id, self.local_account_id)
        if course is None:
            account = self._ensure_platform_account()
            if account:
                course = self.storage.get_course(lecture.course_id, account.id)
        if course is None:
            raise CourseServiceError("NOT_FOUND", "没有找到这个课次。", status=404)
        return course, lecture

    def sync_courses(self) -> dict[str, Any]:
        identity = self.account_identity()
        if identity is None or not identity[0].strip() or not identity[1].strip():
            raise CourseServiceError(
                "AUTH_REQUIRED", "请先在设置中配置同济账号。", status=401
            )
        provider, platform_account_id = identity
        account = self.storage.upsert_account(
            provider=provider, platform_account_id=platform_account_id
        )
        started_at = _now()
        self.storage.set_course_sync_status(account.id, "syncing")
        try:
            normalized = self._fetch_platform_snapshot()
            with self.storage.transaction():
                for course, lectures in normalized:
                    saved_course = self.storage.upsert_course(
                        account_id=account.id,
                        platform_course_id=course["course_id"],
                        title=course["title"],
                        teacher=course["teacher"],
                        term=course["term"],
                        source="platform",
                        synced_at=_now(),
                    )
                    for lecture in lectures:
                        self.storage.upsert_lecture(
                            course_id=saved_course.id,
                            platform_sub_id=lecture["sub_id"],
                            title=lecture["title"],
                            class_date=lecture["class_date"],
                            playback_available=lecture["playback_available"],
                        )
            status = self.storage.set_course_sync_status(account.id, "succeeded")
        except CourseServiceError as error:
            safe_message = (
                "请先在设置中检查同济账号配置。"
                if error.code == "AUTH_REQUIRED"
                else "课程同步失败，原有缓存已保留。"
            )
            self.storage.set_course_sync_status(
                account.id,
                "failed",
                error={"code": error.code, "message": safe_message},
            )
            raise CourseServiceError(
                error.code,
                safe_message,
                retryable=error.retryable,
                status=error.status,
            ) from error
        except Exception as error:
            safe_message = "课程同步失败，原有缓存已保留。"
            if isinstance(error, (OSError, TimeoutError)):
                safe_message = "课程同步遇到临时网络或文件错误，原有缓存已保留。"
            self.storage.set_course_sync_status(
                account.id,
                "failed",
                error={"code": "COURSE_SYNC_FAILED", "message": safe_message},
            )
            raise CourseServiceError(
                "COURSE_SYNC_FAILED", safe_message, retryable=True
            ) from error

        ended_at = _now()
        return {
            "sync_job": {
                "id": str(uuid.uuid4()),
                "state": "succeeded",
                "started_at": started_at,
                "ended_at": ended_at,
                "course_count": len(normalized),
                "lecture_count": sum(len(lectures) for _, lectures in normalized),
            },
            "cache": asdict(status),
        }

    def _fetch_platform_snapshot(self) -> list[tuple[dict[str, Any], list[dict[str, Any]]]]:
        courses = self.adapter.list_courses()
        snapshot = []
        seen_courses: set[str] = set()
        for source_course in courses:
            course_id = str(source_course.get("course_id") or "").strip()
            title = str(source_course.get("title") or "").strip()
            if not course_id or not title or course_id in seen_courses:
                continue
            seen_courses.add(course_id)
            source_lectures = self.adapter.list_lectures(course_id)
            lectures = []
            seen_lectures: set[str] = set()
            for source_lecture in source_lectures:
                sub_id = str(source_lecture.get("sub_id") or "").strip()
                if not sub_id or sub_id in seen_lectures:
                    continue
                seen_lectures.add(sub_id)
                lectures.append(
                    {
                        "sub_id": sub_id,
                        "title": str(source_lecture.get("sub_title") or "").strip() or sub_id,
                        "class_date": str(source_lecture.get("date") or "").strip() or None,
                        "playback_available": source_lecture.get("has_playback") is True,
                    }
                )
            snapshot.append(
                (
                    {
                        "course_id": course_id,
                        "title": title,
                        "teacher": str(source_course.get("teacher") or "").strip() or None,
                        "term": str(source_course.get("term") or "").strip() or None,
                    },
                    lectures,
                )
            )
        return snapshot

    def _ensure_platform_account(self):
        identity = self.account_identity()
        if identity is None or not identity[0].strip() or not identity[1].strip():
            return None
        account_id = stable_account_id(identity[0], identity[1])
        return self.storage.get_account(account_id)
