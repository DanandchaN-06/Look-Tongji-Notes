"""Typed contracts shared by console storage and future V2 services."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


WORKFLOW_STATES = frozenset(
    {
        "queued",
        "running",
        "waiting_user",
        "waiting_agent",
        "blocked",
        "succeeded",
        "failed",
        "cancelled",
        "interrupted",
    }
)
STEP_STATES = frozenset(
    {
        "pending",
        "queued",
        "running",
        "succeeded",
        "skipped",
        "waiting_user",
        "waiting_agent",
        "blocked",
        "failed",
        "cancelled",
        "interrupted",
    }
)
ATTEMPT_STATES = frozenset(
    {"running", "succeeded", "failed", "cancelled", "interrupted"}
)

_SECRET_SETTING_KEYS = frozenset(
    {
        "password",
        "secret",
        "api_key",
        "api-key",
        "apikey",
        "api_token",
        "api-token",
        "auth_token",
        "auth-token",
        "cookie",
        "jwt",
        "authorization",
        "token",
        "session_id",
        "session-id",
        "signature",
        "access_token",
        "access-token",
        "refresh_token",
        "refresh-token",
        "signed_url",
        "signed-url",
    }
)


def validate_state(value: str, allowed: frozenset[str], label: str) -> str:
    if value not in allowed:
        raise ValueError(f"invalid {label} state: {value!r}")
    return value


def public_object_json(value: dict[str, Any], *, label: str) -> str:
    """Serialize public metadata while rejecting obvious credential fields."""
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")

    def check(item: Any) -> None:
        if isinstance(item, dict):
            for key, child in item.items():
                normalized = str(key).strip().lower().replace(" ", "_")
                if normalized in _SECRET_SETTING_KEYS:
                    raise ValueError("secret fields cannot be stored in public metadata")
                check(child)
        elif isinstance(item, list):
            for child in item:
                check(child)

    check(value)
    import json

    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def public_settings_json(value: dict[str, Any]) -> str:
    return public_object_json(value, label="settings revision")


@dataclass(frozen=True)
class AccountRecord:
    id: str
    provider: str
    created_at: str
    course_sync_state: str
    course_sync_started_at: str | None
    course_sync_last_success_at: str | None
    course_sync_error: dict[str, Any]


@dataclass(frozen=True)
class CourseRecord:
    id: str
    account_id: str
    platform_course_id: str | None
    identity_kind: str
    import_key: str | None
    title: str
    teacher: str | None
    term: str | None
    source: str
    synced_at: str | None


@dataclass(frozen=True)
class LectureRecord:
    id: str
    course_id: str
    platform_sub_id: str | None
    identity_kind: str
    import_key: str | None
    title: str
    class_date: str | None
    playback_available: bool
    manifest_path: str | None


@dataclass(frozen=True)
class SettingsRevisionRecord:
    id: str
    public_settings: dict[str, Any]
    created_at: str


@dataclass(frozen=True)
class CourseSyncStatus:
    state: str
    started_at: str | None
    last_success_at: str | None
    error: dict[str, Any]


@dataclass(frozen=True)
class AttemptRecord:
    id: str
    step_id: str
    number: int
    state: str
    started_at: str | None
    ended_at: str | None
    exit_code: int | None
    progress: dict[str, Any]
    log_path: str | None
    runner_task_id: str | None = None
    process_pid: int | None = None
    error_code: str | None = None
    error: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepRecord:
    id: str
    workflow_id: str
    name: str
    ordinal: int
    state: str
    artifact: dict[str, Any]
    error_code: str | None
    error: dict[str, Any]
    attempts: list[AttemptRecord] = field(default_factory=list)


@dataclass(frozen=True)
class WorkflowRecord:
    id: str
    lecture_id: str
    operation: str
    state: str
    settings_revision_id: str | None
    target: dict[str, Any]
    created_at: str
    updated_at: str
    cancel_requested: bool
    steps: list[StepRecord] = field(default_factory=list)


@dataclass(frozen=True)
class ArtifactRecord:
    id: str
    lecture_id: str
    kind: str
    path: str
    validation: dict[str, Any]
    producer_attempt_id: str | None
    updated_at: str


@dataclass(frozen=True)
class LogSlice:
    offset: int
    next_offset: int
    size: int
    text: str
    truncated: bool
