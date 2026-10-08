"""Per-course checkpoint identity, shared by the CLI and console."""
import hashlib
from pathlib import Path


def path_for(root: Path, course_id: str) -> Path:
    key = hashlib.sha256(str(course_id).encode("utf-8")).hexdigest()[:20]
    return root / "state" / "batches" / f"{key}.json"
