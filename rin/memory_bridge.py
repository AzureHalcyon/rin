"""memory_v2 的工具面：remember / recall / recollect。

这一层只做翻译：

- 模型给的 JSON → 动词参数（`memory_v2.api.Memory` 的六个动作）
- 动词返回的 `{"ok", "reason", "data"}` → 一句中文

判断（算不算重复、名字像不像 id、能不能连、重名怎么办）全在 `memory_v2.api` 里。
这里出现 `self.graph` 就是越界——写库的时机也只归动词。

自检断的是动词的 `reason`（稳定码），不是这些句子：措辞可以随时改。
"""

from __future__ import annotations

import json
from typing import List

from memory_v2.api import Memory, fail

from loop import prompt_files

OFF_TEXT = "记忆还没接上。"


def _remember_tool(text: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "remember",
            "description": text["remember"],
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string",
                             "description": text["remember.name"]},
                    "event": {"type": "string",
                              "description": text["remember.event"]},
                    "note": {"type": "string",
                             "description": text["remember.note"]},
                    "scope": {"type": "string", "enum": ["long", "short"],
                              "description": text["remember.scope"]},
                },
                "required": ["note"],
            },
        },
    }


def _link_tool(text: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "link",
            "description": text["link"],
            "parameters": {
                "type": "object",
                "properties": {
                    "a": {"type": "string", "description": text["link.a"]},
                    "b": {"type": "string", "description": text["link.b"]},
                    "relation": {"type": "string",
                                 "description": text["link.relation"]},
                },
                "required": ["a", "b"],
            },
        },
    }


def _recall_tool(text: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "recall",
            "description": text["recall"],
            "parameters": {
                "type": "object",
                "properties": {
                    "keywords": {"type": "array", "items": {"type": "string"},
                                 "description": text["recall.keywords"]},
                    "after": {"type": "string",
                              "description": text["recall.after"]},
                    "before": {"type": "string",
                               "description": text["recall.before"]},
                    "exclude": {"type": "array", "items": {"type": "string"},
                                "description": text["recall.exclude"]},
                },
                "required": ["keywords"],
            },
        },
    }


def _recollect_tool(text: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "recollect",
            "description": text["recollect"],
            "parameters": {
                "type": "object",
                "properties": {
                    "event": {"type": "string",
                              "description": text["recollect.event"]},
                },
                "required": ["event"],
            },
        },
    }


def _forget_tool(text: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "forget",
            "description": text["forget"],
            "parameters": {
                "type": "object",
                "properties": {
                    "event": {"type": "string",
                              "description": text["forget.event"]},
                },
                "required": ["event"],
            },
        },
    }


def _rename_tool(text: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": "rename",
            "description": text["rename"],
            "parameters": {
                "type": "object",
                "properties": {
                    "event": {"type": "string",
                              "description": text["rename.event"]},
                    "name": {"type": "string",
                             "description": text["rename.name"]},
                },
                "required": ["event", "name"],
            },
        },
    }


class MemoryBridge(Memory):
    """引擎 + 工具面：引擎管策略与落盘，这里只管模型看到的那几句话。"""

    def tools(self) -> List[dict]:
        if not self.enabled:
            return []
        text = prompt_files.tool_texts()
        return [_remember_tool(text), _link_tool(text), _recall_tool(text),
                _recollect_tool(text), _forget_tool(text), _rename_tool(text)]

    def call(self, name: str, arguments: str) -> str:
        return self.say(name, self.call_result(name, arguments))

    def call_result(self, name: str, arguments: str) -> dict:
        """走一遍动词，返回结构化结果；`call` 才是给模型看的那句话。"""
        if not self.enabled:
            return fail("memory_off")
        try:
            payload = json.loads(arguments or "{}")
        except ValueError:
            return fail("bad_json", tool=name)
        if not isinstance(payload, dict):
            return fail("bad_json", tool=name)
        if name == "remember":
            return self.remember(note=payload.get("note") or "",
                                 name=payload.get("name") or "",
                                 event=payload.get("event") or "",
                                 scope=payload.get("scope") or "long",
                                 days=payload.get("days") or 3)
        if name == "link":
            return self.link(a=payload.get("a") or "", b=payload.get("b") or "",
                             relation=payload.get("relation") or "")
        if name == "recall":
            return self.recall(keywords=payload.get("keywords") or [],
                               exclude=payload.get("exclude") or [],
                               after=payload.get("after") or "",
                               before=payload.get("before") or "")
        if name == "recollect":
            return self.recollect(event=payload.get("event") or "")
        if name == "forget":
            return self.forget(event=payload.get("event") or
                               payload.get("name") or "")
        if name == "rename":
            return self.rename(event=payload.get("event") or "",
                               name=payload.get("name") or "")
        return fail("unknown_tool", tool=name)

    # ── 措辞 ──────────────────────────────────────────────────────────────
    def say(self, name: str, result: dict) -> str:
        """结果 → 一句话。只按 `reason` 分叉，不猜内容。"""
        reason = str(result.get("reason") or "")
        data = result.get("data") or {}
        if reason == "memory_off":
            return OFF_TEXT
        if reason == "bad_json":
            return "%s 的参数不是 JSON。" % name
        if reason == "unknown_tool":
            return "没有这个工具：%s" % name
        say = getattr(self, "_say_" + name, None)
        if say is None:
            return "没有这个工具：%s" % name
        return say(reason, data)

    def _say_remember(self, reason: str, data: dict) -> str:
        if reason == "empty_note":
            return ("remember 需要 note：这次什么都没写。把要记的内容放进 note 再调一次"
                    "（name 或 event 一起给，才知道记到哪一件上）。只加关系用 link。")
        if reason == "name_looks_like_id":
            return ("name 看着像 id（%s）：要改那件就把 id 放进 event，别当新名字用"
                    "（不然会建出一件叫这个名字的空壳）。这次什么都没写。"
                    % (data.get("name") or ""))
        if reason == "event_not_found":
            return "没有这一件：%s。" % data.get("reference")
        if reason == "name_required":
            return ("remember 需要 name 或 event：没有说记到哪一件上，这次什么都没写。"
                    "新开一件就给 name；改旧的那件就给 event（id 或名字）。")
        if reason == "likely_duplicate":
            lines = ["已经有一件很像的（%s）：" % (data.get("name") or "")]
            for item in (data.get("candidates") or [])[:3]:
                lines.append("  %s %s · %s · 相似 %.2f" % (
                    item.get("id"), item.get("name"), item.get("date"),
                    float(item.get("score") or 0)))
            lines.append("要并进那一件就把它的 id 放进 event；"
                         "确实是另一件事，就换个更具体的名字再来。这次没新建。")
            return "\n".join(lines)
        if reason == "duplicate_note":
            return ("这条和「%s」里已经记的一字不差，没再写。要改就换个说法。"
                    % (data.get("before") or ""))
        if reason == "created":
            return "已记（新建）：%s（%s，%s，%d 字）" % (
                data.get("name"), data.get("event_id"),
                data.get("scope") or "long", int(data.get("chars") or 0))
        if reason == "updated":
            return "已更新：%s（%s）——换成了新的这 %d 字%s" % (
                data.get("name"), data.get("event_id"), int(data.get("chars") or 0),
                ("，原来那句是「%s」" % data.get("before")) if data.get("before") else "")
        return "记下了。"

    def _say_link(self, reason: str, data: dict) -> str:
        if reason == "need_two_sides":
            return "link 需要 a 和 b（各给一个 id 或名字，先用 recall 拿）。"
        if reason == "event_not_found":
            missing = data.get("missing") or []
            return ("没找到%s（用 recall 返回的 id，别凭印象写）。这次没连。"
                    % "".join("「%s」" % name for name in missing))
        if reason == "same_event":
            return "a 和 b 是同一件，不用连。"
        if reason == "already_linked":
            return "这两件本来就连着，没再连。"
        return "连上了：%s — %s（%s）" % (data.get("first_name"), data.get("second_name"),
                                        data.get("relation"))

    def _say_forget(self, reason: str, data: dict) -> str:
        if reason == "need_event":
            return "forget 需要 event（id 或名字）。先用 recall 找。"
        if reason == "event_not_found":
            return "没有这一件：%s（用 recall 找找看）" % data.get("reference")
        return "忘了：%s（%s）——这件和它的连接都删了" % (
            data.get("name"), data.get("event_id"))

    def _say_rename(self, reason: str, data: dict) -> str:
        if reason == "need_event_and_name":
            return "rename 需要 event（id 或旧名字）和 name（新名字）。"
        if reason == "event_not_found":
            return "没有这一件：%s（用 recall 找找看）" % data.get("reference")
        if reason == "name_taken":
            return "已经有同名的一件（%s）。换个名字，或者先 forget 掉那一件。" % (
                data.get("clash"))
        return "改名了：%s → %s（%s）" % (data.get("old_name"), data.get("new_name"),
                                        data.get("event_id"))

    def _say_recall(self, reason: str, data: dict) -> str:
        if reason == "need_keywords":
            return "recall 需要 keywords。"
        if reason == "no_index":
            return "嵌入不可用，词面开关关着，这次查不到。"
        if reason == "embed_failed":
            return "嵌入调用失败，词面开关关着，这次查不到。"
        if reason == "not_found":
            return "没有找到。换个关键词再试。"
        items = data.get("items") or []
        lines = ["找到 %d 件：" % len(items)]
        if not data.get("embedded"):
            lines.append("（嵌入服务不可用，只按词面找）")
        for item in items[:5]:
            chars = int(item.get("chars") or 0)
            lines.append("%s %s · %s%s" % (
                item.get("id"), item.get("name") or "", item.get("date") or "",
                (" · %d 字" % chars) if chars else ""))
        lines.append("看具体用 recollect；连关系用 link。")
        return "\n".join(lines)

    def _say_recollect(self, reason: str, data: dict) -> str:
        if reason == "event_not_found":
            return "没有这一件：%s。" % data.get("reference")
        if reason == "no_material":
            return "这一件没有材料。"
        if reason == "no_writer":
            return "编写模型还没接上。"
        if reason == "writer_failed":
            return "编写失败：%s" % data.get("error")
        return str(data.get("text") or "")
