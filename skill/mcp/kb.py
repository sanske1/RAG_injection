"""知识库公共层：Ollama 向量化 + Milvus 连接与检索。

ingest.py 和 mcp_server.py 都从这里取，保证两边的模型、集合、度量方式一致。

检索采用三通道并行 + 取最高相似度融合。原因：语料正文几乎全是英文，而用户的
查询常是「英文术语 + 中文修饰词」。实测发现修饰词会把 bge-m3 的查询向量带偏——
同一个术语，光秃秃查是 rank 1，加几个中文词就完全找不到。所以同一次检索跑三路：

    通道1  原查询          常规情形
    通道2  纯英文术语      救 DCSync / pass the hash 这类被中文稀释的术语
    通道3  纯中文实词      救 命令注入 这类被英文词稀释的中文术语

每个块取三路中最高的余弦相似度作为最终得分（详见 _fuse）。
"""
import json
import os
import re
import urllib.request

MILVUS_URI = os.environ.get("PENTEST_KB_MILVUS_URI", "http://localhost:19530")
OLLAMA_HOST = os.environ.get("PENTEST_KB_OLLAMA_HOST", "http://localhost:11434")
EMBED_MODEL = os.environ.get("PENTEST_KB_EMBED_MODEL", "bge-m3")
COLLECTION = os.environ.get("PENTEST_KB_COLLECTION", "pentest_kb")
DIM = 1024                      # bge-m3 的向量维度
OUTPUT_FIELDS = ["source", "path", "title", "text"]


# ── 向量化 ──────────────────────────────────────────────────────────────
def embed(texts, timeout=300):
    """调 Ollama 批量向量化，返回 list[list[float]]。"""
    if not texts:
        return []
    req = urllib.request.Request(
        f"{OLLAMA_HOST}/api/embed",
        data=json.dumps({"model": EMBED_MODEL, "input": texts}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())["embeddings"]


# ── Milvus ──────────────────────────────────────────────────────────────
_client = None


def get_client():
    """复用同一个连接：MCP 服务会被反复调用，每次新建连接太浪费。"""
    global _client
    if _client is None:
        from pymilvus import MilvusClient
        _client = MilvusClient(uri=MILVUS_URI)
    return _client


def ensure_collection(client, reset=False):
    if reset and client.has_collection(COLLECTION):
        client.drop_collection(COLLECTION)
    if not client.has_collection(COLLECTION):
        client.create_collection(
            collection_name=COLLECTION,
            dimension=DIM,
            metric_type="COSINE",
            auto_id=True,
            enable_dynamic_field=True,
        )
    return client


def embed_text(rec):
    """构造用于向量化的文本：标题路径在前，正文在后。

    标题给模型额外的上下文（例如 "SQL Injection > sqlmap > os-shell"），
    实测能明显改善这类技术文档的召回。
    """
    title = (rec.get("title") or "").strip()
    return f"{title}\n\n{rec['text']}" if title else rec["text"]


# ── 查询派生 ────────────────────────────────────────────────────────────
_ASCII_RUN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+#\-]*(?:\s+[A-Za-z0-9][A-Za-z0-9._+#\-]*)*")

# 中文虚词/修饰语，只剥这些不带主题信息的词
_CJK_FILLER = sorted([
    "有哪些", "是什么", "怎么样", "怎么办", "我想", "请问", "常见", "步骤", "方法",
    "用法", "如何", "怎么", "怎样", "什么", "哪些", "以及", "进行", "现在", "然后",
    "就是", "可以", "需要", "应该", "这个", "那个", "我们", "你们", "他们", "它们",
    "一下", "的", "地", "得", "了", "吗", "呢", "吧", "和", "与", "或",
], key=len, reverse=True)


def _ascii_channel(q):
    """只保留英文/技术术语，去掉中文修饰词。"""
    runs = [m.group(0).strip() for m in _ASCII_RUN.finditer(q)]
    return " ".join(r for r in runs if len(r) >= 2).strip()


def _cjk_channel(q):
    """只保留中文实词，去掉英文词和中文虚词。"""
    s = re.sub(r"[A-Za-z0-9][A-Za-z0-9._+#\-]*", " ", q)
    for w in _CJK_FILLER:
        s = s.replace(w, " ")
    s = "".join(ch if ("一" <= ch <= "鿿") else " " for ch in s)
    return " ".join(s.split()).strip()


# 单个词在语料里遍地都是，单独做一路只会引入噪声、把融合结果带偏。
# 只针对「单个词」；多词短语（pass the hash / unquoted service path）照常保留。
_GENERIC_EN = {
    "payload", "payloads", "injection", "attack", "attacks", "exploit", "exploits",
    "command", "commands", "shell", "file", "files", "upload", "user", "username",
    "password", "passwords", "hash", "hashes", "token", "tokens", "test", "testing",
    "bypass", "vulnerability", "vulnerabilities", "security", "web", "server",
    "windows", "linux", "privilege", "escalation", "enumeration", "information",
}


def _channels(query):
    """派生检索通道，去重后返回。第一个永远是原查询。"""
    base = query.strip()
    out = [base]

    ascii_c = _ascii_channel(base)
    too_generic = len(ascii_c.split()) == 1 and ascii_c.lower() in _GENERIC_EN
    if ascii_c and not too_generic and ascii_c.lower() != base.lower() and ascii_c not in out:
        out.append(ascii_c)

    cjk_c = _cjk_channel(base)
    if cjk_c and cjk_c.lower() != base.lower() and cjk_c not in out:
        out.append(cjk_c)

    return out


def _fuse(hit_lists):
    """合并多路结果：同一块取各通道中最高的余弦相似度。

    这里刻意不用 RRF。RRF 按「跨通道共识」加分，适合合并异构检索器；
    而这三路只是同一查询的不同表述，用共识会奖励那些在多个通道尾部都出现的
    噪声块（实测：一个字符重复的 fuzz 文件会因此被顶到第一，且结果随 depth 抖动）。
    取最高相似度则稳定且无深敏感依赖。
    """
    best = {}
    for hits in hit_lists:
        for h in hits:
            cur = best.get(h["id"])
            if cur is None or h["distance"] > cur["distance"]:
                best[h["id"]] = h
    return sorted(best.values(), key=lambda h: -h["distance"])


# ── 后处理：同文件去重 + 链接块降级 ────────────────────────────────────
_LINK = re.compile(r"\[[^\]]{0,60}\]\([^)]+\)|https?://\S+")
_MDLINK_LINE = re.compile(r"^\s*[-*+]\s*\[.*\]\(.*\)\s*$")
LINK_BLOCK_RATIO = 0.6          # 链接行占比 >= 此值即视为「链接块」
MIN_ENTRIES = 3                 # 去重后不足这么多条时，才用链接块补位

# 相似度分数**不能**用来过滤相关性：实测正确答案最低 0.571、噪声最低 0.572，
# 两个分布几乎完全重叠（都挤在 0.57~0.78），任何阈值都会误杀正确答案。
# 所以这里改按**内容形态**判断：只有一堆链接、没有可执行内容的块降级。
# （来源：豆包黑盒测试的发现，已在反序列化/开放重定向等查询上验证。）


def link_ratio(text):
    """链接行占比：整行就是 markdown 链接，或去掉链接后剩不下几个字的行。"""
    lines = [l for l in text.split("\n") if l.strip()]
    if not lines:
        return 0.0
    n = sum(1 for l in lines
            if _MDLINK_LINE.match(l)
            or (_LINK.search(l) and len(_LINK.sub("", l).strip()) < 25))
    return n / len(lines)


def _rank(fused, k):
    """同文件只留一条，且优先留该文件里的非链接块；链接块整体排到后面补位。

    去重同时收紧名额：重复多的查询（实测 AMSI 6 条里 4 条同文件）返回条数
    自然变少，避免拿近似重复的内容白占 agent 的上下文。
    """
    per_file = {}
    for h in fused:
        key = (h["source"], h["path"])
        is_link = link_ratio(h["text"]) >= LINK_BLOCK_RATIO
        cur = per_file.get(key)
        if cur is None:
            per_file[key] = {"hit": h, "link": is_link, "score": h["distance"]}
            continue
        # 非链接块优先；同为非链接或同为链接时取高分
        if (cur["link"] and not is_link) or (cur["link"] == is_link and h["distance"] > cur["score"]):
            per_file[key] = {"hit": h, "link": is_link, "score": h["distance"]}

    items = sorted(per_file.values(), key=lambda x: (x["link"], -x["score"]))
    content = [x for x in items if not x["link"]]
    links = [x for x in items if x["link"]]

    picked = content[:k]
    if len(picked) < min(k, MIN_ENTRIES):
        picked += links[:k - len(picked)]
    return [x["hit"] for x in picked]


def _excerpt(text, query, width):
    """围绕查询命中位置取窗口，而不是从头截。

    同样 400 字符，把命令那段包进来的概率高得多——技术文档里真正有用的
    命令行往往不在块的开头。
    """
    if len(text) <= width:
        return text
    low = text.lower()
    terms = [t for t in re.split(r"[^\w一-鿿]+", query.lower()) if len(t) >= 2]
    pos = -1
    for t in sorted(set(terms), key=len, reverse=True):   # 长词优先，短词到处命中
        p = low.find(t)
        if p >= 0:
            pos = p
            break
    if pos < 0:
        return text[:width].rstrip() + " …"
    start = max(0, pos - width // 3)
    piece = text[start:start + width].strip()
    return ("… " if start > 0 else "") + piece + (" …" if start + width < len(text) else "")


def _raw_search(client, vector, limit, extra):
    hits = client.search(
        collection_name=COLLECTION,
        data=[vector],
        limit=limit,
        output_fields=OUTPUT_FIELDS,
        search_params={"metric_type": "COSINE"},
        **extra,
    )
    out = []
    for h in (hits[0] if hits else []):
        ent = h.get("entity", {}) or {}
        out.append({
            "id": h.get("id"),
            "distance": float(h.get("distance", 0.0)),
            "source": ent.get("source", ""),
            "path": ent.get("path", ""),
            "title": ent.get("title", ""),
            "text": ent.get("text", ""),
        })
    return out


def search(query, k=6, source=None, snippet=400):
    """检索知识库，返回 [{score, source, path, title, text}, ...]。

    source 过滤下推给 Milvus，而不是取回 top-k 后再筛——否则若前 k 条恰好都
    不来自该源，就会被筛成空结果。
    """
    client = get_client()
    if not client.has_collection(COLLECTION):
        return []

    extra = {}
    if source:
        safe = "".join(ch for ch in source if ch.isalnum() or ch in "-_")
        if safe:
            extra["filter"] = f'source == "{safe}"'

    chans = _channels(query)
    depth = max(k * 4, 20)                      # 每路多取一些，供融合用
    vectors = embed(chans)                      # 三路一次性批量嵌入
    lists = [_raw_search(client, v, depth, extra) for v in vectors]

    out = []
    for h in _rank(_fuse(lists), max(1, k)):
        txt = h["text"]
        out.append({
            "score": round(h["distance"], 4),
            "source": h["source"],
            "path": h["path"],
            "title": h["title"],
            "text": _excerpt(txt, query, snippet),
        })
    return out
