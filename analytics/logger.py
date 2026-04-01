import json
import os
from datetime import datetime, timezone

def ensure_dir(path: str) -> None:
    os.makedirs(path, exist_ok=True)

def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def append_jsonl(path: str, payload: dict) -> None:
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")