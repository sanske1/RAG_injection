#!/usr/bin/env python3
"""MCP 检索服务：把本地渗透测试 RAG 知识库暴露成一个工具，供 Claude Code 调用。

stdio 传输，注意 stdout 只能走 JSON-RPC——任何日志都必须走 stderr。
"""
import logging
import sys
import traceback

# 日志一律进 stderr，绝不污染 stdout 的 JSON-RPC 流
logging.basicConfig(stream=sys.stderr, level=logging.WARNING)

import kb  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402

INSTRUCTIONS = (
    "本地渗透测试知识库，覆盖 Web 渗透与内网提权。"
    "语料为上游权威文档原文（HackTricks、PayloadsAllTheThings、InternalAllTheThings、"
    "The Hacker Recipes、OWASP WSTG、PEASS-ng、GTFOBins、LOLBAS、AD Exploitation Cheat Sheet、"
    "netbiosX Checklists）。做渗透测试时用它查实操步骤、payload、提权手法。"
)

server = MCPServer(name="pentest-kb", version="1.0.0", instructions=INSTRUCTIONS)


@server.tool(
    name="search_pentest_kb",
    description=(
        "在本地渗透测试知识库做语义检索，返回最相关的文档片段及其来源。"
        "适合查：具体漏洞的利用手法、payload、命令、提权路径、AD 攻击步骤、工具用法。"
        "中英文都支持（查中文文档用中文，查英文文档用英文效果更好）。"
        "query 传自然语言或技术关键词；k 为返回条数（默认 6）；"
        "source 可选，用于限定单个来源仓库，取值如 hacktricks、PayloadsAllTheThings、"
        "InternalAllTheThings、GTFOBins、LOLBAS、wstg、PEASS-ng。"
        "每条正文是围绕命中位置的约 400 字窗口，不是全文；需要完整内容时按返回的路径直接读文件。"
        "结果按文件去重，每条来自不同文档。"
    ),
)
def search_pentest_kb(query: str, k: int = 6, source: str | None = None) -> str:
    try:
        hits = kb.search(query, k=k, source=source)
    except Exception as exc:  # 让模型看到可读的错误，而不是协议层崩溃
        return f"检索失败：{type(exc).__name__}: {exc}\n" + traceback.format_exc(limit=2)

    if not hits:
        return "没有命中。换个更短或更具体的关键词，或去掉 source 限制。"

    # 紧凑格式：一条一行头（分数 | 仓库 | 路径 | 标题），紧接正文，不要分隔线。
    # 实测相比旧格式（三行头 + 空行 + --- 分隔）每个条目省约 26 字符。
    blocks = []
    for h in hits:
        blocks.append(f"[{h['score']}] {h['source']} | {h['path']} | {h['title']}\n{h['text']}")
    return "\n\n".join(blocks)


if __name__ == "__main__":
    server.run(transport="stdio")
