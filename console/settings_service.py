"""Unified settings validation and secret-safe settings revisions."""

from __future__ import annotations

import copy
import json
import re
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from . import config_store
from .asr_providers import provider_catalog
from .storage import ConsoleStorage


DEFAULT_PREFERENCES: dict[str, Any] = {
    "asr": {"provider": "bcutasr"},
    "note": {"route": "external_agent", "style": "standard", "include_timeline": True},
    "slides": {
        "concurrency": 4,
        "retries": 3,
        "timeout_seconds": 30,
        "allow_without_screenshots": True,
    },
    "tasks": {"course_parallelism": 2, "asr_parallelism": 1, "retry_limit": 3},
    "reader": {"auto_update": True},
}

_ALLOWED_FIELDS = {
    "account": {"username", "password", "workspace_root", "owner_name", "site_name"},
    "asr": {"provider"},
    "note": {"route", "style", "include_timeline"},
    "text_model": {"provider", "model", "base_url", "api_key", "clear_api_key"},
    "vision_model": {"provider", "model", "base_url", "api_key"},
    "slides": {"concurrency", "retries", "timeout_seconds", "allow_without_screenshots"},
    "tasks": {"course_parallelism", "asr_parallelism", "retry_limit"},
    "reader": {"auto_update", "publish_repository"},
}


def _merge(base: dict[str, Any], updates: dict[str, Any]) -> dict[str, Any]:
    result = copy.deepcopy(base)
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _require_object(value: Any, field: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{field} 必须是对象")
    return value


def _validate_fields(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    unknown_groups = set(payload) - set(_ALLOWED_FIELDS)
    if unknown_groups:
        raise ValueError(f"未知设置分组: {', '.join(sorted(unknown_groups))}")
    groups: dict[str, dict[str, Any]] = {}
    for name, raw in payload.items():
        group = _require_object(raw, name)
        unknown = set(group) - _ALLOWED_FIELDS[name]
        if unknown:
            raise ValueError(f"{name} 包含未知字段: {', '.join(sorted(unknown))}")
        groups[name] = group

    asr = groups.get("asr", {})
    if "provider" in asr and asr["provider"] != "bcutasr":
        raise ValueError("当前只支持 BcutASR 字幕识别服务")

    note = groups.get("note", {})
    if note.get("route", "external_agent") not in {"external_agent", "model"}:
        raise ValueError("笔记路线必须是 external_agent 或 model")
    if note.get("style", "standard") not in {"standard", "dialogue"}:
        raise ValueError("笔记风格必须是 standard 或 dialogue")
    if "include_timeline" in note and not isinstance(note["include_timeline"], bool):
        raise ValueError("时间轴设置必须是布尔值")

    slides = groups.get("slides", {})
    _integer_range(slides, "concurrency", 1, 16)
    _integer_range(slides, "retries", 0, 10)
    _integer_range(slides, "timeout_seconds", 5, 300)
    _boolean(slides, "allow_without_screenshots")

    tasks = groups.get("tasks", {})
    _integer_range(tasks, "course_parallelism", 1, 4)
    if "asr_parallelism" in tasks:
        value = tasks["asr_parallelism"]
        if isinstance(value, bool) or not isinstance(value, int) or value != 1:
            raise ValueError("首版字幕识别并发固定为 1")
    _integer_range(tasks, "retry_limit", 1, 3)

    reader = groups.get("reader", {})
    _boolean(reader, "auto_update")
    if "publish_repository" in reader:
        repository = _string(reader["publish_repository"], "发布仓库", max_length=200)
        if repository and not re.fullmatch(r"[A-Za-z0-9-]+/[A-Za-z0-9._-]+", repository):
            raise ValueError("发布仓库格式应为 owner/repository")
        reader["publish_repository"] = repository

    account = groups.get("account", {})
    for field in ("username", "password", "workspace_root", "owner_name", "site_name"):
        if field in account:
            account[field] = _string(
                account[field], field,
                max_length=4096 if field == "workspace_root" else (2048 if field == "password" else 512),
            )
    if "workspace_root" in account and "\0" in account["workspace_root"]:
        raise ValueError("保存目录无效")

    for name in ("text_model", "vision_model"):
        model = groups.get(name, {})
        for field in ("provider", "model", "base_url"):
            if field in model:
                model[field] = _string(model[field], f"{name}.{field}", max_length=2048)
        if "api_key" in model:
            model["api_key"] = _string(model["api_key"], f"{name}.api_key", max_length=4096)
        if name == "text_model" and "clear_api_key" in model:
            _boolean(model, "clear_api_key")
        if name == "text_model" and model.get("provider"):
            if model["provider"] not in {"openai-compatible", "ollama"}:
                raise ValueError("未知的文本模型平台")
        if name == "vision_model" and model.get("provider"):
            if model["provider"] not in config_store.PROVIDER_BY_ID:
                raise ValueError("未知的视觉模型平台")
        if model.get("base_url"):
            config_store._validated_model_base_url(model["base_url"])
    return groups


def _integer_range(group: dict[str, Any], key: str, minimum: int, maximum: int) -> None:
    if key not in group:
        return
    value = group[key]
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{key} 必须在 {minimum} 到 {maximum} 之间")


def _boolean(group: dict[str, Any], key: str) -> None:
    if key in group and not isinstance(group[key], bool):
        raise ValueError(f"{key} 必须是布尔值")


def _string(value: Any, label: str, *, max_length: int) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} 必须是文本")
    value = value.strip()
    if len(value) > max_length or any(char in value for char in "\0\r\n"):
        raise ValueError(f"{label} 长度无效")
    return value


class SettingsService:
    def __init__(self, storage: ConsoleStorage) -> None:
        self.storage = storage

    def get(self) -> dict[str, Any]:
        preferences = _merge(DEFAULT_PREFERENCES, config_store.read_console_settings())
        public_preferences = {
            group: {
                key: value
                for key, value in preferences[group].items()
                if key in DEFAULT_PREFERENCES[group]
            }
            for group in DEFAULT_PREFERENCES
        }

        environment = config_store.read_env()
        workspace = config_store.load_workspace_config()
        text_model = config_store.public_text_model()
        vision_raw = config_store.public_vision()
        vision = {
            "configured": bool(vision_raw.get("configured")),
            "provider": str(vision_raw.get("provider") or ""),
            "model": str(vision_raw.get("model") or ""),
            "base_url": self._safe_existing_base_url(vision_raw.get("base_url")),
            "key_configured": bool(vision_raw.get("api_key_set"))
            or bool(vision_raw.get("key_env") and os_environment_has(vision_raw["key_env"])),
            "providers": vision_raw.get("providers", []),
        }
        latest = self.storage.latest_settings_revision()
        return {
            "revision_id": latest.id if latest else None,
            "account": {
                "username": str(environment.get("TONGJI_USERNAME") or ""),
                "username_set": bool(environment.get("TONGJI_USERNAME")),
                "password_set": bool(environment.get("TONGJI_PASSWORD")),
                "workspace_root": str(workspace.workspace_root) if workspace else "",
                "owner_name": workspace.owner_name if workspace else "",
                "site_name": workspace.site_name if workspace else "",
            },
            "asr": {
                **public_preferences["asr"],
                "availability": "unknown",
                "upload_target": "BcutASR 云端识别服务",
                "providers": provider_catalog(),
            },
            "note": public_preferences["note"],
            "text_model": text_model,
            "vision_model": vision,
            "slides": public_preferences["slides"],
            "tasks": public_preferences["tasks"],
            "reader": {
                **public_preferences["reader"],
                "publish_repository": str(environment.get("GH_PAGES_REPO") or ""),
            },
        }

    def test_text_model(self) -> dict[str, Any]:
        saved = config_store.read_text_model()
        settings = config_store.public_text_model()
        if not settings["configured"]:
            raise ValueError("请先保存完整的文本模型配置")
        base_url = settings["base_url"]
        if settings["provider"] == "ollama":
            base_url = re.sub(r"/v1/?$", "", base_url.rstrip("/"))
            endpoint = f"{base_url}/api/tags"
        else:
            endpoint = f"{base_url}/models"
        headers = {"Accept": "application/json"}
        api_key = str(saved.get("apiKey") or "").strip()
        if api_key and settings["provider"] != "ollama":
            headers["Authorization"] = f"Bearer {api_key}"
        request = Request(endpoint, headers=headers, method="GET")

        class NoRedirect(HTTPRedirectHandler):
            def redirect_request(self, req, fp, code, msg, response_headers, new_url):
                return None

        try:
            response = build_opener(NoRedirect()).open(request, timeout=12)
            with response:
                payload = json.load(response)
            list_key = "models" if settings["provider"] == "ollama" else "data"
            if not isinstance(payload, dict) or not isinstance(payload.get(list_key), list):
                return {"success": False, "message": "模型服务响应格式不符合模型列表接口。"}
        except HTTPError as error:
            error.close()
            return {"success": False, "message": f"模型服务返回 HTTP {error.code}。"}
        except (URLError, TimeoutError, OSError, ValueError):
            return {"success": False, "message": "无法连接模型服务，或服务没有返回有效 JSON。"}
        return {"success": True, "message": "模型列表接口连接成功；本次没有发送生成请求。"}

    def save(self, payload: dict[str, Any]) -> dict[str, Any]:
        groups = _validate_fields(_require_object(payload, "设置"))
        if groups.get("note", {}).get("route") == "model" and not self._text_model_will_be_configured(groups):
            raise ValueError("选择文本模型路线前，请先配置可用的文本模型和密钥")
        preferences_updates = {
            group: values
            for group, values in groups.items()
            if group in {"asr", "note", "slides", "tasks", "reader"}
        }
        if preferences_updates:
            # Detect unreadable existing preferences before changing any other config files.
            config_store.read_console_settings()
        self._validate_workspace_update(groups.get("account", {}))

        with self.storage.transaction():
            self._save_models(groups)
            self._save_account(groups.get("account", {}))
            if preferences_updates:
                config_store.write_console_settings(preferences_updates)
            public = self.get()
            revision = self.storage.create_settings_revision(self._revision_snapshot(public))
        public["revision_id"] = revision.id
        return {"revision_id": revision.id, "settings": public}

    @staticmethod
    def _validate_workspace_update(account: dict[str, Any]) -> None:
        workspace_fields = {"workspace_root", "owner_name", "site_name"}
        if workspace_fields.intersection(account) and not (
            account.get("workspace_root") or config_store.load_workspace_config()
        ):
            raise ValueError("首次设置保存目录时必须填写 workspace_root")

    @staticmethod
    def _text_model_will_be_configured(groups: dict[str, dict[str, Any]]) -> bool:
        existing = config_store.read_text_model()
        submitted = groups.get("text_model", {})
        provider = str(submitted.get("provider") or existing.get("provider") or "openai-compatible")
        model = str(submitted.get("model") or existing.get("model") or "").strip()
        base_url = str(submitted.get("base_url") or existing.get("baseUrl") or "").strip().rstrip("/")
        same_endpoint = provider == existing.get("provider") and base_url == str(existing.get("baseUrl") or "").rstrip("/")
        if submitted.get("clear_api_key"):
            has_key = False
        elif str(submitted.get("api_key") or "").strip():
            has_key = True
        else:
            has_key = same_endpoint and bool(str(existing.get("apiKey") or ""))
        return bool(model and base_url and (has_key or provider == "ollama"))

    def _save_account(self, account: dict[str, Any]) -> None:
        updates: dict[str, str] = {}
        if account.get("username"):
            updates["TONGJI_USERNAME"] = account["username"]
        if account.get("password"):
            updates["TONGJI_PASSWORD"] = account["password"]
        if updates:
            config_store.write_env(updates)

        if "workspace_root" in account or "owner_name" in account or "site_name" in account:
            existing = config_store.load_workspace_config()
            workspace_root = account.get("workspace_root") or (
                str(existing.workspace_root) if existing else ""
            )
            if not workspace_root:
                raise ValueError("首次设置保存目录时必须填写 workspace_root")
            config_store.save_workspace(
                workspace_root=workspace_root,
                owner_name=account.get("owner_name") or (existing.owner_name if existing else ""),
                site_name=account.get("site_name") or (existing.site_name if existing else ""),
                migrate=False,
            )

    def _save_models(self, groups: dict[str, dict[str, Any]]) -> None:
        text = groups.get("text_model")
        if text is not None:
            config_store.save_text_model(
                provider=text.get("provider", ""),
                model=text.get("model", ""),
                base_url=text.get("base_url", ""),
                api_key=text.get("api_key"),
                clear_api_key=text.get("clear_api_key", False),
            )

        vision = groups.get("vision_model")
        if vision is not None:
            if any(key in vision for key in ("provider", "model", "base_url", "api_key")):
                existing = config_store.public_vision()
                config_store.save_vision(
                    provider_id=vision.get("provider") or existing.get("provider") or "custom",
                    model=vision.get("model") or existing.get("model") or "",
                    api_key=vision.get("api_key", ""),
                    base_url=vision.get("base_url") or existing.get("base_url") or "",
                )

        reader = groups.get("reader", {})
        if "publish_repository" in reader:
            config_store.write_env({"GH_PAGES_REPO": reader["publish_repository"]})

    @staticmethod
    def _safe_existing_base_url(value: Any) -> str:
        try:
            return config_store._validated_model_base_url(str(value or "")) if value else ""
        except ValueError:
            return ""

    @staticmethod
    def _revision_snapshot(public: dict[str, Any]) -> dict[str, Any]:
        return {
            "asr": {"provider": public["asr"]["provider"]},
            "note": copy.deepcopy(public["note"]),
            "text_model": {
                key: public["text_model"][key]
                for key in ("provider", "model", "base_url", "configured", "key_configured")
            },
            "vision_model": {
                key: public["vision_model"][key]
                for key in ("provider", "model", "base_url", "configured", "key_configured")
            },
            "slides": copy.deepcopy(public["slides"]),
            "tasks": copy.deepcopy(public["tasks"]),
            "reader": {
                "auto_update": public["reader"]["auto_update"],
                "publish_repository": public["reader"]["publish_repository"],
            },
        }


def os_environment_has(name: str) -> bool:
    import os

    return bool(os.environ.get(name))
