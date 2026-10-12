"""Configuration store: credentials, workspace, and vision model settings.

Design rules:
- The console never hands a secret back to the browser. `public_*` helpers
  return only booleans and non-secret fields.
- Writing the workspace config never silently migrates existing content.
  Migration is a separate, explicitly requested operation.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .core import ENV_PATH, REPO_ROOT, SCRIPTS_DIR, VISION_CONFIG_PATH, VISION_DIR

if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from tongji_backend.workspace import (  # noqa: E402
    WorkspaceConfig,
    _config_path as _resolve_config_path,
    ensure_workspace_scaffold,
    load_workspace_config,
    migrate_workspace_root,
    save_workspace_config,
)

# --------------------------------------------------------------------------
# Vision provider catalog (mirrors vision-support/scripts/vision.mjs)
# --------------------------------------------------------------------------

VISION_PROVIDERS: list[dict[str, Any]] = [
    {
        "id": "openai", "name": "OpenAI", "apiFormat": "openai",
        "baseUrl": "https://api.openai.com/v1", "keyEnv": "OPENAI_API_KEY",
        "models": ["gpt-4o", "gpt-4o-mini", "gpt-4-turbo"], "needsKey": True,
    },
    {
        "id": "google", "name": "Google Gemini", "apiFormat": "google",
        "baseUrl": "https://generativelanguage.googleapis.com/v1beta",
        "keyEnv": "GEMINI_API_KEY",
        "models": ["gemini-2.5-pro", "gemini-2.5-flash", "gemini-2.0-flash"], "needsKey": True,
    },
    {
        "id": "anthropic", "name": "Anthropic Claude", "apiFormat": "anthropic",
        "baseUrl": "https://api.anthropic.com", "keyEnv": "ANTHROPIC_API_KEY",
        "models": ["claude-sonnet-4-20250514", "claude-opus-4-20250514"], "needsKey": True,
    },
    {
        "id": "deepseek", "name": "DeepSeek", "apiFormat": "openai",
        "baseUrl": "https://api.deepseek.com", "keyEnv": "DEEPSEEK_API_KEY",
        "models": ["deepseek-chat"], "needsKey": True,
    },
    {
        "id": "dashscope", "name": "通义千问 Qwen-VL", "apiFormat": "openai",
        "baseUrl": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "keyEnv": "DASHSCOPE_API_KEY",
        "models": ["qwen-vl-max", "qwen-vl-plus", "qwen2.5-vl-72b-instruct"], "needsKey": True,
    },
    {
        "id": "zhipuai", "name": "智谱 GLM-4V", "apiFormat": "openai",
        "baseUrl": "https://open.bigmodel.cn/api/paas/v4", "keyEnv": "GLM_API_KEY",
        "models": ["glm-4v-plus", "glm-4v", "glm-4v-flash"], "needsKey": True,
    },
    {
        "id": "moonshot", "name": "Moonshot (Kimi)", "apiFormat": "openai",
        "baseUrl": "https://api.moonshot.cn/v1", "keyEnv": "MOONSHOT_API_KEY",
        "models": ["moonshot-v1-8k"], "needsKey": True,
    },
    {
        "id": "siliconflow", "name": "SiliconFlow 硅基流动", "apiFormat": "openai",
        "baseUrl": "https://api.siliconflow.cn/v1", "keyEnv": "SILICONFLOW_API_KEY",
        "models": [], "needsKey": True,
    },
    {
        "id": "ollama", "name": "Ollama（本地）", "apiFormat": "openai",
        "baseUrl": "http://localhost:11434/v1", "keyEnv": "",
        "models": ["llava", "llava-llama3", "bakllava", "minicpm-v"], "needsKey": False,
    },
    {
        "id": "lmstudio", "name": "LM Studio（本地）", "apiFormat": "openai",
        "baseUrl": "http://localhost:1234/v1", "keyEnv": "",
        "models": [], "needsKey": False,
    },
    {
        "id": "custom", "name": "第三方 OpenAI 兼容平台", "apiFormat": "openai",
        "baseUrl": "", "keyEnv": "", "models": [], "needsKey": True,
    },
]

PROVIDER_BY_ID = {p["id"]: p for p in VISION_PROVIDERS}

DEFAULT_VISION_PROMPT = (
    "Please describe this image in detail. If it shows a slide or a blackboard, "
    "transcribe the readable text and formulas."
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


# --------------------------------------------------------------------------
# .env (credentials)
# --------------------------------------------------------------------------


def _parse_env(raw: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip()
        if len(value) >= 2 and value.startswith('"') and value.endswith('"'):
            value = value[1:-1].replace('\\"', '"').replace("\\\\", "\\")
        result[key] = value
    return result


def _quote(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def read_env() -> dict[str, str]:
    if not ENV_PATH.exists():
        return {}
    try:
        return _parse_env(ENV_PATH.read_text(encoding="utf-8"))
    except OSError:
        return {}


def write_env(updates: dict[str, str]) -> None:
    """Merge `updates` into the repo `.env`, preserving unrelated keys.

    A value of `None`-like empty string with `clear=True` removes the key; we
    keep it simple and only ever set or overwrite.
    """
    current = read_env()
    current.update({k: v for k, v in updates.items() if v is not None})
    lines = [
        "# Auto-generated by look-tongji-notes console",
        f"# Updated: {_now_iso()}",
    ]
    for key, value in current.items():
        lines.append(f"{key}={_quote(str(value))}")
    ENV_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


def console_user_config_dir() -> Path:
    """Return the per-user config directory, with an isolated test override."""
    override = os.environ.get("LOOK_TONGJI_CONSOLE_CONFIG_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    if os.environ.get("APPDATA"):
        base = Path(os.environ["APPDATA"])
    elif os.environ.get("XDG_CONFIG_HOME"):
        base = Path(os.environ["XDG_CONFIG_HOME"])
    else:
        base = Path.home() / ".config"
    return base / "look-tongji"


def console_settings_path() -> Path:
    return console_user_config_dir() / "console-settings.json"


def text_model_config_path() -> Path:
    return console_user_config_dir() / "text-model.json"


def _read_private_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        raise ValueError(f"本地设置文件无法读取：{path.name}") from error
    if not isinstance(payload, dict):
        raise ValueError(f"本地设置文件格式无效：{path.name}")
    return payload


def _atomic_private_json_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", newline="\n", dir=path.parent,
            prefix=f".{path.name}.", suffix=".tmp", delete=False,
        ) as stream:
            temp_path = Path(stream.name)
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        try:
            os.chmod(temp_path, 0o600)
        except OSError:
            pass
        os.replace(temp_path, path)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    finally:
        if temp_path is not None and temp_path.exists():
            temp_path.unlink(missing_ok=True)


def _merge_config(existing: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    merged = dict(existing)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_config(merged[key], value)
        elif isinstance(value, dict):
            merged[key] = _merge_config({}, value)
        else:
            merged[key] = value
    return merged


def read_console_settings() -> dict[str, Any]:
    return _read_private_json(console_settings_path())


def write_console_settings(updates: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(updates, dict):
        raise ValueError("控制台设置必须是 JSON 对象")
    path = console_settings_path()
    merged = _merge_config(_read_private_json(path), updates)
    _atomic_private_json_write(path, merged)
    return merged


def _validated_model_base_url(value: str) -> str:
    base_url = value.strip()
    parsed = urlsplit(base_url)
    hostname = (parsed.hostname or "").lower()
    local_hosts = {"localhost", "127.0.0.1", "::1"}
    if (
        parsed.scheme not in ("http", "https")
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or any(char.isspace() for char in base_url)
        or (parsed.scheme == "http" and hostname not in local_hosts)
    ):
        raise ValueError("模型地址必须使用 HTTPS；本机服务可使用 localhost HTTP，且不能包含凭据或查询参数")
    return base_url.rstrip("/")


def read_text_model() -> dict[str, Any]:
    return _read_private_json(text_model_config_path())


def public_text_model() -> dict[str, Any]:
    saved = read_text_model()
    provider = str(saved.get("provider") or "")
    model = str(saved.get("model") or "")
    try:
        base_url = _validated_model_base_url(str(saved.get("baseUrl") or "")) if saved.get("baseUrl") else ""
    except ValueError:
        base_url = ""
    key_set = bool(str(saved.get("apiKey") or ""))
    configured = bool(model and base_url and (key_set or provider == "ollama"))
    return {
        "provider": provider,
        "model": model,
        "base_url": base_url,
        "key_configured": key_set,
        "configured": configured,
        "providers": ["openai-compatible", "ollama"],
    }


def save_text_model(
    *,
    provider: str,
    model: str,
    base_url: str,
    api_key: str | None,
    clear_api_key: bool = False,
) -> dict[str, Any]:
    existing = read_text_model()
    provider = (provider or existing.get("provider") or "openai-compatible").strip().lower()
    if provider not in {"openai-compatible", "ollama"}:
        raise ValueError("未知的文本模型平台")
    model = (model if model is not None else existing.get("model") or "").strip()
    if len(model) > 200 or any(char in model for char in "\r\n\0"):
        raise ValueError("模型名称无效")
    base_url = (base_url if base_url is not None else existing.get("baseUrl") or "").strip()
    resolved_base = _validated_model_base_url(base_url) if base_url else ""
    if base_url and len(base_url) > 2048:
        raise ValueError("模型地址过长")
    old_key = str(existing.get("apiKey") or "")
    same_endpoint = provider == existing.get("provider") and resolved_base == existing.get("baseUrl")
    if clear_api_key:
        resolved_key = ""
    elif api_key and api_key.strip():
        resolved_key = api_key.strip()
    elif same_endpoint:
        resolved_key = old_key
    else:
        resolved_key = ""
    if len(resolved_key) > 4096 or "\0" in resolved_key:
        raise ValueError("文本模型密钥无效")

    payload = dict(existing)
    payload.update(
        {
            "provider": provider,
            "model": model,
            "baseUrl": resolved_base,
            "apiKey": resolved_key,
        }
    )
    _atomic_private_json_write(text_model_config_path(), payload)
    return public_text_model()


def public_credentials() -> dict[str, Any]:
    env = read_env()
    return {
        "env_path": str(ENV_PATH),
        "env_exists": ENV_PATH.exists(),
        "username_set": bool(env.get("TONGJI_USERNAME")),
        "password_set": bool(env.get("TONGJI_PASSWORD")),
        "configured": bool(env.get("TONGJI_USERNAME") and env.get("TONGJI_PASSWORD")),
        "gh_pages_repo": env.get("GH_PAGES_REPO", ""),
    }


# --------------------------------------------------------------------------
# Workspace config
# --------------------------------------------------------------------------


def public_workspace() -> dict[str, Any] | None:
    config = load_workspace_config()
    if config is None:
        return None
    root = config.workspace_root
    manifests: list[str] = []
    if root.exists():
        try:
            manifests = [str(p) for p in sorted(root.glob("raw/*/*/原始数据/manifest.json"))]
        except OSError:
            manifests = []
    return {
        "workspace_root": str(root),
        "owner_name": config.owner_name,
        "site_name": config.site_name,
        "config_path": str(config.config_path),
        "exists": root.exists(),
        "site_built": (root / "site" / "index.html").exists(),
        "lecture_count": len(manifests),
        "manifests": manifests[:500],
    }


def save_workspace(
    *,
    workspace_root: str,
    owner_name: str,
    site_name: str,
    migrate: bool = False,
) -> dict[str, Any]:
    """Persist the workspace config.

    `migrate=False` (the default) only repoints the config. Existing content is
    left exactly where it is. Passing `migrate=True` moves the generated trees
    and is only ever triggered by an explicit user confirmation in the UI.
    """
    target = Path(workspace_root).expanduser().resolve()
    owner = (owner_name or "").strip() or "WALKERKILLER"
    site = (site_name or "").strip() or f"{owner}的课程知识库"

    existing = load_workspace_config()
    moved: list[str] = []

    if existing is not None and migrate and existing.workspace_root != target:
        moved = [str(p) for p in migrate_workspace_root(existing.workspace_root, target)]

    target.mkdir(parents=True, exist_ok=True)
    config = WorkspaceConfig(
        workspace_root=target,
        owner_name=owner,
        site_name=site,
        # Reuse the upstream resolver so LOOK_TONGJI_CONFIG_PATH and
        # XDG_CONFIG_HOME are honoured, and the CLI reads back what we wrote.
        config_path=existing.config_path if existing is not None else _resolve_config_path(),
    )
    save_workspace_config(config)
    ensure_workspace_scaffold(config)

    return {"workspace_root": str(target), "owner_name": owner, "site_name": site, "moved": moved}


# --------------------------------------------------------------------------
# Vision model config
# --------------------------------------------------------------------------


def read_vision() -> dict[str, Any]:
    if not VISION_CONFIG_PATH.exists():
        return {}
    try:
        payload = json.loads(VISION_CONFIG_PATH.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        return {}


def public_vision() -> dict[str, Any]:
    raw = read_vision()
    models = raw.get("models") or []
    first = models[0] if isinstance(models, list) and models and isinstance(models[0], dict) else {}
    api_key = str(first.get("apiKey") or "").strip()
    key_env = str(first.get("apiKeyEnv") or "").strip()
    # Naming an environment variable is not proof the key exists: the variable
    # itself has to be present in the environment.
    key_available = bool(api_key) or bool(key_env and os.environ.get(key_env))
    configured = bool(str(first.get("model") or "").strip()) and key_available
    provider_id = str(first.get("consoleProvider") or "")
    if provider_id not in PROVIDER_BY_ID:
        provider_id = next((pid for pid, entry in PROVIDER_BY_ID.items()
                            if entry["baseUrl"] and entry["baseUrl"] == first.get("baseUrl")),
                           "custom" if first.get("baseUrl") else "")
    return {
        "configured": configured,
        "config_path": str(VISION_CONFIG_PATH),
        "provider": provider_id,
        "model": str(first.get("model") or ""),
        "base_url": str(first.get("baseUrl") or ""),
        "api_key_set": bool(api_key),
        "key_env": key_env,
        "providers": VISION_PROVIDERS,
    }


def save_vision(
    *,
    provider_id: str,
    model: str,
    api_key: str,
    base_url: str = "",
    api_format: str = "",
) -> dict[str, Any]:
    """Write `vision-support/config.json`.

    The API key lands in this file (which `.gitignore` excludes) rather than in
    the repository `.env`, so it is never part of a commit.
    """
    provider = PROVIDER_BY_ID.get(provider_id)
    if provider is None:
        raise ValueError(f"未知的视觉模型平台: {provider_id}")

    resolved_base = (base_url or provider["baseUrl"]).strip()
    if not resolved_base:
        raise ValueError("第三方平台必须填写 baseUrl")
    resolved_model = (model or "").strip()
    if not resolved_model:
        raise ValueError("必须填写模型名称")

    key = (api_key or "").strip()
    existing = read_vision()
    saved_models = existing.get("models") or []
    saved = saved_models[0] if isinstance(saved_models, list) and saved_models and isinstance(saved_models[0], dict) else {}
    resolved_format = api_format or provider["apiFormat"]
    if resolved_format not in ("openai", "google", "anthropic"):
        raise ValueError("不支持的 API 格式")
    same_endpoint = (saved.get("baseUrl") == resolved_base and saved.get("provider") == resolved_format
                     and saved.get("consoleProvider", provider_id) == provider_id)
    if not key and same_endpoint:
        key = str(saved.get("apiKey") or "")
    key_env = provider.get("keyEnv") or (str(saved.get("apiKeyEnv") or "") if same_endpoint else "")
    if provider.get("needsKey") and not key and not (key_env and os.environ.get(key_env)):
        raise ValueError("该平台需要 API Key")
    if provider_id == "ollama" and not key:
        key = "ollama"
    if provider_id == "lmstudio" and not key:
        key = "lm-studio"

    entry = {
        "name": f"{provider['name']} / {resolved_model}",
        "provider": resolved_format,
        "consoleProvider": provider_id,
        "model": resolved_model,
        "baseUrl": resolved_base,
        "apiKeyEnv": key_env,
        "apiKey": key,
        "timeout": 60000,
    }

    payload: dict[str, Any] = dict(existing) if existing else {}
    payload["models"] = [entry]
    payload.setdefault("defaultPrompt", DEFAULT_VISION_PROMPT)
    payload.setdefault("proxy", {"urls": []})
    payload.setdefault("maxImageSize", 20971520)
    payload.setdefault(
        "supportedFormats", ["jpg", "jpeg", "png", "gif", "webp", "bmp", "svg"]
    )

    VISION_DIR.mkdir(parents=True, exist_ok=True)
    VISION_CONFIG_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    try:
        os.chmod(VISION_CONFIG_PATH, 0o600)
    except OSError:
        pass

    return {"provider": provider_id, "model": resolved_model, "base_url": resolved_base}


def vision_node_available() -> bool:
    return shutil.which("node") is not None
