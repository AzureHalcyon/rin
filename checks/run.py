"""跑落地侧全部自检。

    python checks/run.py

不联网、不用凭证：假模型、假嵌入、临时目录。
"""

from __future__ import annotations

import sys
import time

import side


ORDER = [
    "check_tools", "check_memory", "check_recall_log", "check_input_line",
    "check_web", "check_qq_bridge", "check_qq_tool", "check_bili_tool",
    "check_settings",
    "check_memory_tools", "check_page_edits", "check_channel_settings",
    "check_channel", "check_workspace", "check_web_tools", "check_shell",
]


def main() -> int:
    side.app.register_prompts()
    started = time.time()
    for name in ORDER:
        getattr(side, name)()
        print("  %s ok" % name, flush=True)
    print("ok: %d checks（%.1f 秒）" % (side.CHECKS[0], time.time() - started))
    return 0


if __name__ == "__main__":
    sys.exit(main())
