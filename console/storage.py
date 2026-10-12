"""SQLite persistence for console courses, settings revisions, and workflows."""

from __future__ import annotations

import codecs
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .contracts import (
    ATTEMPT_STATES,
    STEP_STATES,
    WORKFLOW_STATES,
    AccountRecord,
    ArtifactRecord,
    AttemptRecord,
    CourseRecord,
    CourseSyncStatus,
    LectureRecord,
    LogSlice,
    SettingsRevisionRecord,
    StepRecord,
    WorkflowRecord,
    public_object_json,
    public_settings_json,
    validate_state,
)


CURRENT_SCHEMA_VERSION = 5
_MAX_LOG_BYTES = 1024 * 1024


class IdempotencyConflict(ValueError):
    pass

_MIGRATIONS: dict[int, tuple[str, ...]] = {
    1: (
        """CREATE TABLE IF NOT EXISTS accounts (
            id TEXT PRIMARY KEY,
            provider TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS courses (
            id TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            platform_course_id TEXT,
            title TEXT NOT NULL,
            teacher TEXT,
            term TEXT,
            source TEXT NOT NULL,
            synced_at TEXT,
            UNIQUE(account_id, platform_course_id)
        )""",
        """CREATE TABLE IF NOT EXISTS lectures (
            id TEXT PRIMARY KEY,
            course_id TEXT NOT NULL REFERENCES courses(id),
            platform_sub_id TEXT NOT NULL,
            title TEXT NOT NULL,
            class_date TEXT,
            playback_available INTEGER NOT NULL DEFAULT 0,
            manifest_path TEXT,
            UNIQUE(course_id, platform_sub_id)
        )""",
        """CREATE TABLE IF NOT EXISTS settings_revisions (
            id TEXT PRIMARY KEY,
            public_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS workflows (
            id TEXT PRIMARY KEY,
            lecture_id TEXT NOT NULL REFERENCES lectures(id),
            operation TEXT NOT NULL,
            state TEXT NOT NULL,
            settings_revision_id TEXT REFERENCES settings_revisions(id),
            target_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            cancel_requested INTEGER NOT NULL DEFAULT 0
        )""",
        """CREATE TABLE IF NOT EXISTS steps (
            id TEXT PRIMARY KEY,
            workflow_id TEXT NOT NULL REFERENCES workflows(id) ON DELETE CASCADE,
            name TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            state TEXT NOT NULL,
            artifact_json TEXT NOT NULL DEFAULT '{}',
            error_code TEXT,
            error_json TEXT NOT NULL DEFAULT '{}',
            UNIQUE(workflow_id, name)
        )""",
        """CREATE TABLE IF NOT EXISTS attempts (
            id TEXT PRIMARY KEY,
            step_id TEXT NOT NULL REFERENCES steps(id) ON DELETE CASCADE,
            number INTEGER NOT NULL,
            state TEXT NOT NULL,
            started_at TEXT,
            ended_at TEXT,
            exit_code INTEGER,
            progress_json TEXT NOT NULL DEFAULT '{}',
            log_path TEXT,
            UNIQUE(step_id, number)
        )""",
        """CREATE TABLE IF NOT EXISTS artifacts (
            id TEXT PRIMARY KEY,
            lecture_id TEXT NOT NULL REFERENCES lectures(id),
            kind TEXT NOT NULL,
            path TEXT NOT NULL,
            validation_json TEXT NOT NULL DEFAULT '{}',
            producer_attempt_id TEXT REFERENCES attempts(id),
            updated_at TEXT NOT NULL,
            UNIQUE(lecture_id, kind, path)
        )""",
        """CREATE TABLE IF NOT EXISTS idempotency_keys (
            key TEXT PRIMARY KEY,
            account_id TEXT NOT NULL REFERENCES accounts(id),
            request_hash TEXT NOT NULL,
            result_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        )""",
        """CREATE TABLE IF NOT EXISTS migration_runs (
            id TEXT PRIMARY KEY,
            state TEXT NOT NULL,
            preview_json TEXT NOT NULL,
            backup_path TEXT,
            created_at TEXT NOT NULL
        )""",
        "CREATE INDEX IF NOT EXISTS idx_courses_account_title ON courses(account_id, title)",
        "CREATE INDEX IF NOT EXISTS idx_lectures_course_date ON lectures(course_id, class_date)",
        "CREATE INDEX IF NOT EXISTS idx_workflows_updated ON workflows(updated_at, id)",
        "CREATE INDEX IF NOT EXISTS idx_steps_workflow_order ON steps(workflow_id, ordinal)",
        "CREATE INDEX IF NOT EXISTS idx_attempts_step_number ON attempts(step_id, number)",
    ),
    2: (
        "ALTER TABLE accounts ADD COLUMN course_sync_state TEXT NOT NULL DEFAULT 'never'",
        "ALTER TABLE accounts ADD COLUMN course_sync_started_at TEXT",
        "ALTER TABLE accounts ADD COLUMN course_sync_last_success_at TEXT",
        "ALTER TABLE accounts ADD COLUMN course_sync_error_json TEXT NOT NULL DEFAULT '{}'",
        "ALTER TABLE courses ADD COLUMN identity_kind TEXT NOT NULL DEFAULT 'platform'",
        "ALTER TABLE courses ADD COLUMN import_key TEXT",
        "ALTER TABLE lectures ADD COLUMN identity_kind TEXT NOT NULL DEFAULT 'platform'",
        "ALTER TABLE lectures ADD COLUMN import_key TEXT",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_courses_import_identity ON courses(account_id, import_key) WHERE import_key IS NOT NULL",
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_lectures_import_identity ON lectures(course_id, import_key) WHERE import_key IS NOT NULL",
    ),
    3: (
        "ALTER TABLE attempts ADD COLUMN runner_task_id TEXT",
        "ALTER TABLE attempts ADD COLUMN process_pid INTEGER",
    ),
    4: (
        "ALTER TABLE attempts ADD COLUMN error_code TEXT",
        "ALTER TABLE attempts ADD COLUMN error_json TEXT NOT NULL DEFAULT '{}'",
    ),
    5: (
        """CREATE TABLE reader_build_state (
            id TEXT PRIMARY KEY CHECK (id = 'workspace'),
            requested_generation INTEGER NOT NULL DEFAULT 0,
            published_generation INTEGER NOT NULL DEFAULT 0,
            state TEXT NOT NULL DEFAULT 'idle',
            last_error TEXT NOT NULL DEFAULT '',
            site_url TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL
        )""",
    ),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _json_object(value: str | None) -> dict[str, Any]:
    if not value:
        return {}
    decoded = json.loads(value)
    if not isinstance(decoded, dict):
        raise ValueError("stored JSON value must be an object")
    return decoded


_CLASS_DATE_PREFIX = re.compile(
    r"^\s*(\d{4})[-/](\d{1,2})[-/](\d{1,2})"
    r"(?:[ T](\d{1,2}):(\d{1,2})(?::(\d{1,2})(?:\.(\d+))?)?)?"
)


def class_date_sort_key(value: str | None) -> tuple[int, int, int, int, int, int, int, int, str]:
    """Parse padded or unpadded calendar dates into a stable chronological key."""
    raw = str(value or "").strip()
    match = _CLASS_DATE_PREFIX.match(raw)
    if not match:
        return (-1, 0, 0, 0, 0, 0, 0, 0, raw)
    parts = [int(part or 0) for part in match.groups()[:6]]
    fraction = (match.group(7) or "")[:6].ljust(6, "0")
    microsecond = int(fraction or 0)
    year, month, day, hour, minute, second = parts
    try:
        datetime(year, month, day, hour, minute, second, microsecond)
    except ValueError:
        return (-1, 0, 0, 0, 0, 0, 0, 0, raw)
    return (year, month, day, hour, minute, second, microsecond, 0, raw)


def stable_account_id(provider: str, platform_account_id: str) -> str:
    """Return an account-scoped opaque ID without storing the platform ID."""
    provider = provider.strip().lower()
    platform_account_id = platform_account_id.strip()
    if not provider or not platform_account_id:
        raise ValueError("provider and platform account ID are required")
    digest = hashlib.sha256(f"{provider}\0{platform_account_id}".encode("utf-8")).hexdigest()
    return f"acct-{digest[:32]}"


def _course_id(account_id: str, platform_course_id: str) -> str:
    digest = hashlib.sha256(f"{account_id}\0{platform_course_id}".encode("utf-8")).hexdigest()
    return f"course-{digest[:32]}"


def _import_course_id(account_id: str, import_key: str) -> str:
    digest = hashlib.sha256(f"{account_id}\0import\0{import_key}".encode("utf-8")).hexdigest()
    return f"course-{digest[:32]}"


def _lecture_id(course_id: str, platform_sub_id: str) -> str:
    digest = hashlib.sha256(f"{course_id}\0{platform_sub_id}".encode("utf-8")).hexdigest()
    return f"lecture-{digest[:32]}"


def _import_lecture_id(course_id: str, import_key: str) -> str:
    digest = hashlib.sha256(f"{course_id}\0import\0{import_key}".encode("utf-8")).hexdigest()
    return f"lecture-{digest[:32]}"


class ConsoleStorage:
    """Thread-local SQLite connections with short, nestable write transactions."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        if db_path is None:
            configured = os.environ.get("LOOK_TONGJI_CONSOLE_DB")
            db_path = configured or (Path(__file__).resolve().parent.parent / "state" / "console-v2.sqlite3")
        self.db_path = Path(db_path).expanduser().resolve()
        self._local = threading.local()
        self._migration_lock = threading.RLock()
        self.initialize()

    @property
    def connection(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            connection = sqlite3.connect(
                str(self.db_path), timeout=5.0, isolation_level=None, check_same_thread=True
            )
            try:
                connection.row_factory = sqlite3.Row
                connection.execute("PRAGMA foreign_keys = ON")
                connection.execute("PRAGMA busy_timeout = 5000")
                self._enable_wal(connection)
            except BaseException:
                connection.close()
                raise
            self._local.connection = connection
            self._local.transaction_depth = 0
        return connection

    @staticmethod
    def _enable_wal(connection: sqlite3.Connection) -> None:
        for attempt in range(10):
            try:
                connection.execute("PRAGMA journal_mode = WAL")
                return
            except sqlite3.OperationalError as error:
                if "locked" not in str(error).lower() or attempt == 9:
                    raise
                time.sleep(0.05 * (attempt + 1))

    def close_thread_connection(self) -> None:
        connection = getattr(self._local, "connection", None)
        if connection is not None:
            connection.close()
            del self._local.connection
            self._local.transaction_depth = 0

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        connection = self.connection
        depth = getattr(self._local, "transaction_depth", 0)
        savepoint = f"console_sp_{depth}"
        connection.execute("BEGIN IMMEDIATE" if depth == 0 else f"SAVEPOINT {savepoint}")
        self._local.transaction_depth = depth + 1
        try:
            yield connection
        except BaseException:
            if depth == 0:
                connection.rollback()
            else:
                connection.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
            raise
        else:
            if depth == 0:
                connection.commit()
            else:
                connection.execute(f"RELEASE SAVEPOINT {savepoint}")
        finally:
            self._local.transaction_depth = depth

    def initialize(self) -> None:
        with self._migration_lock:
            connection = self.connection
            connection.execute(
                """CREATE TABLE IF NOT EXISTS schema_migrations (
                    version INTEGER PRIMARY KEY,
                    applied_at TEXT NOT NULL
                )"""
            )
            rows = connection.execute("SELECT version FROM schema_migrations").fetchall()
            current = max((int(row[0]) for row in rows), default=0)
            if current > CURRENT_SCHEMA_VERSION:
                raise RuntimeError(
                    f"database schema {current} is newer than supported {CURRENT_SCHEMA_VERSION}"
                )
            for version in range(1, CURRENT_SCHEMA_VERSION + 1):
                statements = _MIGRATIONS.get(version)
                if statements is None:
                    raise RuntimeError(f"missing console database migration {version}")
                with self.transaction():
                    applied = connection.execute(
                        "SELECT COALESCE(MAX(version), 0) FROM schema_migrations"
                    ).fetchone()[0]
                    if int(applied) >= version:
                        continue
                    if int(applied) != version - 1:
                        raise RuntimeError(
                            f"database migration gap before version {version}"
                        )
                    for statement in statements:
                        connection.execute(statement)
                    connection.execute(
                        "INSERT INTO schema_migrations(version, applied_at) VALUES (?, ?)",
                        (version, _now()),
                    )

    def upsert_account(self, *, platform_account_id: str, provider: str) -> AccountRecord:
        provider = provider.strip().lower()
        account_id = stable_account_id(provider, platform_account_id)
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO accounts(id, provider, created_at) VALUES (?, ?, ?) "
                "ON CONFLICT(id) DO UPDATE SET provider = excluded.provider",
                (account_id, provider, _now()),
            )
            row = connection.execute("SELECT * FROM accounts WHERE id = ?", (account_id,)).fetchone()
        return self._account_dto(row)

    def upsert_course(
        self,
        *,
        account_id: str,
        platform_course_id: str,
        title: str,
        teacher: str | None = None,
        term: str | None = None,
        source: str = "platform",
        synced_at: str | None = None,
    ) -> CourseRecord:
        account_id = account_id.strip()
        platform_course_id = platform_course_id.strip()
        title = title.strip()
        if not account_id or not platform_course_id or not title:
            raise ValueError("account, platform course ID, and course title are required")
        course_id = _course_id(account_id, platform_course_id)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO courses(
                    id, account_id, platform_course_id, title, teacher, term, source, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(account_id, platform_course_id) DO UPDATE SET
                    title = excluded.title, teacher = excluded.teacher, term = excluded.term,
                    source = excluded.source, synced_at = excluded.synced_at""",
                (course_id, account_id, platform_course_id, title, teacher, term, source, synced_at),
            )
            row = connection.execute(
                "SELECT * FROM courses WHERE account_id = ? AND platform_course_id = ?",
                (account_id, platform_course_id),
            ).fetchone()
        return self._course_dto(row)

    def upsert_import_course(
        self,
        *,
        account_id: str,
        import_key: str,
        title: str,
        teacher: str | None = None,
        term: str | None = None,
    ) -> CourseRecord:
        account_id, import_key, title = account_id.strip(), import_key.strip(), title.strip()
        if not account_id or not import_key or not title:
            raise ValueError("account, import identity, and course title are required")
        course_id = _import_course_id(account_id, import_key)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO courses(
                    id, account_id, platform_course_id, identity_kind, import_key,
                    title, teacher, term, source, synced_at
                ) VALUES (?, ?, NULL, 'import', ?, ?, ?, ?, 'local', NULL)
                ON CONFLICT(account_id, import_key) WHERE import_key IS NOT NULL DO UPDATE SET
                    title = excluded.title, teacher = excluded.teacher, term = excluded.term,
                    source = excluded.source""",
                (course_id, account_id, import_key, title, teacher, term),
            )
            row = connection.execute(
                "SELECT * FROM courses WHERE account_id = ? AND import_key = ?",
                (account_id, import_key),
            ).fetchone()
        return self._course_dto(row)

    def upsert_lecture(
        self,
        *,
        course_id: str,
        platform_sub_id: str,
        title: str,
        class_date: str | None = None,
        playback_available: bool = False,
        manifest_path: str | None = None,
    ) -> LectureRecord:
        course_id, platform_sub_id, title = course_id.strip(), platform_sub_id.strip(), title.strip()
        if not course_id or not platform_sub_id or not title:
            raise ValueError("course, platform lecture ID, and lecture title are required")
        lecture_id = _lecture_id(course_id, platform_sub_id)
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO lectures(
                    id, course_id, platform_sub_id, title, class_date, playback_available, manifest_path
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(course_id, platform_sub_id) DO UPDATE SET
                    title = excluded.title, class_date = excluded.class_date,
                    playback_available = excluded.playback_available,
                    manifest_path = excluded.manifest_path""",
                (
                    lecture_id,
                    course_id,
                    platform_sub_id,
                    title,
                    class_date,
                    int(playback_available),
                    manifest_path,
                ),
            )
            row = connection.execute(
                "SELECT * FROM lectures WHERE course_id = ? AND platform_sub_id = ?",
                (course_id, platform_sub_id),
            ).fetchone()
        return self._lecture_dto(row)

    def upsert_import_lecture(
        self,
        *,
        course_id: str,
        import_key: str,
        title: str,
        class_date: str | None = None,
        playback_available: bool = False,
        manifest_path: str | None = None,
    ) -> LectureRecord:
        course_id, import_key, title = course_id.strip(), import_key.strip(), title.strip()
        if not course_id or not import_key or not title:
            raise ValueError("course, import identity, and lecture title are required")
        lecture_id = _import_lecture_id(course_id, import_key)
        import_marker = "import-" + hashlib.sha256(import_key.encode("utf-8")).hexdigest()[:32]
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO lectures(
                    id, course_id, platform_sub_id, identity_kind, import_key,
                    title, class_date, playback_available, manifest_path
                ) VALUES (?, ?, ?, 'import', ?, ?, ?, ?, ?)
                ON CONFLICT(course_id, import_key) WHERE import_key IS NOT NULL DO UPDATE SET
                    title = excluded.title, class_date = excluded.class_date,
                    playback_available = excluded.playback_available,
                    manifest_path = excluded.manifest_path""",
                (
                    lecture_id,
                    course_id,
                    import_marker,
                    import_key,
                    title,
                    class_date,
                    int(playback_available),
                    manifest_path,
                ),
            )
            row = connection.execute(
                "SELECT * FROM lectures WHERE course_id = ? AND import_key = ?",
                (course_id, import_key),
            ).fetchone()
        return self._lecture_dto(row)

    def get_course(self, course_id: str, account_id: str) -> CourseRecord | None:
        row = self.connection.execute(
            "SELECT * FROM courses WHERE id = ? AND account_id = ?", (course_id, account_id)
        ).fetchone()
        return self._course_dto(row) if row else None

    def get_account(self, account_id: str) -> AccountRecord | None:
        row = self.connection.execute(
            "SELECT * FROM accounts WHERE id = ?", (account_id,)
        ).fetchone()
        return self._account_dto(row) if row else None

    def list_courses(
        self, account_id: str, *, source: str = "all", query: str = ""
    ) -> list[CourseRecord]:
        if source not in {"local", "platform", "all"}:
            raise ValueError("course source must be local, platform, or all")
        rows = self.connection.execute(
            """SELECT * FROM courses
            WHERE account_id = ?
              AND (? = 'all' OR (? = 'local' AND source IN ('local', 'import'))
                   OR (? = 'platform' AND source = 'platform'))
              AND (? = '' OR instr(lower(title), lower(?)) > 0
                   OR instr(lower(COALESCE(teacher, '')), lower(?)) > 0)
            ORDER BY title COLLATE NOCASE, id""",
            (account_id, source, source, source, query.strip(), query.strip(), query.strip()),
        ).fetchall()
        return [self._course_dto(row) for row in rows]

    def list_lectures(self, course_id: str) -> list[LectureRecord]:
        rows = self.connection.execute(
            "SELECT * FROM lectures WHERE course_id = ?",
            (course_id,),
        ).fetchall()
        lectures = [self._lecture_dto(row) for row in rows]
        return sorted(
            lectures,
            key=lambda lecture: (
                class_date_sort_key(lecture.class_date)[0] < 0,
                class_date_sort_key(lecture.class_date),
                lecture.id,
            ),
        )

    def set_course_sync_status(
        self,
        account_id: str,
        state: str,
        *,
        error: dict[str, Any] | None = None,
    ) -> CourseSyncStatus:
        if state not in {"syncing", "succeeded", "failed"}:
            raise ValueError("invalid course sync state")
        now = _now()
        with self.transaction() as connection:
            if state == "syncing":
                connection.execute(
                    """UPDATE accounts SET course_sync_state = ?, course_sync_started_at = ?,
                        course_sync_error_json = '{}' WHERE id = ?""",
                    (state, now, account_id),
                )
            elif state == "succeeded":
                connection.execute(
                    """UPDATE accounts SET course_sync_state = ?, course_sync_last_success_at = ?,
                        course_sync_error_json = '{}' WHERE id = ?""",
                    (state, now, account_id),
                )
            else:
                connection.execute(
                    """UPDATE accounts SET course_sync_state = ?, course_sync_error_json = ?
                        WHERE id = ?""",
                    (
                        state,
                        json.dumps(error or {}, ensure_ascii=False, sort_keys=True),
                        account_id,
                    ),
                )
        status = self.get_course_sync_status(account_id)
        if status is None:
            raise KeyError("account not found")
        return status

    def get_course_sync_status(self, account_id: str) -> CourseSyncStatus | None:
        row = self.connection.execute(
            """SELECT course_sync_state, course_sync_started_at,
                course_sync_last_success_at, course_sync_error_json
                FROM accounts WHERE id = ?""",
            (account_id,),
        ).fetchone()
        if not row:
            return None
        return CourseSyncStatus(
            state=row["course_sync_state"],
            started_at=row["course_sync_started_at"],
            last_success_at=row["course_sync_last_success_at"],
            error=_json_object(row["course_sync_error_json"]),
        )

    def get_lecture(self, lecture_id: str) -> LectureRecord | None:
        row = self.connection.execute("SELECT * FROM lectures WHERE id = ?", (lecture_id,)).fetchone()
        return self._lecture_dto(row) if row else None

    def create_settings_revision(self, settings: dict[str, Any]) -> SettingsRevisionRecord:
        serialized = public_settings_json(settings)
        revision_id, created_at = str(uuid.uuid4()), _now()
        with self.transaction() as connection:
            connection.execute(
                "INSERT INTO settings_revisions(id, public_json, created_at) VALUES (?, ?, ?)",
                (revision_id, serialized, created_at),
            )
        return SettingsRevisionRecord(revision_id, json.loads(serialized), created_at)

    def get_settings_revision(self, revision_id: str) -> SettingsRevisionRecord | None:
        row = self.connection.execute(
            "SELECT * FROM settings_revisions WHERE id = ?", (revision_id,)
        ).fetchone()
        if not row:
            return None
        return SettingsRevisionRecord(row["id"], _json_object(row["public_json"]), row["created_at"])

    def latest_settings_revision(self) -> SettingsRevisionRecord | None:
        row = self.connection.execute(
            "SELECT * FROM settings_revisions ORDER BY created_at DESC, id DESC LIMIT 1"
        ).fetchone()
        if not row:
            return None
        return SettingsRevisionRecord(row["id"], _json_object(row["public_json"]), row["created_at"])

    def create_workflow(
        self,
        *,
        lecture_id: str,
        operation: str,
        settings_revision_id: str | None,
        target: dict[str, Any],
        state: str = "queued",
    ) -> WorkflowRecord:
        validate_state(state, WORKFLOW_STATES, "workflow")
        target_json = public_object_json(target, label="workflow target")
        workflow_id, created_at = str(uuid.uuid4()), _now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO workflows(
                    id, lecture_id, operation, state, settings_revision_id, target_json,
                    created_at, updated_at, cancel_requested
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)""",
                (
                    workflow_id,
                    lecture_id,
                    operation,
                    state,
                    settings_revision_id,
                    target_json,
                    created_at,
                    created_at,
                ),
            )
        return self.get_workflow(workflow_id)  # type: ignore[return-value]

    def create_idempotent_workflow(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        request_hash: str,
        lecture_id: str,
        operation: str,
        settings_snapshot: dict[str, Any],
        target: dict[str, Any],
    ) -> tuple[WorkflowRecord, bool]:
        target_json = public_object_json(target, label="workflow target")
        settings_json = public_settings_json(settings_snapshot)
        workflow_id, created_at = str(uuid.uuid4()), _now()
        settings_revision_id = str(uuid.uuid4())
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT account_id, request_hash, result_json FROM idempotency_keys WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["account_id"] != account_id or existing["request_hash"] != request_hash:
                    raise IdempotencyConflict("相同请求键已用于不同的任务内容")
                result = _json_object(existing["result_json"])
                row = connection.execute("SELECT id FROM workflows WHERE id = ?", (result.get("workflow_id"),)).fetchone()
                if row is None:
                    raise RuntimeError("幂等记录指向的任务不存在")
                existing_id = row["id"]
                created = False
            else:
                connection.execute(
                    "INSERT INTO settings_revisions(id, public_json, created_at) VALUES (?, ?, ?)",
                    (settings_revision_id, settings_json, created_at),
                )
                connection.execute(
                    """INSERT INTO workflows(
                        id, lecture_id, operation, state, settings_revision_id, target_json,
                        created_at, updated_at, cancel_requested
                    ) VALUES (?, ?, ?, 'queued', ?, ?, ?, ?, 0)""",
                    (workflow_id, lecture_id, operation, settings_revision_id, target_json, created_at, created_at),
                )
                connection.execute(
                    "INSERT INTO idempotency_keys(key, account_id, request_hash, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (idempotency_key, account_id, request_hash, json.dumps({"workflow_id": workflow_id}), created_at),
                )
                existing_id = workflow_id
                created = True
        return self.get_workflow(existing_id), created  # type: ignore[return-value]

    def update_workflow_state(
        self, workflow_id: str, state: str, *, cancel_requested: bool | None = None
    ) -> WorkflowRecord | None:
        validate_state(state, WORKFLOW_STATES, "workflow")
        with self.transaction() as connection:
            if cancel_requested is None:
                connection.execute(
                    "UPDATE workflows SET state = ?, updated_at = ? WHERE id = ?",
                    (state, _now(), workflow_id),
                )
            else:
                connection.execute(
                    "UPDATE workflows SET state = ?, cancel_requested = ?, updated_at = ? WHERE id = ?",
                    (state, int(cancel_requested), _now(), workflow_id),
                )
        return self.get_workflow(workflow_id)

    def request_reader_build(self) -> dict[str, Any]:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO reader_build_state(id, requested_generation, published_generation, state, updated_at)
                   VALUES ('workspace', 1, 0, 'queued', ?)
                   ON CONFLICT(id) DO UPDATE SET requested_generation = reader_build_state.requested_generation + 1,
                       state = CASE WHEN reader_build_state.state = 'building' THEN 'building' ELSE 'queued' END,
                       last_error = '', updated_at = excluded.updated_at""",
                (_now(),),
            )
        return self.get_reader_build_state()

    def get_reader_build_state(self) -> dict[str, Any]:
        row = self.connection.execute("SELECT * FROM reader_build_state WHERE id = 'workspace'").fetchone()
        if row is None:
            return {"requested_generation": 0, "published_generation": 0, "state": "idle", "last_error": "", "site_url": ""}
        return dict(row)

    def set_reader_build_state(
        self, *, published_generation: int | None = None, state: str, last_error: str = "", site_url: str = ""
    ) -> dict[str, Any]:
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO reader_build_state(id, requested_generation, published_generation, state, last_error, site_url, updated_at)
                   VALUES ('workspace', 0, ?, ?, ?, ?, ?)
                   ON CONFLICT(id) DO UPDATE SET
                       published_generation = COALESCE(?, reader_build_state.published_generation),
                       state = ?, last_error = ?, site_url = ?, updated_at = ?""",
                (published_generation or 0, state, last_error, site_url, _now(), published_generation,
                 state, last_error, site_url, _now()),
            )
        return self.get_reader_build_state()

    def upsert_step(
        self,
        *,
        workflow_id: str,
        name: str,
        ordinal: int,
        state: str = "pending",
        artifact: dict[str, Any] | None = None,
        error_code: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> StepRecord:
        validate_state(state, STEP_STATES, "step")
        if ordinal < 0:
            raise ValueError("step ordinal must be non-negative")
        step_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{workflow_id}\0{name}"))
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO steps(
                    id, workflow_id, name, ordinal, state, artifact_json, error_code, error_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(workflow_id, name) DO UPDATE SET
                    ordinal = excluded.ordinal, state = excluded.state,
                    artifact_json = excluded.artifact_json, error_code = excluded.error_code,
                    error_json = excluded.error_json""",
                (
                    step_id,
                    workflow_id,
                    name,
                    ordinal,
                    state,
                    json.dumps(artifact or {}, ensure_ascii=False, sort_keys=True),
                    error_code,
                    json.dumps(error or {}, ensure_ascii=False, sort_keys=True),
                ),
            )
            row = connection.execute(
                "SELECT * FROM steps WHERE workflow_id = ? AND name = ?", (workflow_id, name)
            ).fetchone()
        return self._step_dto(row, attempts=[])

    def create_attempt(
        self,
        *,
        step_id: str,
        state: str = "running",
        exit_code: int | None = None,
        progress: dict[str, Any] | None = None,
        log_path: str | None = None,
        runner_task_id: str | None = None,
        process_pid: int | None = None,
    ) -> AttemptRecord:
        validate_state(state, ATTEMPT_STATES, "attempt")
        with self.transaction() as connection:
            number = int(
                connection.execute(
                    "SELECT COALESCE(MAX(number), 0) + 1 FROM attempts WHERE step_id = ?", (step_id,)
                ).fetchone()[0]
            )
            started_at = _now()
            ended_at = None if state == "running" else started_at
            attempt_id = str(uuid.uuid4())
            connection.execute(
                """INSERT INTO attempts(
                    id, step_id, number, state, started_at, ended_at, exit_code, progress_json, log_path,
                    runner_task_id, process_pid, error_code, error_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, '{}')""",
                (
                    attempt_id,
                    step_id,
                    number,
                    state,
                    started_at,
                    ended_at,
                    exit_code,
                    json.dumps(progress or {}, ensure_ascii=False, sort_keys=True),
                    log_path,
                    runner_task_id,
                    process_pid,
                ),
            )
            row = connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        return self._attempt_dto(row)

    def create_idempotent_retry_attempt(
        self,
        *,
        account_id: str,
        idempotency_key: str,
        request_hash: str,
        workflow_id: str,
        step_id: str,
        log_path: str,
    ) -> tuple[AttemptRecord, bool]:
        with self.transaction() as connection:
            existing = connection.execute(
                "SELECT account_id, request_hash, result_json FROM idempotency_keys WHERE key = ?",
                (idempotency_key,),
            ).fetchone()
            if existing:
                if existing["account_id"] != account_id or existing["request_hash"] != request_hash:
                    raise IdempotencyConflict("相同请求键已用于不同的恢复操作")
                attempt_id = _json_object(existing["result_json"]).get("attempt_id")
                row = connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                if row is None:
                    raise RuntimeError("幂等记录指向的执行记录不存在")
                created = False
            else:
                step = connection.execute(
                    "SELECT workflow_id FROM steps WHERE id = ?", (step_id,)
                ).fetchone()
                if step is None or step["workflow_id"] != workflow_id:
                    raise ValueError("恢复步骤与任务不匹配")
                number = int(connection.execute(
                    "SELECT COALESCE(MAX(number), 0) + 1 FROM attempts WHERE step_id = ?", (step_id,)
                ).fetchone()[0])
                attempt_id, started_at = str(uuid.uuid4()), _now()
                connection.execute(
                    """INSERT INTO attempts(
                        id, step_id, number, state, started_at, ended_at, exit_code, progress_json,
                        log_path, runner_task_id, process_pid, error_code, error_json
                    ) VALUES (?, ?, ?, 'running', ?, NULL, NULL, '{}', ?, NULL, NULL, NULL, '{}')""",
                    (attempt_id, step_id, number, started_at, log_path),
                )
                connection.execute(
                    "INSERT INTO idempotency_keys(key, account_id, request_hash, result_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (idempotency_key, account_id, request_hash, json.dumps({"attempt_id": attempt_id}), started_at),
                )
                connection.execute(
                    "UPDATE workflows SET state = 'queued', cancel_requested = 0, updated_at = ? WHERE id = ?",
                    (started_at, workflow_id),
                )
                connection.execute(
                    "UPDATE steps SET state = 'queued', error_code = NULL, error_json = '{}' WHERE id = ?",
                    (step_id,),
                )
                row = connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
                created = True
        return self._attempt_dto(row), created

    def find_idempotent_attempt(
        self, *, account_id: str, idempotency_key: str, request_hash: str
    ) -> AttemptRecord | None:
        row = self.connection.execute(
            "SELECT account_id, request_hash, result_json FROM idempotency_keys WHERE key = ?",
            (idempotency_key,),
        ).fetchone()
        if row is None:
            return None
        if row["account_id"] != account_id or row["request_hash"] != request_hash:
            raise IdempotencyConflict("相同请求键已用于不同的恢复操作")
        attempt_id = _json_object(row["result_json"]).get("attempt_id")
        attempt = self.connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        if attempt is None:
            raise RuntimeError("幂等记录指向的执行记录不存在")
        return self._attempt_dto(attempt)

    def update_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        exit_code: int | None = None,
        progress: dict[str, Any] | None = None,
        log_path: str | None = None,
        runner_task_id: str | None = None,
        process_pid: int | None = None,
        error_code: str | None = None,
        error: dict[str, Any] | None = None,
    ) -> AttemptRecord | None:
        validate_state(state, ATTEMPT_STATES, "attempt")
        with self.transaction() as connection:
            connection.execute(
                """UPDATE attempts SET state = ?, ended_at = ?, exit_code = ?,
                    progress_json = ?, log_path = COALESCE(?, log_path),
                    runner_task_id = COALESCE(?, runner_task_id), process_pid = COALESCE(?, process_pid),
                    error_code = COALESCE(?, error_code), error_json = COALESCE(?, error_json)
                    WHERE id = ?""",
                (
                    state,
                    _now() if state != "running" else None,
                    exit_code,
                    json.dumps(progress or {}, ensure_ascii=False, sort_keys=True),
                    log_path,
                    runner_task_id,
                    process_pid,
                    error_code,
                    json.dumps(error, ensure_ascii=False, sort_keys=True) if error is not None else None,
                    attempt_id,
                ),
            )
        row = self.connection.execute("SELECT * FROM attempts WHERE id = ?", (attempt_id,)).fetchone()
        return self._attempt_dto(row) if row else None

    def upsert_artifact(
        self,
        *,
        lecture_id: str,
        kind: str,
        path: str,
        validation: dict[str, Any],
        producer_attempt_id: str | None = None,
    ) -> ArtifactRecord:
        artifact_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{lecture_id}\0{kind}\0{path}"))
        updated_at = _now()
        with self.transaction() as connection:
            connection.execute(
                """INSERT INTO artifacts(
                    id, lecture_id, kind, path, validation_json, producer_attempt_id, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(lecture_id, kind, path) DO UPDATE SET
                    validation_json = excluded.validation_json,
                    producer_attempt_id = excluded.producer_attempt_id,
                    updated_at = excluded.updated_at""",
                (
                    artifact_id,
                    lecture_id,
                    kind,
                    path,
                    json.dumps(validation, ensure_ascii=False, sort_keys=True),
                    producer_attempt_id,
                    updated_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM artifacts WHERE lecture_id = ? AND kind = ? AND path = ?",
                (lecture_id, kind, path),
            ).fetchone()
        return ArtifactRecord(
            row["id"], row["lecture_id"], row["kind"], row["path"],
            _json_object(row["validation_json"]), row["producer_attempt_id"], row["updated_at"]
        )

    def get_workflow(self, workflow_id: str) -> WorkflowRecord | None:
        row = self.connection.execute("SELECT * FROM workflows WHERE id = ?", (workflow_id,)).fetchone()
        if not row:
            return None
        steps_rows = self.connection.execute(
            "SELECT * FROM steps WHERE workflow_id = ? ORDER BY ordinal, id", (workflow_id,)
        ).fetchall()
        steps: list[StepRecord] = []
        for step_row in steps_rows:
            attempt_rows = self.connection.execute(
                "SELECT * FROM attempts WHERE step_id = ? ORDER BY number, id", (step_row["id"],)
            ).fetchall()
            steps.append(self._step_dto(step_row, [self._attempt_dto(item) for item in attempt_rows]))
        return WorkflowRecord(
            id=row["id"],
            lecture_id=row["lecture_id"],
            operation=row["operation"],
            state=row["state"],
            settings_revision_id=row["settings_revision_id"],
            target=_json_object(row["target_json"]),
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            cancel_requested=bool(row["cancel_requested"]),
            steps=steps,
        )

    def list_workflows_for_account(self, account_id: str, *, limit: int = 100) -> list[WorkflowRecord]:
        bounded = max(1, min(int(limit), 500))
        rows = self.connection.execute(
            """SELECT w.id FROM workflows w
            JOIN lectures l ON l.id = w.lecture_id
            JOIN courses c ON c.id = l.course_id
            WHERE c.account_id = ? ORDER BY w.created_at DESC, w.id DESC LIMIT ?""",
            (account_id, bounded),
        ).fetchall()
        return [record for row in rows if (record := self.get_workflow(row["id"])) is not None]

    def get_workflow_for_account(self, workflow_id: str, account_id: str) -> WorkflowRecord | None:
        row = self.connection.execute(
            """SELECT w.id FROM workflows w
            JOIN lectures l ON l.id = w.lecture_id
            JOIN courses c ON c.id = l.course_id
            WHERE w.id = ? AND c.account_id = ?""",
            (workflow_id, account_id),
        ).fetchone()
        return self.get_workflow(row["id"]) if row else None

    def list_active_workflows(self) -> list[WorkflowRecord]:
        rows = self.connection.execute(
            "SELECT id FROM workflows WHERE state IN ('queued', 'running', 'blocked') ORDER BY created_at, id"
        ).fetchall()
        return [record for row in rows if (record := self.get_workflow(row["id"])) is not None]

    def read_log_offset(
        self, path: str | Path, *, offset: int = 0, max_bytes: int = 64 * 1024
    ) -> LogSlice:
        if offset < 0:
            raise ValueError("log offset must be non-negative")
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        read_limit = max(4, min(int(max_bytes), _MAX_LOG_BYTES))
        with Path(path).open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            size = stream.tell()
            actual_offset = min(offset, size)
            stream.seek(actual_offset)
            raw = stream.read(read_limit)
        decoder = codecs.getincrementaldecoder("utf-8")("replace")
        text = decoder.decode(raw, final=False)
        incomplete = decoder.getstate()[0]
        consumed = len(raw) - len(incomplete)
        next_offset = actual_offset + consumed
        return LogSlice(
            offset=actual_offset,
            next_offset=next_offset,
            size=size,
            text=text,
            truncated=next_offset < size,
        )

    @staticmethod
    def _account_dto(row: sqlite3.Row) -> AccountRecord:
        return AccountRecord(
            id=row["id"],
            provider=row["provider"],
            created_at=row["created_at"],
            course_sync_state=row["course_sync_state"],
            course_sync_started_at=row["course_sync_started_at"],
            course_sync_last_success_at=row["course_sync_last_success_at"],
            course_sync_error=_json_object(row["course_sync_error_json"]),
        )

    @staticmethod
    def _course_dto(row: sqlite3.Row) -> CourseRecord:
        return CourseRecord(**dict(row))

    @staticmethod
    def _lecture_dto(row: sqlite3.Row) -> LectureRecord:
        values = dict(row)
        values["playback_available"] = bool(values["playback_available"])
        return LectureRecord(**values)

    @staticmethod
    def _attempt_dto(row: sqlite3.Row) -> AttemptRecord:
        return AttemptRecord(
            id=row["id"],
            step_id=row["step_id"],
            number=int(row["number"]),
            state=row["state"],
            started_at=row["started_at"],
            ended_at=row["ended_at"],
            exit_code=row["exit_code"],
            progress=_json_object(row["progress_json"]),
            log_path=row["log_path"],
            runner_task_id=row["runner_task_id"],
            process_pid=row["process_pid"],
            error_code=row["error_code"],
            error=_json_object(row["error_json"]),
        )

    @staticmethod
    def _step_dto(row: sqlite3.Row, attempts: list[AttemptRecord]) -> StepRecord:
        return StepRecord(
            id=row["id"],
            workflow_id=row["workflow_id"],
            name=row["name"],
            ordinal=int(row["ordinal"]),
            state=row["state"],
            artifact=_json_object(row["artifact_json"]),
            error_code=row["error_code"],
            error=_json_object(row["error_json"]),
            attempts=attempts,
        )
