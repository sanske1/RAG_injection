#!/usr/bin/env python3
"""把 sources/ 下的上游文档切成 RAG 用的块，输出 chunks.jsonl。

切块策略：
  - Markdown：按标题层级（H1–H3）切，标题路径作为 title；块过长再滑窗切
  - 其它（yaml / txt / GTFOBins 的无扩展名文件）：整体作为一块，过长再滑窗
每块带 source / path / title / text，供向量化和检索溯源。

只做切分，不改动语料。
"""
import json
import os
import re
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

KB_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = KB_ROOT / "sources"
OUT_FILE = KB_ROOT / "rag" / "chunks.jsonl"

# 自建经验（本项目自己的实战复盘）性质与上游权威原文不同，
# 单独切一份、入独立集合，绝不混进 pentest_kb。
SELF_SOURCE = "自建经验"
SELF_OUT_FILE = KB_ROOT / "rag" / "chunks_lessons.jsonl"

EXCLUDE_SOURCES = {"SecLists"}          # 纯爆破字典，对 RAG 无价值
EXCLUDE_DIRS = {".git", "node_modules", ".github", "__pycache__", ".vscode", ".idea", "site"}
DOC_EXT = {".md", ".markdown", ".rst", ".txt", ".yml", ".yaml"}
NOEXT_DIRS = {"_gtfobins"}              # GTFOBins 每个二进制一个无扩展名文件

# 仓库治理类文件，不是渗透知识（AGENTS.md 甚至是在指挥 AI 干活），滤掉
SKIP_NAMES = {
    "agents.md", "contributing.md", "code_of_conduct.md", "security.md",
    "changelog.md", "notice.md", "funding.json", "license",
}
# 只有「仓库根目录」的 README 才是仓库介绍；子目录里的 README 往往是正文
# （例如 PEASS-ng/linPEAS/README.md 就是 linPEAS 的文档），不能误删
SKIP_ROOT_ONLY = {"readme.md"}

MAX_FILE_BYTES = 2_000_000
MAX_CHUNK = 1400                        # 单块字符上限
MIN_CHUNK = 80                          # 短于此的块丢弃（多为空标题/噪声）
OVERLAP = 200                           # 滑窗重叠
HEADING_RE = re.compile(r"^(#{1,3})\s+(.*)$")
FRONTMATTER_RE = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.DOTALL)

# 词表过滤：PayloadsAllTheThings 的 Intruder/ 等目录里躺着的是纯 fuzz 字典
# （dotdotpwn.txt 一个文件就有 1600+ 块，占全库 5%）。它们没有语义、却是向量
# 空间的「吸引子」，会劫持短查询。与 SecLists 同理——喂工具用的，不是知识。
# 只对 .txt 判定：正经文档即使代码多（如 windows-amsi-bypass.md，比值 0.019）
# 也是 .md，不会被误伤。
_WORD = re.compile(r"[A-Za-z]{3,}")
WORD_RATIO_MIN = 0.04


def prose_ratio(text):
    """每字符里有多少个 >=3 字母的英文词。散文约 0.08~0.13，fuzz 字典 < 0.04。"""
    return len(_WORD.findall(text)) / max(1, len(text))


def read_text(path: Path):
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES or b"\x00" in raw[:8192]:
        return None
    for enc in ("utf-8", "utf-8-sig", "gbk", "latin-1"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+\n", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def window(text: str):
    """把过长文本按 MAX_CHUNK 滑窗切开。"""
    text = text.strip()
    if len(text) <= MAX_CHUNK:
        return [text]
    out, n, i = [], len(text), 0
    while i < n:
        end = min(i + MAX_CHUNK, n)
        piece = text[i:end]
        # 尽量在换行处收尾，避免把一行切断
        if end < n:
            nl = piece.rfind("\n")
            if nl > MAX_CHUNK * 0.6:
                piece = piece[:nl]
        out.append(piece.strip())
        if end >= n:            # 已到尾部，收工（否则会原地打转）
            break
        i += max(1, len(piece) - OVERLAP)
    return [p for p in out if p]


def split_markdown(text: str):
    """按 H1–H3 标题切段，返回 [(title, body), ...]。"""
    text = FRONTMATTER_RE.sub("", text)
    lines = text.split("\n")
    sections = []
    stack = []          # [(level, text)]
    buf = []
    title = ""

    def flush():
        body = tidy("\n".join(buf))
        if body:
            sections.append((title, body))

    in_fence = False
    for line in lines:
        bare = line.lstrip()
        if bare.startswith("```") or bare.startswith("~~~"):
            in_fence = not in_fence
            buf.append(line)
            continue
        m = None if in_fence else HEADING_RE.match(line)
        if m:
            flush()
            buf = []
            level = len(m.group(1))
            head = m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, head))
            title = " > ".join(h for _, h in stack)
        else:
            buf.append(line)
    flush()
    return sections


def iter_candidates(repo: Path):
    """列出所有可能是文档的文件（还没做治理文件过滤）。"""
    for root, dirs, files in os.walk(repo):
        dirs[:] = [d for d in dirs if d not in EXCLUDE_DIRS]
        for fn in files:
            p = Path(root) / fn
            rel = p.relative_to(repo)
            if p.suffix.lower() not in DOC_EXT and not (
                not p.suffix and NOEXT_DIRS.intersection(rel.parts)
            ):
                continue
            yield p, rel


def collect_files(repo: Path):
    cands = list(iter_candidates(repo))
    # 只有当仓库里还有别的文档时，根 README 才当作仓库介绍滤掉；
    # 像 Active-Directory-Exploitation-Cheat-Sheet 这种全部内容就在根 README 里的，必须保留
    has_inner = any(
        len(rel.parts) > 1 and p.suffix.lower() in (".md", ".markdown")
        for p, rel in cands
    )
    for p, rel in cands:
        name = p.name.lower()
        if name in SKIP_NAMES:
            continue
        if has_inner and len(rel.parts) == 1 and name in SKIP_ROOT_ONLY:
            continue
        yield p, rel


def emit(repos, out_file):
    """把给定来源切块写入 out_file。返回 (总块数, [(来源, 文件数, 块数)], 丢弃的字典文件)。"""
    total_chunks = 0
    dropped = []
    stats = []
    with out_file.open("w", encoding="utf-8") as fh:
        for repo in repos:
            n_chunks = n_files = 0
            for path, rel in collect_files(repo):
                text = read_text(path)
                if not text or not text.strip():
                    continue
                text = tidy(text)
                if path.suffix.lower() == ".txt" and prose_ratio(text) < WORD_RATIO_MIN:
                    dropped.append((repo.name, rel.as_posix(), prose_ratio(text)))
                    continue
                if path.suffix.lower() in (".md", ".markdown"):
                    sections = split_markdown(text) or [("", text)]
                else:
                    sections = [(path.stem, text)]
                for title, body in sections:
                    for piece in window(body):
                        if len(piece) < MIN_CHUNK:
                            continue
                        rec = {
                            "source": repo.name,
                            "path": rel.as_posix(),
                            "title": title,
                            "text": piece,
                        }
                        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
                        n_chunks += 1
                n_files += 1
            total_chunks += n_chunks
            stats.append((repo.name, n_files, n_chunks))
            print(f"[ok]   {repo.name}: {n_files} files -> {n_chunks} chunks")

    print(f"\ntotal: {total_chunks} chunks -> {out_file}")
    print(f"size : {out_file.stat().st_size / 1e6:.1f} MB")
    if dropped:
        print(f"\nwordlist files dropped ({len(dropped)}):")
        for src, p, r in sorted(dropped, key=lambda x: x[2]):
            print(f"   ratio={r:.3f}  {src}/{p}")
    return total_chunks, stats, dropped


def main():
    if not SRC_DIR.is_dir():
        sys.exit(f"sources not found: {SRC_DIR}")
    OUT_FILE.parent.mkdir(parents=True, exist_ok=True)

    repos = sorted(p for p in SRC_DIR.iterdir()
                   if p.is_dir() and p.name not in EXCLUDE_SOURCES)
    upstream = [r for r in repos if r.name != SELF_SOURCE]
    selfmade = [r for r in repos if r.name == SELF_SOURCE]

    print(f"=== 上游语料 -> {OUT_FILE.name} ===")
    emit(upstream, OUT_FILE)

    if selfmade:
        print(f"\n=== 自建经验 -> {SELF_OUT_FILE.name}（独立集合，不混入主库）===")
        emit(selfmade, SELF_OUT_FILE)
    else:
        print(f"\n（未找到 sources/{SELF_SOURCE}/，跳过自建经验）")


if __name__ == "__main__":
    main()
