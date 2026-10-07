# 设计决策与运维笔记

README 讲**怎么用**，本文讲**为什么这么做**——尤其是那些看起来可以改、
但改回去会出问题的地方，以及踩过的坑。

## 语料更新流程

浅克隆仓库**不能用 `git pull`**，必须 fetch + reset：

```bash
git -C sources/hacktricks fetch --depth=1 origin main
```

```bash
git -C sources/hacktricks reset --hard origin/main
```

（分支名各仓库不同，用 `git -C <仓库> rev-parse --abbrev-ref HEAD` 查。）

全部拉完后重新切块、重新入库（入库约 10 分钟，`--reset` 会重建集合）：

```bash
python rag/chunk.py
```

```bash
python rag/ingest.py --reset
```

`reset --hard` 会丢弃 `sources/` 里的本地改动——**那里一律当只读**。

### 自建经验走独立集合，不进主库

`sources/自建经验/` 是本项目自己的实战复盘（靶场失误点、WAF 绕过实测结论、日志判读要点），
**不是上游原文**，所以入独立集合 `pentest_lessons`，与 `pentest_kb` 隔离。

`chunk.py` **一次产出两个文件**：`chunks.jsonl`（上游）+ `chunks_lessons.jsonl`（自建经验）。
重跑主库后用 `diff chunks.jsonl chunks.jsonl.bak` 自查有没有被污染——**自建经验永远不进主库**。

```bash
python rag/ingest.py --chunks rag/chunks_lessons.jsonl --collection pentest_lessons --reset
```

检索**默认只查主库**；`search_pentest_kb(query, include_lessons=True)` 才把两边一起捞，
交给同一个 `_fuse` 同权融合（返回里 `source` 标注 `自建经验`）。
**改了 `kb.py` / `mcp_server.py` 后必须重启 MCP 服务（重开会话）**，参数才会生效。

## 黑盒测试（交给外部 agent 做）

白盒路线的「预置答案键自动判分」脚本已删除——白盒口径参考意义有限。现在只保留**黑盒**路线。

材料在 `测试/黑盒测试/`，6 个小文件（每个 < 2KB，单独读，别一次全读）。
交给被测 agent 时说这一句即可：

> 读 `测试/黑盒测试/00-先读我.txt`，按里面的顺序读完其余文件并执行。

三个测试都**不需要标准答案**——这正是它能做成黑盒的原因：

1. **可用性打分**：只看返回正文够不够照着做（0/1/2 分），打 2 分必须贴出依据原文
2. **措辞敏感性**：同一主题用纯中文 / 纯英文 / 中英混排各问一次，看返回的**来源+标题是否一致**
3. **点名来源**：问题里点名某个仓库（如"HackTricks 里 GTFOBins 怎么用"），
   看返回的**来源标注对不对得上**，统计命中数 / 10

**硬性约束**：测试者不许打开 `sources/` 原文、不许找任何答案文件；
判定依据只能是 `search_pentest_kb` 实际返回的内容。

**为什么拆成一堆小文件**：某些 agent 单次读取有大小限制，读大文件会失败然后开始瞎猜。
（实测有 agent 卡在 4000 字节边界反复做字节切片，切进多字节汉字中间导致解码失败，
一路误判成"环境故障"。）所以每个文件 < 2KB、不含 emoji，并在文档里明确写了
"读失败不要做字节切片二分"。

## skill 分发包

位置：`skill/`，用于**提交给别的 agent**，完整包约 779 MB。

| 内容 | 体积 |
|---|---|
| `milvus/volumes.tar.gz` + compose（Milvus v3.0.2 数据卷快照） | 751 MB |
| `mcp/mcp_server.py` + `kb.py` + `requirements.txt` + `.mcp.json.template` | 18 KB |
| `rebuild/chunks.jsonl` + `chunk.py` + `ingest.py` | 28 MB |
| `SKILL.md`（部署与使用说明）+ `打包信息.txt`（版本/校验和） | 12 KB |

**本仓库不含** `volumes.tar.gz` 与 `rebuild/chunks.jsonl`——两者都超过 GitHub 单文件限制，
且前者是可再生的数据卷快照、后者能由 `sources/` 重建。要开箱即用请按 `SKILL.md` 自行重建索引。

**部署要点（详见 `SKILL.md`）**：解 `volumes.tar.gz` 到名为 `volumes/` 的目录 → `docker compose up -d`
→ 建 venv 装 `requirements.txt` → 改 `.mcp.json` 路径 → 重启会话。
**目标机必须有 Ollama + `bge-m3`**（每次检索都要嵌入查询，硬依赖）。

**打包方式**：停 Milvus（停机仅约 2 秒）→ 本地快拷卷 → 立刻重启 → 再后台压缩。
`volumes.tar.gz` 的 sha256 见 `打包信息.txt`。

## 已定的设计决策（别改回去）

- **检索**：三通道并行 + 取最高余弦相似度（①原查询 ②纯英文术语 ③纯中文实词）。
  起因是中文修饰词会把 bge-m3 的查询向量带偏（`DCSync` 是 rank 1，`DCSync 导出域内哈希` 完全失败）。
  **不要改回 RRF**：RRF 按跨通道共识加分，会奖励噪声块且结果随 `k` 抖动
  （实测 k=2 与 k=6 给出不同 top1，这个 bug 是端到端验证 MCP 时发现的，评测里看不出来）。
- **语料清洗**（都在 `chunk.py`）：SecLists 不入库；仓库治理文件（`AGENTS.md`/`LICENSE` 等）跳过；
  仓库根 README **仅在仓库还有别的文档时**才跳（否则会误删 AD Cheat Sheet 这种全部内容就在根 README 的源）；
  `.txt` 文件级英文词占比 < 0.04 整份丢弃（PayloadsAllTheThings 的 `Intruder/` fuzz 字典，
  `dotdotpwn.txt` 一个文件曾占全库 5%，且会劫持短查询）。**只对 `.txt` 判定**，避免误伤代码多的正当 `.md`。
- **评测现状**：白盒脚本（`eval_recall.py`）已删。历史数据（2026-09-30 脚本测得）：
  22 题真实问句全部 top-2 内命中、内容级 R@6 = 100%、未命中 0，延迟中位 ~90ms。
  现在评测走**黑盒**路线，见上文。
- **不跨题利用**：多端口靶场不等于同一台机器，先确认隔离边界再谈跨题利用，
  详见 `sources/自建经验/靶场失误与经验.md`。

## 环境注意事项

- **杀软会删语料**：`sources/` 里有真 webshell 样本，会被杀软当病毒删。若发现文件缺失：
  `git -C sources/<仓库> checkout -- .`（从本地对象库还原，不走网络）。
- **venv 隔离**：`rag/.venv` 是独立环境（`include-system-site-packages = false`），
  **不**污染全局 Python。
- **给 Windows 的批处理脚本必须纯 ASCII**——cmd.exe 用 GBK 解析 UTF-8 的 `.cmd`，中文会打乱解析。
- **`pip` 走国内镜像**时对部分包只有 ~50kB/s；装包**别用 `-q`**，否则看着像卡死，用 `-v`。
