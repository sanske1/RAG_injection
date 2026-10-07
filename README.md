# 渗透测试 RAG 知识库

给渗透测试用的**离线语义检索知识库**。语料是上游权威文档原文，经切块、向量化后存进 Milvus，
通过 MCP 工具供 Claude Code 在渗透测试过程中检索取用。

范围：**Web 渗透 + 内网提权**。不涉及 Kali 环境搭建。

## 架构

```
sources/*                  上游仓库原文（只读，一字未改）
   │
   │  chunk.py            按 Markdown 标题层级切块
   ▼
rag/chunks.jsonl           27,820 块，每块带 source / path / title / text
   │
   │  ingest.py           bge-m3 向量化（Ollama）→ 写入 Milvus
   ▼
Milvus  collection: pentest_kb     1024 维, COSINE  ← 上游权威原文
   │
   │  mcp_server.py       暴露 search_pentest_kb 工具（stdio MCP）
   ▼
Claude Code                渗透测试时按需检索（默认只查这一条）

sources/自建经验/           本项目自己的实战复盘（非上游原文，见下节）
   │  chunk.py            → rag/chunks_lessons.jsonl（独立文件，不混入上面那条）
   │  ingest.py --collection pentest_lessons
   ▼
Milvus  collection: pentest_lessons  1024 维, COSINE  ← 默认不查，include_lessons=true 才带
```

## 依赖

| 组件 | 说明 |
|---|---|
| Milvus 3.0.2 standalone | 容器：`milvus-standalone` + `milvus-etcd` + `milvus-minio`，gRPC `19530` |
| Ollama + `bge-m3` | 向量化模型，本地 `11434` |
| Python 3.11 venv | `rag/.venv`，含 `pymilvus` 3.0.2、`mcp` 2.2.0 |

Milvus 未起时：

```bash
docker compose -f skill/milvus/milvus-standalone-docker-compose.yaml up -d
```

## 用法

**① 切块**（改了语料或过滤规则后重跑）

```bash
python rag/chunk.py
```

**② 向量化并入库**

```bash
python rag/ingest.py --reset
```

不加 `--reset` 会从上次中断处续跑（进度记在 `rag/.ingest_state.json`）。
全量约 11 分钟（RTX 5060 Ti 上约 49 块/秒）。

**②′ 自建经验（独立集合，只在改了 `sources/自建经验/` 时才需要）**

```bash
python rag/ingest.py --chunks rag/chunks_lessons.jsonl --collection pentest_lessons --reset
```

`chunk.py` **一次产出两个文件**：`chunks.jsonl`（上游，27,820 块）与
`chunks_lessons.jsonl`（自建经验，11 块）。**自建经验永远不进主库**——
`chunk.py` 已把 `自建经验` 单列，重跑主库也不会被污染（可用
`diff chunks.jsonl chunks.jsonl.bak` 自查）。

**③ 接入 MCP**

把 `skill/mcp/.mcp.json.template` 里的路径改成你机器上的真实路径，放到会话的项目根并改名为 `.mcp.json`，
Claude Code 需重启会话或批准后才能生效。
生效后即可让 Claude 调 `search_pentest_kb` 检索。

## 切块规则

- **Markdown**：按 H1–H3 标题切，标题路径作为 `title`（如 `SQL Injection > sqlmap > os-shell`）。
  标题会拼在正文前一起向量化，显著改善技术文档的召回。**代码块内的 `#` 注释不会被误判为标题。**
- **YAML / TXT / GTFOBins 的无扩展名文件**：整体作为一块。
- 单块上限 1400 字符，超长滑窗切分（重叠 200）；短于 80 字符丢弃。
- **过滤**：跳过 SecLists（纯爆破字典，不是知识）；跳过仓库治理文件（`AGENTS.md`、
  `CONTRIBUTING.md`、`LICENSE` 等）；**仓库根目录的 README 仅在仓库还有其它文档时**才跳过
  （否则会误删 AD Exploitation Cheat Sheet 这种全部内容就在根 README 里的源）。
- **词表过滤**：`.txt` 文件如果**文件级**英文词占比 < 0.04 就整份丢弃。这类是
  PayloadsAllTheThings 里 `Intruder/` 之类的 fuzz 字典（`dotdotpwn.txt` 一个文件就有
  1,645 块、占全库 5%）。它们没有语义却是向量空间的「吸引子」，会劫持短查询
  （实测 `GTFOBins sudo` 的 top1 曾被 `dotdotpwn.txt` 占住）。与 SecLists 同理——
  喂工具用的，不是知识。**只对 `.txt` 判定**：正经文档即使代码多（如
  `windows-amsi-bypass.md`，比值 0.019）也是 `.md`，不会被误伤。

## 检索策略

语料正文几乎全是英文，而查询常是「英文术语 + 中文修饰词」。实测发现修饰词会把
bge-m3 的查询向量带偏——同一个术语，光秃秃查是 rank 1，加几个中文词就完全找不到
（`DCSync` ✓ → `DCSync 导出域内哈希` ✗；`命令注入` ✓ → `命令注入的常见 payload` ✗）。

所以同一次检索跑三路，每块取三路中**最高的余弦相似度**作为最终得分：

| 通道 | 内容 | 作用 |
|---|---|---|
| 1 | 原查询 | 常规情形 |
| 2 | 纯英文术语 | 救被中文稀释的英文术语 |
| 3 | 纯中文实词（剥虚词） | 救被英文词稀释的中文术语 |

单个通用英文词（`payload`、`injection` …）作废独立通道——它们在语料里遍地都是，
只引入噪声。

**刻意不用 RRF。** RRF 按「跨通道共识」加分，适合合并异构检索器；而这里三路只是
同一查询的不同表述，用共识会奖励在多个通道尾部都出现的噪声块——实测一个字符重复
的 fuzz 文件会被顶到第一，且结果随 `depth` 抖动（`k=2` 与 `k=6` 给出不同 top1）。
取最高相似度则稳定、不依赖 depth。

## 返回体压缩（后处理）

检索只解决"捞什么"，返回多少则靠这一层。实测每次返回 **3,024 字符（约 1,200 tokens）**，
改造前是 4,527 字符——**省 33%**。

| 手段 | 做什么 | 效果 |
|---|---|---|
| **同文件去重** | 每个文档只返回一条（取该文件里最优的块） | 不再出现 6 条里 4 条同一文件（实测 AMSI 曾如此）；6 条来自 6 个不同文档 |
| **链接块降级** | 按**内容形态**判断：整行都是 markdown 链接、去掉链接剩不下几个字的块，排到非链接块之后 | 反序列化查询原 top6 里 3 条是纯链接列表（豆包黑盒测试的发现），现在名额让给了有内容的块 |
| **正文 400 字窗口** | 不是从头截，而是**围绕查询命中位置**取约 400 字 | 同样长度下把命令那段包进来（`sqlmap` 那条窗口正中 `--sql-shell`/`--os-shell`） |

**为什么不用相似度阈值**：实测**做不到**。正确答案最低 0.571、噪声最低 0.572，
两个分布几乎完全重叠（都挤在 0.57–0.78）。设 0.58 会砍掉 `cron 计划任务提权` 的正确答案
（它排第 6），同时留着大量 0.60+ 的噪声。**余弦分数在这类长度相近的技术文档上不携带相关性信息**，
所以只能用内容形态判断。

**还想要更省**：唯一有效的杠杆是**砍条数**（k 6→3 省 51%）或**继续砍窗口**（400→300 省 39%）。
但 k 6→3 会真的丢正确文档（实测多丢 1/9），砍窗口则只是少显示。
真正的数量级下降要靠**两段式**（search 只返回标题+路径，另设 fetch 取正文，约省 85%），
代价是 agent 得学会两步走。

## 检索质量

下表是 2026-09-30 用本机脚本测出的**历史数据**（那条脚本属白盒口径，已按要求删除）。
22 条真实问句（Web + 内网提权）+ 250 块自检索，双口径：**严格**=按文件名命中，
**内容**=返回块正文含预期内容（很多主题藏在大文件正文里，文件名不含该词，
只看文件名会误判）。

当前评测改走**黑盒**路线，材料在 `测试\黑盒测试\`，交给外部 agent 执行。

| 指标 | 最初 | 三通道融合后 | 清词表后（当前） |
|---|---|---|---|
| 自检索 · 块级 | 92.4% | 94.4% | **96.0%** |
| 自检索 · 文件级 | 96.0% | 98.4% | 97.6% |
| 问句 · 严格 R@1 | 72.7% | 81.8% | 77.3% |
| 问句 · 严格 R@3 | 86.4% | 100.0% | **100.0%** |
| 问句 · 严格 MRR | 0.795 | 0.909 | 0.886 |
| 问句 · 内容 R@1 | 81.8% | 81.8% | **90.9%** |
| 问句 · 内容 R@6 | 86.4% | 100.0% | **100.0%** |
| 问句 · 内容 MRR | 0.841 | 0.955 | **0.955** |
| 未命中 | 3 | 0 | **0** |
| 单次检索延迟 | 62ms | 93ms | 95ms |

**22 题全部在 top-2 内命中，内容级 R@6 = 100%。**

清词表后严格 R@1 从 81.8% 微降到 77.3%（22 题里两题从 rank 1 滑到 rank 2：
`windows token impersonation`、`mimikatz`），但**块级自检索从 94.4% 升到 96.0%**、
跨语言组的相关性也更好（`如何查找可写的服务路径` 现在直接命中
`EoP - Unquoted Service Path`）。净效果是移除了 2,728 块（8.9%）纯噪声、修掉了
短查询被 fuzz 字典劫持的缺陷，代价是两题的文件级排名挪了一位——在 22 题样本上属噪声范围。

局限：自检索组的查询就是块标题、而标题被拼进了向量化文本，故偏乐观，测的是索引
完好性而非语义泛化。跨语言组只有 3 题，样本太小，不构成统计意义。

## 自建经验（独立集合 `pentest_lessons`）

**这一份不是上游原文。** `sources/自建经验/` 放的是本项目自己的实战复盘
（靶场失误点、WAF 绕过实测结论、日志判读要点），性质与上游权威文档不同，
所以**单独切块、单独入集合**，与 `pentest_kb` 完全隔离。

- **默认不参与检索**：`search_pentest_kb(query)` 只查上游主库，语义与评测基线都不变。
- **按需打开**：`search_pentest_kb(query, include_lessons=True)` 会把两个集合一起检索，
  结果交给同一个 `_fuse` 融合、**同权竞争**（不做额外加权）。返回里 `source` 标注为
  `自建经验`，引用时应说明它来自自建复盘而非上游权威文档。
- **什么时候开**：排查卡住的思路、想避开已知的坑、或怀疑自己工具用法有问题时。
  纯查「某个漏洞怎么打」就保持默认，让权威原文说话。

| 集合 | 内容 | 块数 | 默认 |
|---|---|---|---|
| `pentest_kb` | 上游 11 个权威仓库原文 | 27,820 | 查 |
| `pentest_lessons` | 自建实战经验（非上游） | 11 | 不查 |

实测（2026-09-30）：查 `上传 WAF 绕过 尾点文件名`、`日志分析 怎么区分扫描器和真实攻击者`、
`登录爆破 过滤器 状态码`，`include_lessons=True` 时自建经验均以 **0.68~0.77** 命中 top1
（高于主库同题的 0.57~0.62，因为复盘用词与查询同源）。

**MCP 生效前提**：改了 `mcp_server.py` / `kb.py` 后，**正在跑的 MCP 服务进程加载的还是旧代码**，
`include_lessons` 这个参数要等 MCP 服务重启（重开会话）后才可用。

## 可调参数（环境变量）

| 变量 | 默认 |
|---|---|
| `PENTEST_KB_MILVUS_URI` | `http://localhost:19530` |
| `PENTEST_KB_OLLAMA_HOST` | `http://localhost:11434` |
| `PENTEST_KB_EMBED_MODEL` | `bge-m3` |
| `PENTEST_KB_COLLECTION` | `pentest_kb` |
| `PENTEST_KB_LESSONS_COLLECTION` | `pentest_lessons` |

换向量模型时必须同步改 `kb.py` 里的 `DIM`（bge-m3 = 1024），并重建集合。

## 语料来源

`sources/` 下的**上游**仓库共 11 个（浅克隆，无提交历史）。SecLists 保留在盘上但**不入库**——
它是喂 sqlmap/gobuster/hydra 的字典，不是知识，入 RAG 只会污染检索结果。

### 语料不入库，需自行克隆

**本仓库不含 `sources/` 的 11 个上游仓库**（约 3.1 GB，且都是他人仓库，重复搬运没有意义）。
clone 本仓库后，按下面的清单把它们浅克隆进 `sources/` 即可，目录名必须与仓库名一致
（`chunk.py` 按目录名区分语料来源）：

```bash
git clone --depth=1 https://github.com/HackTricks-wiki/hacktricks.git sources/hacktricks
```

其余 10 个（`--depth=1`，目录名同仓库名）：

| 目录 | 仓库 |
|---|---|
| `Active-Directory-Exploitation-Cheat-Sheet` | `S1ckB0y1337/Active-Directory-Exploitation-Cheat-Sheet` |
| `Checklists` | `netbiosX/Checklists` |
| `GTFOBins` | `GTFOBins/GTFOBins.github.io` |
| `InternalAllTheThings` | `swisskyrepo/InternalAllTheThings` |
| `LOLBAS` | `LOLBAS-Project/LOLBAS` |
| `PEASS-ng` | `peass-ng/PEASS-ng` |
| `PayloadsAllTheThings` | `swisskyrepo/PayloadsAllTheThings` |
| `SecLists` | `danielmiessler/SecLists`（不入库，仅备用） |
| `The-Hacker-Recipes` | `The-Hacker-Recipes/The-Hacker-Recipes` |
| `wstg` | `OWASP/wstg` |

上游仓库的 License 各自独立，使用时请遵循原仓库的授权条款。

HackTricks 单源占 19,312 块，是内网提权与 Web 漏洞的主力参考。

另有 `sources/自建经验/`（1 个文件，11 块）**不属于上游**，是本项目自己的实战复盘，
入独立集合 `pentest_lessons`，详见上面「自建经验」一节。
