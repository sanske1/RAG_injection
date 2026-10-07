#!/bin/sh
# 新机器部署脚本（面向全新环境）
# 用法：把整个 skill 目录拷到新机器，然后  sh deploy.sh
#
# 它做六件事：检查依赖 -> 解数据卷 -> 起 Milvus -> 等健康 -> 建 Python 环境 -> 自检数据
# 最后打印可以直接粘贴的 .mcp.json（路径已填好）。

set -u
SKILL="$(cd "$(dirname "$0")" && pwd)"
MILVUS_DIR="$SKILL/milvus"
COMPOSE="$MILVUS_DIR/milvus-standalone-docker-compose.yaml"

echo "skill 目录: $SKILL"
echo

# ── 1 依赖检查 ────────────────────────────────────────────────
echo "== 1/6 依赖检查 =="
miss=0
chk() { # chk <名字> <检测命令...>
  if command -v "$2" >/dev/null 2>&1; then echo "  有   $1"
  else echo "  缺   $1   <-- 先装它"; miss=1; fi
}
chk "docker"  docker
chk "tar"     tar
PYBASE=""
if command -v python3 >/dev/null 2>&1; then PYBASE=python3; echo "  有   python3"
elif command -v python  >/dev/null 2>&1; then PYBASE=python;  echo "  有   python"
else echo "  缺   python3 / python   <-- 先装 Python 3.10+"; miss=1; fi
if docker compose version >/dev/null 2>&1; then echo "  有   docker compose"
else echo "  缺   docker compose   <-- 先装它"; miss=1; fi

if curl -s -m 4 http://localhost:11434/api/tags >/dev/null 2>&1; then
  echo "  有   Ollama（在跑）"
  if curl -s -m 5 http://localhost:11434/api/tags | grep -q 'bge-m3'; then
    echo "  有   bge-m3 模型"
  else
    echo "  缺   bge-m3 模型   <-- 执行: ollama pull bge-m3  (约 1.2 GB)"; miss=1
  fi
else
  echo "  缺   Ollama（没装或没起）   <-- 装好并启动它"; miss=1
fi

if [ "$miss" -eq 1 ]; then
  echo
  echo "依赖不全，装完再来。它们都不在包里：Docker / Ollama+bge-m3 / Python3。"
  exit 1
fi

# ── 2 解数据卷 ────────────────────────────────────────────────
echo
echo "== 2/6 解数据卷 =="
if [ -d "$MILVUS_DIR/volumes/minio" ]; then
  echo "  volumes/ 已存在，跳过。（要重解先删掉 $MILVUS_DIR/volumes）"
else
  mkdir -p "$MILVUS_DIR/volumes"
  tar xzf "$MILVUS_DIR/volumes.tar.gz" -C "$MILVUS_DIR/volumes"
  echo "  已解压到 $MILVUS_DIR/volumes"
fi

# ── 3 起 Milvus（必须 cd 进 milvus 目录：compose 里的卷路径是相对当前目录的）──
echo
echo "== 3/6 起 Milvus =="
( cd "$MILVUS_DIR" && docker compose -f milvus-standalone-docker-compose.yaml up -d ) \
  || { echo "  compose 启动失败，看上面的报错"; exit 1; }

# ── 4 等健康 ──────────────────────────────────────────────────
echo
echo "== 4/6 等 Milvus 健康（最多 90 秒）=="
i=0
while [ "$i" -lt 45 ]; do
  if curl -s -m 3 http://localhost:9091/healthz 2>/dev/null | grep -q OK; then
    echo "  healthz OK（等了 $((i*2)) 秒）"; break
  fi
  i=$((i+1)); sleep 2
done
if [ "$i" -ge 45 ]; then
  echo "  超时。看日志： docker compose -f $COMPOSE logs --tail=50"
fi

# ── 5 Python 环境 ─────────────────────────────────────────────
echo
echo "== 5/6 建 Python 环境 =="
cd "$SKILL" || exit 1
[ -d .venv ] || "$PYBASE" -m venv .venv
if [ -x .venv/bin/pip ]; then PIP=.venv/bin/pip; else PIP=.venv/Scripts/pip.exe; fi
"$PIP" install -q -r mcp/requirements.txt && echo "  依赖已装（pymilvus 3.0.2 / mcp 2.2.0）"

# ── 6 自检 ────────────────────────────────────────────────────
echo
echo "== 6/6 自检：集合条数 =="
if [ -x .venv/bin/python ]; then PYBIN="$SKILL/.venv/bin/python"
else PYBIN="$SKILL/.venv/Scripts/python.exe"; fi

"$PYBIN" -c "import sys;sys.path.insert(0,'$SKILL/mcp');import kb
try:
    n=kb.get_client().query('pentest_kb',filter='',output_fields=['count(*)'])[0]['count(*)']
except Exception as e:
    print('  查询失败:', e); raise SystemExit(1)
print('  条数:', n)
raise SystemExit(0 if n==27820 else 2)" || echo "  ^ 条数不是 27820 或查不到——多半是第 3 步卷路径指错了（见 SKILL.md）"

# ── 生成 MCP 配置 ─────────────────────────────────────────────
cat <<EOF

================ 最后一步：把 MCP 接进会话 ================

1) 在「会话的项目根目录」建一个文件，名字必须叫 .mcp.json，内容：

{
  "mcpServers": {
    "pentest-kb": {
      "command": "$PYBIN",
      "args": ["$SKILL/mcp/mcp_server.py"]
    }
  }
}

2) 在同一个项目根下的 .claude/settings.local.json 里加一行（没有这个文件就建）：

{
  "enabledMcpjsonServers": ["pentest-kb"]
}

3) 重启会话。MCP 服务只在会话启动时连接，改完配置不重启不会生效。

自检：重启后调一次 search_pentest_kb(query="kerberoasting")
      能返回「[分数] 仓库 | 路径 | 标题」就成功了。
EOF
