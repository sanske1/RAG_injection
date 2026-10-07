#!/usr/bin/env python3
"""实时看入库进度，每 3 秒原地刷新一行。Ctrl+C 退出。

    python rag/watch.py
"""
import json
import sys
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
STATE = HERE / ".ingest_state.json"

total = 0
with open(HERE / "chunks.jsonl", encoding="utf-8") as f:
    for _ in f:
        total += 1

t0 = time.time()
last_done, last_t = 0, t0
width = 40

while True:
    done = 0
    if STATE.exists():
        try:
            done = int(json.loads(STATE.read_text(encoding="utf-8")).get("inserted", 0))
        except Exception:
            pass

    now = time.time()
    rate = (done - last_done) / (now - last_t) if now > last_t and done >= last_done else 0
    last_done, last_t = done, now

    pct = (done / total * 100) if total else 0.0
    filled = int(pct / 100 * width)
    bar = "=" * filled + "-" * (width - filled)
    eta = (total - done) / rate / 60 if rate > 0 else 0

    sys.stdout.write(
        f"\r[{bar}] {done:,}/{total:,}  {pct:5.1f}%  {rate:4.0f} 块/秒  还需 {eta:4.1f} 分钟   "
    )
    sys.stdout.flush()

    if done >= total:
        print(f"\n入库完成，用时 {(now - t0) / 60:.1f} 分钟。")
        break
    time.sleep(3)
