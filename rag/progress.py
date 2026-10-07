#!/usr/bin/env python3
"""看入库进度。随时执行：python rag/progress.py """
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
CHUNKS = HERE / "chunks.jsonl"
STATE = HERE / ".ingest_state.json"

total = 0
with open(CHUNKS, encoding="utf-8") as f:
    for _ in f:
        total += 1

done = 0
if STATE.exists():
    try:
        done = int(json.loads(STATE.read_text(encoding="utf-8")).get("inserted", 0))
    except Exception:
        pass

pct = (done / total * 100) if total else 0.0
width = 40
filled = int(pct / 100 * width)
bar = "=" * filled + "-" * (width - filled)
print(f"[{bar}] {done:,}/{total:,}  {pct:.1f}%")
if done >= total:
    print("入库完成。")
