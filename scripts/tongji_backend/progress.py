"""Optional machine progress stream for local consoles; normal CLI stays unchanged."""
import json
import os


def emit(track: str, stage: str, **counts) -> None:
    if os.environ.get("LOOK_TONGJI_CONSOLE_EVENTS") == "1":
        print("LOOK_PROGRESS " + json.dumps({"track": track, "stage": stage, **counts}, ensure_ascii=False), flush=True)
