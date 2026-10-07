#!/usr/bin/env python3
"""把 chunks.jsonl 向量化后写入 Milvus。

用法：
    python ingest.py --reset --limit 200    # 冒烟测试，只灌前 200 块
    python ingest.py --reset                # 全量重建（主库 pentest_kb）
    python ingest.py                        # 续跑（从上次中断处继续）

自建经验入独立集合（不混入上游权威原文）：
    python ingest.py --chunks chunks_lessons.jsonl --collection pentest_lessons --reset

进度记在 rag/.ingest_state.json（非主库则记 .ingest_state_<集合名>.json），
中断后重跑会接着走，不会重复灌。
"""
import argparse
import json
import sys
import time
from pathlib import Path

import kb

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = Path(__file__).resolve().parent
CHUNKS = HERE / "chunks.jsonl"
BATCH = 64


def state_file(collection):
    """每个集合一份续跑状态；主库沿用旧文件名，不破坏已有进度。"""
    if collection == kb.COLLECTION:
        return HERE / ".ingest_state.json"
    return HERE / f".ingest_state_{collection}.json"


def load(path):
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                yield json.loads(line)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--reset", action="store_true", help="删掉集合重建")
    ap.add_argument("--limit", type=int, default=0, help="只灌前 N 块（冒烟测试）")
    ap.add_argument("--chunks", default=str(CHUNKS), help="要灌的 chunks jsonl 路径")
    ap.add_argument("--collection", default=kb.COLLECTION, help="目标 Milvus 集合名")
    args = ap.parse_args()

    chunks = Path(args.chunks)
    if not chunks.exists():
        sys.exit(f"chunks 文件不存在：{chunks}")
    state = state_file(args.collection)

    client = kb.get_client()
    kb.ensure_collection(client, reset=args.reset, collection=args.collection)
    if args.reset and state.exists():
        state.unlink()

    records = []
    for i, rec in enumerate(load(chunks)):
        if args.limit and i >= args.limit:
            break
        records.append(rec)
    total = len(records)

    start = 0
    if state.exists():
        start = min(json.loads(state.read_text(encoding="utf-8")).get("inserted", 0), total)

    print(f"chunks     = {chunks.name}")
    print(f"collection = {args.collection}")
    print(f"model      = {kb.EMBED_MODEL}  dim={kb.DIM}")
    print(f"chunks     = {total}   resume from {start}")
    if start >= total:
        print("已经灌完，无需重跑。要重建加 --reset")
        return

    inserted = start
    t0 = time.time()
    nbatch = 0

    def flush(buf):
        nonlocal inserted, nbatch
        if not buf:
            return
        vecs = kb.embed([kb.embed_text(r) for r in buf])
        if len(vecs) != len(buf):
            raise RuntimeError(f"embed 数量不匹配: {len(vecs)} != {len(buf)}")
        entities = [{
            "vector": v,
            "source": r["source"],
            "path": r["path"],
            "title": (r.get("title") or "")[:500],
            "text": r["text"],
        } for r, v in zip(buf, vecs)]
        client.insert(args.collection, entities)
        inserted += len(entities)
        state.write_text(json.dumps({"inserted": inserted}), encoding="utf-8")
        nbatch += 1
        if nbatch % 10 == 0 or inserted >= total:
            el = time.time() - t0
            rate = (inserted - start) / el if el > 0 else 0
            eta = (total - inserted) / rate if rate > 0 else 0
            print(f"  {inserted}/{total}  {rate:.0f}/s  已用 {el/60:.1f}min  预计还需 {eta/60:.1f}min",
                  flush=True)

    buf = []
    for rec in records[start:]:
        buf.append(rec)
        if len(buf) >= BATCH:
            flush(buf)
            buf = []
    flush(buf)

    try:
        client.flush(args.collection)
    except Exception:
        pass
    print(f"\n完成：{inserted} 块已入库到 {args.collection}，用时 {(time.time()-t0)/60:.1f} 分钟")


if __name__ == "__main__":
    main()
