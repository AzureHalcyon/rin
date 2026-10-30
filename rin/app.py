"""组装：把引擎、记忆、工具接成一台能跑的。

这里是**唯一的装配线**，也是这一侧存在的理由：

- 引擎（`loop.driver.Loop`）不知道有哪些工具，工具表从这里递进去；
- 工具描述（`prompts/tools/*.txt`）随工具放在这一侧，登记进 `loop.prompt_files`；
- 记忆工具面、设置表、网关、命令行入口都从这里拿同一个 config。

换一套工具、加一个工具，都只改这里与 `rin.tools`；核心一行不用动。
"""

from __future__ import annotations

from loop.toolset import ModuleToolset

from rin.tools import bilibili, myself, qq, shell, webtools, workspace, xdao


def register_prompts() -> None:
    """把工具描述登记进核心的提示词表。

    入口（`rin.cli`）与自检都要先调一次：核心的 `tool_texts()` 只认得登记过的工具。
    """
    from loop import prompt_files

    from rin import tool_texts
    tool_texts.register(prompt_files)


def make_toolsets(config) -> list:
    """她这一套工具：上网（fetch / search）、跑命令（run）、写工作区（write）、
    她自己写的那段（myself）、B 站、QQ（一个入口 + 一份清单）、X岛。"""
    return [ModuleToolset(webtools),
            ModuleToolset(shell, config),
            ModuleToolset(workspace, config),
            ModuleToolset(myself, config),
            ModuleToolset(bilibili),
            ModuleToolset(qq, config),
            ModuleToolset(xdao, config)]
