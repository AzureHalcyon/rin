"""工具的实现：每一个都满足 `loop.toolset` 的三条约定。

    tools() -> list[dict]      给模型的 schema
    call(name, arguments) -> str  按名字调，返回要写进记录的一段
    NAMES                      这个模块管哪几个名字

`config` 只有需要工作区或超时的模块（`shell` / `workspace`）才收。
"""

__all__ = []
