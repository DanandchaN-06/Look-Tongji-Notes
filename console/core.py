"""Shared helpers: paths, subprocess bridge, JSON extraction, log redaction."""

from __future__ import annotations

import json
import os
import re
import signal
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

CONSOLE_DIR = Path(__file__).resolve().parent
REPO_ROOT = CONSOLE_DIR.parent
SCRIPTS_DIR = REPO_ROOT / "scripts"
CLI_PATH = SCRIPTS_DIR / "look_tongji.py"
WEB_DIR = CONSOLE_DIR / "web"
VISION_DIR = REPO_ROOT / "vision-support"
ENV_PATH = REPO_ROOT / ".env"
VISION_CONFIG_PATH = VISION_DIR / "config.json"


def python_executable() -> str:
    """Interpreter used to run the CLI.

    The console itself must run under the same interpreter that has
    requests/playwright installed, so reuse `sys.executable`.
    """
    return sys.executable or "python"


def cli_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for CLI subprocesses.

    `PYTHONPATH` points at `scripts/` so `import tongji_backend` works without
    relying on the current working directory.
    """
    env = os.environ.copy()
    env["PYTHONPATH"] = str(SCRIPTS_DIR) + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    # Never let a child process block on stdin.
    env["PYTHONUNBUFFERED"] = "1"
    env["LOOK_TONGJI_CONSOLE_EVENTS"] = "1"
    if extra:
        env.update(extra)
    return env


def base_command() -> list[str]:
    return [python_executable(), "-u", str(CLI_PATH)]


def child_popen_kwargs() -> dict[str, Any]:
    """Options every CLI subprocess must share.

    `start_new_session` puts the child in its own process group on POSIX so a
    cancel can signal the whole tree (python -> ffmpeg) without matching our
    own group and taking the console down with it.
    """
    return {"start_new_session": os.name != "nt"}


def kill_process_tree(proc: subprocess.Popen) -> None:
    """Terminate a process and everything it spawned."""
    if proc.poll() is not None:
        return

    if os.name == "nt":
        try:
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
                check=False,
                timeout=20,
            )
        except Exception:
            pass
    else:
        try:
            child_pgid = os.getpgid(proc.pid)
            own_pgid = os.getpgid(0)
            if child_pgid != own_pgid:
                os.killpg(child_pgid, signal.SIGTERM)
            else:
                proc.terminate()
        except Exception:
            try:
                proc.terminate()
            except Exception:
                pass

    try:
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


@dataclass
class CliResult:
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run_cli(
    args: Sequence[str],
    *,
    timeout: float | None = 600.0,
    env_extra: dict[str, str] | None = None,
) -> CliResult:
    """Run the CLI non-interactively and capture output.

    stdin is closed so any accidental `input()` fails fast instead of hanging
    the console forever. On timeout the whole process tree is killed rather
    than just the direct child, otherwise an in-flight ffmpeg would keep
    running and holding its output file.
    """
    proc = subprocess.Popen(
        base_command() + list(args),
        cwd=str(REPO_ROOT),
        env=cli_env(env_extra),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        **child_popen_kwargs(),
    )
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_process_tree(proc)
        try:
            out, err = proc.communicate(timeout=15)
        except Exception:
            out, err = "", ""
        note = f"\n[console] 命令超时（{timeout:.0f}s），已终止其进程树。"
        values = secret_values()
        return CliResult(-1, redact_env_values(redact(out or ""), values), redact_env_values(redact((err or "") + note), values))
    values = secret_values()
    return CliResult(proc.returncode if proc.returncode is not None else -1,
                     redact_env_values(redact(out or ""), values), redact_env_values(redact(err or ""), values))


def extract_json(stdout: str) -> dict[str, Any] | None:
    """Pull the machine-readable payload out of CLI stdout.

    The CLI and its backend print human-readable progress lines to stdout
    (e.g. `[Auth] ...`, `[WARN] ...`), so the JSON is not necessarily the only
    thing on the stream. Our JSON payloads are single-line and always start
    with `{"ok"`, so take the last such line.
    """
    candidates: list[str] = []
    for line in stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("{") and stripped.endswith("}"):
            candidates.append(stripped)
    for candidate in reversed(candidates):
        try:
            parsed = json.loads(candidate)
        except (ValueError, TypeError):
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


# --------------------------------------------------------------------------
# Log redaction
# --------------------------------------------------------------------------

_TOKEN_KEYS = (
    "token",
    "jwt",
    "password",
    "passwd",
    "secret",
    "api_key",
    "apikey",
    "authorization",
)

_REDACTION_PATTERNS: list[tuple[re.Pattern[str], str]] = [
    # `[Auth] Login successful! Token: eyJhbGciOi...`
    (re.compile(r"(?i)\btoken:\s*\S+"), "Token: <redacted>"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]+"), "Bearer <redacted>"),
    (re.compile(r"(?i)\b[\w.\-]*(?:password|passwd|secret|api[_-]?key)[\w.\-]*\s*[=:]\s*\S+"),
     "<redacted-credential>"),
    # Long base64/hex-ish blobs (JWTs, keys) that are not obviously words.
    (re.compile(r"\b[A-Za-z0-9_\-]{32,}\.[A-Za-z0-9_\-]{16,}\.[A-Za-z0-9_\-]{16,}\b"),
     "<redacted-jwt>"),
]


def redact(text: str) -> str:
    """Mask anything that looks like a credential before it reaches the UI."""
    if not text:
        return text
    out = text
    for pattern, replacement in _REDACTION_PATTERNS:
        out = pattern.sub(replacement, out)
    return out


def redact_env_values(text: str, values: Sequence[str]) -> str:
    """Additionally mask literal secret values we know about."""
    out = text
    for value in values:
        if value and len(value) >= 4:
            out = out.replace(value, "<redacted>")
    return out


def secret_values() -> list[str]:
    """Known password/key values, used for both sync responses and task logs."""
    values = [value for key, value in os.environ.items()
              if any(word in key.upper() for word in ("PASSWORD", "API_KEY", "APIKEY", "SECRET", "TOKEN"))]
    try:
        from .config_store import read_env, read_vision
        values += [value for key, value in read_env().items()
                   if any(word in key.upper() for word in ("PASSWORD", "API_KEY", "APIKEY", "SECRET", "TOKEN"))]
        models = read_vision().get("models") or []
        if isinstance(models, list):
            values += [str(entry.get("apiKey") or "") for entry in models if isinstance(entry, dict)]
    except (OSError, ValueError):
        pass
    return list({value for value in values if value})
