"""工具描述：每个工具一份文件，随实现放在这一侧。

核心（`loop.prompt_files`）只提供「登记 → 读 → 渲染 → 校验」的机制，它不知道
有哪些工具；所以**这张表就是工具面的权威名单**，加一个工具就在这里登记一条，
并在 `prompts/tools/<工具>.txt` 里写：开头那段是描述，`## 参数名` 是参数说明。

参数名必须和文件里的 `## 段` 一一对上：缺一段、多一段都会当场报错——宁可起不来，
也不要静默少一句说明或者多一段没人用的。
"""

from __future__ import annotations

import os
from typing import Dict, List, Tuple

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompts")

# 工具名 -> (界面上的显示名, 一句话说明, 参数名)
TOOLS: Dict[str, Tuple[str, str, List[str]]] = {
    "remember": ("工具 · remember",
                 "把一件事写进记忆：name 是以后搜它的键，note 是记得的内容。",
                 ["name", "event", "note", "scope"]),
    "link": ("工具 · link",
             "把两件事连起来，不动内容。",
             ["a", "b", "relation"]),
    "recall": ("工具 · recall",
               "按关键词联想记忆，返回 id、名字、时间。",
               ["keywords", "after", "before", "exclude"]),
    "recollect": ("工具 · recollect",
                  "把某一件写成一段第一人称回忆，只有这段回忆进上下文。",
                  ["event"]),
    "forget": ("工具 · forget",
               "删掉一件记忆（连同它的连接）。",
               ["event"]),
    "rename": ("工具 · rename",
               "给一件记忆改名。",
               ["event", "name"]),
    "fetch": ("工具 · fetch",
              "打开一个公开网页读正文。",
              ["url", "offset"]),
    "search": ("工具 · search",
               "搜一下网上的东西，返回标题、链接、一小段摘要。",
               ["query"]),
    "run": ("工具 · run",
            "在跑自己的那台机器上跑一条命令（沙箱里，只有工作区能写）。",
            ["command"]),
    "write": ("工具 · write",
              "往工作区里写一个文件。",
              ["path", "content", "mode"]),
    "myself": ("工具 · myself",
               "看、整版换掉、或者清空你自己写的那段自我描述（常驻 system）。",
               ["text"]),
    "bilibili": ("工具 · bilibili",
                 "看 bilibili 的视频：搜、详情、字幕、直链、没字幕时转文字。",
                 ["action", "params"]),
    "qq": ("工具 · qq",
           "给 QQ 那边发一个动作：收发消息、看群和好友、改资料、空间。"
           "常用三个直接写在说明里，其余看 <数据>/workspace/QQ-动作清单.md。",
           ["action", "params"]),
    "xdao": ("工具 · xdao",
             "看 X岛 的板块、串列表、一个串和它的回复（只读）。",
             ["action", "params"]),
}


# 声音：她是谁、怎么说话。核心会在登记过时把它拼在 sink 的最前面（`$voice`）。
PROMPTS: Dict[str, Tuple[str, str, str]] = {
    "voice": ("voice · 身份与说话方式",
              "她是谁、怎么说话、什么不做；拼在 sink 最前面。"
              "可用占位符：`$name`、`$user`。",
              "voice.txt"),
}


def register(prompt_files) -> None:
    """登记到核心：工具描述 10 份，外加声音（`voice`）。

    `wait` 不在这里：它由引擎自己实现（等动静 / 等到点），描述也留在核心。
    声音（她是谁、怎么说话）是实例的内容，所以在这一侧：核心只会在登记过时把它
    拼在 sink 最前面。
    重复登记同名会被核心拒——那是配置错误，不该静默覆盖。
    """
    prompt_files.register_prompts(PROMPTS, base=PROMPTS_DIR)
    prompt_files.register_tools(TOOLS, base=PROMPTS_DIR)
