"""Provider boundary for cached-audio transcription.

The interface is deliberately independent from media acquisition: providers
receive an existing audio path and never own or delete that cache file.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol


Progress = Callable[[dict[str, Any]], None]
CancelCheck = Callable[[], bool]


class ASRCancelled(RuntimeError):
    """Raised when a transcription is cancelled by its owning task."""


class ProviderUnavailable(RuntimeError):
    """Raised when the selected provider is not fully configured."""


class ASRProvider(Protocol):
    provider_id: str

    def transcribe(
        self, audio: str | Path, progress: Progress, cancel: CancelCheck
    ) -> tuple[str, str, list[dict[str, Any]]]: ...


@dataclass
class BcutASRProvider:
    """Adapter for the already integrated BcutASR client."""

    client: Any
    provider_id: str = "bcutasr"

    def transcribe(self, audio: str | Path, progress: Progress, cancel: CancelCheck):
        if cancel():
            raise ASRCancelled("识别已取消")
        return self.client.transcribe_file(
            str(audio), progress=progress, cancel=cancel
        )


def provider_catalog() -> list[dict[str, str]]:
    """Return honest availability metadata without downloading or probing a model."""
    return [
        {
            "id": "bcutasr",
            "name": "BcutASR",
            "availability": "unknown",
            "upload_target": "BcutASR 云端识别服务",
        },
        {
            "id": "local_whisper",
            "name": "本地 Whisper",
            "availability": "unavailable",
            "upload_target": "本机",
            "reason": "本地识别运行路径尚未完成集成验证",
        },
    ]


def resolve_provider(
    provider_id: str,
    *,
    bcut_client: Any = None,
) -> ASRProvider:
    if provider_id == "bcutasr":
        if bcut_client is None:
            raise ProviderUnavailable("BcutASR 客户端尚未初始化")
        return BcutASRProvider(bcut_client)
    if provider_id == "local_whisper":
        raise ProviderUnavailable("本地 Whisper 不可用：识别适配器尚未完成集成验证")
    raise ProviderUnavailable(f"未知的字幕识别服务：{provider_id}")
