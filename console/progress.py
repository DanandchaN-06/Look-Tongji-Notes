"""Progress contract: measured counts stay separate from unknown stage progress."""
from __future__ import annotations
import json
from typing import Any

PREFIX = "LOOK_PROGRESS "
TRACKS = {"transcript": "字幕生成流程", "slides": "课件", "batch": "批量课程", "workspace": "资料库"}
STAGES = {"prepare": "准备任务", "auth": "验证登录", "download": "下载素材", "extract": "抽取音频",
          "recognize": "云端识别字幕", "write": "保存素材", "retry": "等待重试", "import": "导入补充材料",
          "index": "同步课程内容", "build": "生成阅读网页", "serve": "启动本地预览", "done": "此步骤完成"}
STAGES.update(download_media="下载录课视频（用于提取音频）", stream_audio="下载并提取录课音频")
STAGES.update(reuse_transcript="复用已生成字幕，跳过下载和识别", reuse_audio="复用已保存音频，继续识别", reuse_media="复用已下载视频，继续抽取音频")
STAGES['waiting_cache'] = "其他任务正在处理同一课次，等待复用结果"
STAGES['check_tool'] = "检查音频提取工具是否可运行"
STAGES['retry_media'] = "续传中断的视频分片"


def parse_event(line: str) -> dict[str, Any] | None:
    if not line.startswith(PREFIX):
        return None
    try:
        raw = json.loads(line[len(PREFIX):])
    except (ValueError, TypeError):
        return None
    if not isinstance(raw, dict) or raw.get("track") not in TRACKS or raw.get("stage") not in STAGES:
        return None
    event = {"track": raw["track"], "stage": raw["stage"], "label": STAGES[raw["stage"]],
             "completed": None, "total": None, "failed": 0, "percent": None, "unit": ""}
    completed, total = raw.get("completed"), raw.get("total")
    if type(completed) is int and type(total) is int and 0 <= completed <= total and total > 0:
        event.update(completed=completed, total=total, percent=round(completed * 100 / total, 1))
        event["unit"] = raw.get("unit") if raw.get("unit") in ("张", "节", "字节", "项") else "项"
        failed = raw.get("failed", 0)
        event["failed"] = failed if type(failed) is int and 0 <= failed <= completed else 0
        if event['track'] == 'transcript' and event['stage'] == 'download' and event['unit'] == '字节':
            event['label'] = STAGES['download_media']
    return event


def snapshot(kind: str, status: str, tracks: dict[str, dict[str, Any]], elapsed: int, cancelling: bool = False) -> dict[str, Any]:
    rows = [{"name": TRACKS[key], **value} for key, value in tracks.items()]
    primary = tracks.get("batch") if kind == "batch" else tracks.get("slides") if kind == "slide" else tracks.get("transcript") if kind == "transcribe" else tracks.get("workspace")
    percent = primary.get("percent") if primary else None
    label = primary.get("label") if primary else "正在准备任务"
    if kind == "note":
        label = "字幕与课件分别处理，下方显示各自阶段"
        percent = None
    if status == "queued":
        label, percent = "排队等待，前面的任务完成后开始", None
    elif status == "succeeded":
        label, percent = ("预览任务已结束" if kind == "serve" else "任务已结束，请查看结果"), 100
    elif status == "failed":
        label = "任务失败，请查看日志与已保留的素材"
    elif status == "cancelled":
        label = "任务已取消，已完成的文件保留"
    elif cancelling:
        label = "正在取消；批量任务需等待当前节次保存"
    return {"label": label, "percent": percent, "tracks": rows, "elapsed_seconds": max(0, elapsed),
            "mode": "determinate" if percent is not None else "indeterminate" if status == "running" else "idle"}
