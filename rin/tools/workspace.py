"""在工作区里写文件：只写 `<data>/workspace` 下面的，绝对路径和 `..` 一律拒。

和 `run` 分工：`run` 跑命令（沙箱只放行工作区的写），想留下东西用这个。两个都
落不到 `<data>/` 上面去——`loop.sqlite3`、`graph.json` 那些只有循环进程自己写。

说明文字在 `prompts/tools/write.txt` 里，这里只管闸门和写盘。
出错不抛给循环，写进工具结果——模型下一步能看到「路径不对」而不是整步挂掉。
"""

from __future__ import annotations

import json
import os
import re
from typing import List

from loop import prompt_files

NAMES = ("write",)

# 单次最多写多少字：多是为了别让它一次把整篇东西塞进记录（内容不进上下文，但要落盘）
DEFAULT_MAX_CHARS = 100_000

_ABSOLUTE = re.compile(r"^(?:[A-Za-z]:[\\/]|[\\/]|~)")


def root_of(config) -> str:
    """工作区：数据目录下的 workspace，和 run 用的是同一个。"""
    return os.path.abspath(os.path.join(
        str(getattr(config, "data_dir", ".") or "."), "workspace"))


def _inside(root: str, target: str) -> bool:
    """解析之后还在工作区里吗（挡 `..` 和符号链接绕出去）。"""
    try:
        real_root = os.path.realpath(root)
        real_target = os.path.realpath(target)
    except OSError:
        return False
    return real_target == real_root or real_target.startswith(real_root + os.sep)


def tools(config=None) -> List[dict]:
    if not bool(getattr(config, "write_enabled", False)):
        return []
    text = prompt_files.tool_texts()
    return [
        {"type": "function", "function": {
            "name": "write",
            "description": text["write"],
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": text["write.path"]},
                    "content": {"type": "string",
                                "description": text["write.content"]},
                    "mode": {"type": "string", "enum": ["overwrite", "append"],
                             "description": text["write.mode"]},
                },
                "required": ["path", "content"],
            },
        }},
    ]


def call(name: str, arguments: str, config) -> str:
    if name not in NAMES:
        return "没有这个工具：%s" % name
    if not bool(getattr(config, "write_enabled", False)):
        return "写入没开：这是工作区的写入口，默认关；要开在设置里打开「写入工具」。"
    try:
        payload = json.loads(arguments or "{}")
    except ValueError:
        return "参数不是 JSON。"
    if not isinstance(payload, dict):
        return "参数不是对象。"
    path = str(payload.get("path") or "").strip()
    content = payload.get("content")
    if not path:
        return "要给一个路径（相对工作区）。"
    if content is None:
        return "要给写的内容（可以是空字符串）。"
    content = str(content)
    return save(path, content, config, mode=str(payload.get("mode") or "overwrite"))


def save(path: str, content: str, config, mode: str = "overwrite") -> str:
    root = root_of(config)
    try:
        limit = int(getattr(config, "write_max_chars", DEFAULT_MAX_CHARS)
                    or DEFAULT_MAX_CHARS)
    except (TypeError, ValueError):
        limit = DEFAULT_MAX_CHARS
    if len(content) > limit:
        return ("太长了（%d 字，上限 %d）：分几次写，或者先写要点。"
                % (len(content), limit))
    if _ABSOLUTE.match(path):
        return "只写工作区：路径要是相对的（现在给的是 %s）。" % path
    parts = [part for part in re.split(r"[\\/]+", path) if part]
    if not parts:
        return "路径是空的。"
    if any(part in (".", "..") for part in parts):
        return "路径里不要有 . 或 ..：只能写工作区里的文件。"
    target = os.path.join(root, *parts)
    if not _inside(root, target):
        return "只写工作区：%s 跑出去了。" % path
    folder = os.path.dirname(target)
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        return "建不了目录：%s" % exc
    append = str(mode).strip() == "append"
    try:
        with open(target, "a" if append else "w", encoding="utf-8") as handle:
            handle.write(content)
    except OSError as exc:
        return "写不了：%s" % exc
    relative = os.path.relpath(target, root).replace(os.sep, "/")
    size = 0
    try:
        size = os.path.getsize(target)
    except OSError:
        pass
    return "写好了：%s（%s %d 字，现在 %d 字节）" % (
        relative, "追加" if append else "覆盖", len(content), size)
