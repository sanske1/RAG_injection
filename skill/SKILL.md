# 渗透测试知识库 RAG（自包含 skill 包）

一个**本地离线**的检索服务：27,820 条渗透测试知识（Web 渗透 + 内网提权），
语料来自 11 个上游权威仓库的**原文**（HackTricks / PayloadsAllTheThings /
InternalAllTheThings / The Hacker Recipes / OWASP WSTG / PEASS-ng / GTFOBins /
LOLBAS / AD Exploitation Cheat Sheet / netbiosX Checklists）。
通过 MCP 暴露成工具 `search_pentest_kb`。

**它只做检索，不生成答案** —— 返回的是原文片段，不是模型编的内容。
所以"没返回相关内容"要归因到**检索**，不是"没生成好答案"。

---

## 一、包里有什么

| 路径 | 内容 | 体积 |
|---|---|---|
| `milvus/volumes.tar.gz` | **Milvus 数据卷**（etcd + minio + milvus 三份） | 750 MB |
| `milvus/milvus-standalone-docker-compose.yaml` | 起 Milvus 的配置，**钉死 v3.0.2** | 3 KB |
| `mcp/mcp_server.py` | MCP 服务入口，暴露 `search_pentest_kb` | 3 KB |
| `mcp/kb.py` | 检索层（三通道 + 融合 + 后处理），`mcp_server` 依赖它 | 12 KB |
| `mcp/requirements.txt` | Python 依赖，**版本钉死** | — |
| `mcp/.mcp.json.template` | MCP 接入配置模板（改路径即可用） | — |
| `rebuild/chunks.jsonl` | 切好的 27,820 块（带 source/path/title） | 28 MB |
| `rebuild/chunk.py` `ingest.py` | 重新切块 / 重新向量化入库 | — |
| `打包信息.txt` | 版本、条数、校验和 | — |

---

## 二、一键部署（推荐）

把整个目录拷到新机器，然后：

```sh
sh deploy.sh
```

它会依次做：**检查依赖 → 解数据卷 → 起 Milvus（自动进对目录）→ 等健康 → 建 Python 环境 → 自检条数**，
最后打印**已填好路径的 `.mcp.json`**，直接粘到会话项目根即可。
依赖不全时它明确告诉你缺什么并停下，不会半途留个烂摊子。

想手工做就照下面四、五节走。

## 资源需求（新机器先确认）

| 项 | 大小 / 说明 |
|---|---|
| 本包（要传输） | 779 MB |
| 解压后的 Milvus 卷 | +934 MB |
| Docker 镜像（首次自动拉取） | ~2.2 GB（milvus 2 GB + etcd + minio） |
| `bge-m3` 模型 | 1.2 GB |
| **磁盘峰值合计** | **约 5–6 GB，建议留 8 GB** |
| 需要联网 | 拉 Docker 镜像、拉 Ollama 模型、pip 装依赖 |
| 占用端口 | `19530` `9091` `9000` `2379`（Milvus）；`11434`（Ollama） |

---

## 三、目标机必须先具备

| 依赖 | 说明 |
|---|---|
| **Docker + docker compose** | 跑 Milvus（三个容器） |
| **Python 3.10+** | 跑 MCP 服务 |
| **Ollama + `bge-m3` 模型（约 1.2 GB）** | **硬依赖，最容易漏**。每次检索都要把查询向量化；没有它检索会直接报错。装法：`ollama pull bge-m3` |
| 端口 | Milvus 用 `19530`(gRPC) 和 `9091`(健康检查)；Ollama 用 `11434`。都会被占用 |

> 磁盘：解压后约 1 GB，加上 Docker 镜像 2 GB，留 5 GB 比较稳。

---

## 三、部署（5 步）

### 1. 解数据卷

必须解到 `volumes/` 这个名字下——compose 里写的是 `${DOCKER_VOLUME_DIRECTORY:-.}/volumes/...`：

```bash
cd <本 skill 目录>/milvus
mkdir -p volumes
tar xzf volumes.tar.gz -C volumes
```

### 2. 起 Milvus（**必须先进到 milvus 目录再执行**）

这个 compose 里写的是 `${DOCKER_VOLUME_DIRECTORY:-.}/volumes/...`，
那个 `.` 是**当前工作目录**，不是 compose 文件所在目录。
在别的地方执行，它会在错误位置新建一套**空卷**——现象是"Milvus 起来了但集合是空的"。

```bash
cd <本 skill 目录>/milvus
docker compose -f milvus-standalone-docker-compose.yaml up -d
```

或者显式指定（这样在哪个目录执行都行）：

```bash
DOCKER_VOLUME_DIRECTORY=<本 skill 目录>/milvus \
  docker compose -f <本 skill 目录>/milvus/milvus-standalone-docker-compose.yaml up -d
```

**版本必须一致**：compose 钉死了 `milvusdb/milvus:v3.0.2`，换别的版本这份数据可能读不了。
会起 3 个容器（milvus-standalone / milvus-etcd / milvus-minio）+ 1 个一次性 init 容器。

### 3. 自检（两步都要过）

```bash
curl -s http://localhost:9091/healthz      # 应返回 OK
```

光健康还不够——**必须确认数据真的加载进来了**：

```bash
<venv>/python -c "import sys;sys.path.insert(0,'<本 skill 目录>/mcp');import kb;print(kb.get_client().query('pentest_kb',filter='',output_fields=['count(*)'])[0]['count(*)'])"
```

应输出 **27820**。若是 0 或报集合不存在，就是上面第 2 步的卷路径指错了。

### 4. 建 Python 环境

```bash
python -m venv .venv
.venv/bin/pip install -r <本 skill 目录>/mcp/requirements.txt
```

Windows 下把 `.venv/bin/pip` 换成 `.venv\Scripts\pip.exe`。

### 5. 接入 MCP

1. 打开 `mcp/.mcp.json.template`，把两个路径改成目标机上的真实路径
2. 存成 **`<会话项目根>/.mcp.json`**（文件名就是这个）
3. 在 **`<会话项目根>/.claude/settings.local.json`** 里加：
   ```json
   "enabledMcpjsonServers": ["pentest-kb"]
   ```
4. **重启会话**——MCP 服务是会话启动时建立连接的，配置改了不会热加载

---

## 四、怎么用

```
search_pentest_kb(query, k=6, source=None)
```

- `query`：自然语言或技术关键词，**中英文都行**（语料正文以英文为主）
- `k`：返回条数，默认 6
- `source`：可选，限定单个来源仓库，取值如 `hacktricks`、`PayloadsAllTheThings`、
  `InternalAllTheThings`、`GTFOBins`、`LOLBAS`、`wstg`、`PEASS-ng`

每条返回：`[相似度] 仓库 | 路径 | 标题`，紧跟**约 400 字的正文窗口**（不是全文）。
结果已按文件去重，每条来自不同文档。要看更多就把 `k` 调大；
要看某篇的完整内容，按返回的路径直接读文件。

---

## 五、排障

| 现象 | 原因 / 处理 |
|---|---|
| 工具列表里没有 `search_pentest_kb` | 会话项目根不对——`.mcp.json` 和 `settings.local.json` 必须都在项目根下 |
| 调用报错、提示连不上 | Milvus 没起。`docker ps` 看三个容器；`curl :9091/healthz` 应返回 OK |
| 报错提到 embedding / ollama | Ollama 没起，或 `bge-m3` 没拉（`ollama list` 应能看到） |
| 返回空 | 集合是空的，用 `rebuild/` 重建 |
| 想确认数据完整 | `milvus_entities` 应为 **27,820** |

---

## 六、重建索引（可选）

`rebuild/` 里是"半成品"：`chunks.jsonl` 已经切好，所以**不必重新切块**。

- **只重算向量**（比如目标的 bge-m3 版本不同）：
  ```bash
  <venv>/python rebuild/ingest.py --reset      # 约 10 分钟
  ```
- **从语料原文重来**：先 `chunk.py`（需要 `sources/` 原文，**本包不含**，3.1 GB），
  再 `ingest.py --reset`

`ingest.py` 支持断点续跑（进度记在 `.ingest_state.json`），中断了重跑会接着走。

---

## 七、设计要点（想改之前先读这几条）

1. **三通道并行 + 取最高相似度**：同一个查询派生出三路——原查询 / 纯英文术语 /
   纯中文实词（剥虚词）——每块取三路中最高的余弦相似度。
   起因：语料是英文而查询常中英混排，修饰词会把 bge-m3 的向量带偏
   （`DCSync` 查得到，`DCSync 导出域内哈希` 完全找不到）。
   **不要改回 RRF**：RRF 按跨通道共识加分，会奖励噪声块，且结果随 depth 抖动。
2. **不要用相似度阈值过滤**：实测**做不到**——正确答案最低 0.571、噪声最低 0.572，
   两个分布几乎完全重叠（都挤在 0.57–0.78）。余弦分数在这类长度相近的技术文档上
   不携带相关性信息。要过滤只能按**内容形态**（例如"整行都是链接、没有可执行内容"
   的 References 块降级）。
3. **语料清洗规则**（在 `chunk.py`）：SecLists 不入库；仓库治理文件
   （`AGENTS.md`/`LICENSE` 等）跳过；仓库根 README 仅在仓库还有别的文档时才跳；
   `.txt` 文件级英文词占比 < 0.04 整份丢弃（PayloadsAllTheThings 的 `Intruder/`
   里是 fuzz 字典，无语义却是向量空间的"吸引子"，会劫持短查询）。
4. **返回体压缩**：同文件去重 + 链接块降级 + 400 字窗口，让每次返回从 4,527 字符
   降到 3,024 字符（省 33%）。真正的大头是**正文长度**，不是元数据。
