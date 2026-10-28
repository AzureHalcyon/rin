"""rin：把一个「持续在场」的实例落地到真机器上。

这一侧放的是**配套**，不是设计与算法：

- 工具的实现（`rin.tools`：run / fetch / search / write，以及它们的沙箱与正文抽取）
- 工具描述文本（`rin.prompts`：模型看到的每个工具怎么用）
- 记忆工具面（`rin.memory_bridge`：JSON → 动词 → 一句话）
- 网关与本机入口（`rin.gateway`、`rin.cli`）、设置表（`rin.settings`）
- 人格、提示词覆盖、部署脚本、运维记录（仓里，不在包里）

核心算法在设计仓 `palimpsest`：`loop`（循环引擎）与 `memory_v2`（记忆介质）。
依赖只有一个方向：**rin 导入 palimpsest，palimpsest 不认识 rin**。
"""

__all__ = []
