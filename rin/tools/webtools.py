"""上网的两个工具：fetch（读一个链接）和 search（搜一下）。

说明文字在 `prompts/tools/fetch.txt`、`prompts/tools/search.txt` 里，这里只管 schema
和分派。和记忆工具一样，出错不是抛给循环，而是写进工具结果——模型下一步能看见
「这个链接被拒了」或者「没搜到」，而不是整步挂掉。
"""

from __future__ import annotations

import json
from typing import List

from loop import prompt_files
from rin.tools import web_access

NAMES = ("fetch", "search")


def tools() -> List[dict]:
    text = prompt_files.tool_texts()
    return [
        {"type": "function", "function": {
            "name": "fetch",
            "description": text["fetch"],
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": text["fetch.url"]},
                    "offset": {"type": "integer",
                               "description": text["fetch.offset"]},
                },
                "required": ["url"],
            },
        }},
        {"type": "function", "function": {
            "name": "search",
            "description": text["search"],
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": text["search.query"]},
                },
                "required": ["query"],
            },
        }},
    ]


def call(name: str, arguments: str) -> str:
    try:
        payload = json.loads(arguments or "{}")
    except ValueError:
        return "参数不是 JSON。"
    if not isinstance(payload, dict):
        return "参数不是对象。"
    try:
        if name == "fetch":
            url = str(payload.get("url") or "").strip()
            if not url:
                return "要给一个 url。"
            try:
                offset = int(payload.get("offset") or 0)
            except (TypeError, ValueError):
                offset = 0
            return web_access.shared().fetch(url, offset)
        if name == "search":
            return web_access.shared().search(str(payload.get("query") or ""))
        return "没有这个工具：%s" % name
    except web_access.WebAccessError as exc:
        return "上网失败：%s" % exc
