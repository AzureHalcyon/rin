"""她自己写的那段：读一眼、整版换掉、清空。

这段挂在 system 上（开场之后、机制之前），`loop.persona` **每步现读**
`<数据>/persona.md`，所以写完之后下一步就是新的——不用重启。

写盘走 `loop.persona.write`：跟网页设置里那一行「自我描述 · 她自己写的」是同一条路，
同一个字数上限、同一份 `persona.log`，也一样把「隔多久自动重写」的计时清零
（不然刚改完，后台整理到点就整版盖掉）。

一小时只让动一次：persona 在 system 前面，改一次下一步的前缀缓存就整段作废。
不到点的时候不写盘，只告诉她「等 X 之后能改」——想改的话先在工作区里起草稿。

三份文件（`persona.md` 当前这版 / `persona.log` 历史 / `persona.stamp` 上次动过的
时刻）都归 `loop.persona` 管，这里不自己拼路径、不自己碰文件。
"""

from __future__ import annotations

import json
import time
from typing import List

from loop import persona as self_store
from loop import prompt_files

NAMES = ("myself",)

# 两次改写之间至少隔这么久。理由不是省字，是省缓存：这段在 system 里。
GAP = 3600.0


def tools(config=None) -> List[dict]:
    text = prompt_files.tool_texts()
    return [
        {"type": "function", "function": {
            "name": "myself",
            "description": text["myself"],
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {"type": "string",
                             "description": text["myself.text"]},
                },
            },
        }},
    ]


def _clock(moment: float) -> str:
    return time.strftime("%m-%d %H:%M", time.localtime(float(moment or 0)))


def call(name: str, arguments: str, config) -> str:
    if name not in NAMES:
        return "没有这个工具：%s" % name
    try:
        payload = json.loads(arguments or "{}")
    except ValueError:
        return "参数不是 JSON。"
    if not isinstance(payload, dict):
        return "参数不是对象。"

    current = self_store.read(config)
    cap = self_store.limit_of(config)
    on = bool(getattr(config, "persona_enabled", False))
    off = "" if on else "（不过「自我描述」现在是关着的，写了也不会拼进 system）"

    if "text" not in payload:
        if not current:
            return "这段现在是空的（上限 %d 字）。%s" % (cap, off)
        return ("现在这版 %d 字，上限 %d。%s\n\n%s"
                % (len(current), cap, off, current))

    body = str(payload.get("text") or "").strip()
    stamp = self_store.stamp_of(config)
    wait = GAP - (time.time() - stamp)
    if wait > 0:
        return ("这段一小时内刚动过（%s）。要到 %s 之后才能再改。"
                "想改的话，先把新说法写进工作区（比如 write 到 `自我描述-草稿.md`），"
                "到点再整版换上去。"
                % (_clock(stamp), _clock(stamp + GAP)))

    if not body:
        self_store.write(config, "")
        self_store.touch_stamp(config)
        return "清空了，下一步起 system 里就没有这段。%s" % off

    asked = len(body)
    saved = self_store.write(config, body)
    self_store.touch_stamp(config)
    note = ""
    if len(saved) < asked:
        note = "（超过上限 %d 字，截掉了后面 %d 字）" % (cap, asked - len(saved))
    return ("换好了：%d 字%s，下一步生效。这段换一次，下一步的前缀缓存会重算一遍。%s"
            % (len(saved), note, off))
