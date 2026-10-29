"""入口：python -m rin.xdao

盯着她自己的那几个串，有人回就推给收件箱（**只攒着，不叫醒她**）。

    python -m rin.xdao --gateway http://127.0.0.1:8080 --token <网关令牌> \\
        --data /opt/rin/data --interval 300
"""

from __future__ import annotations

import argparse
import os
import sys

from loop.config import LoopConfig

from rin import settings
from rin.xdao import feed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rin.xdao", description="岛通道：看她自己那几个串的新回复")
    parser.add_argument("--gateway", default="http://127.0.0.1:8080",
                        help="rin 网关地址（默认 127.0.0.1:8080）")
    parser.add_argument("--token", default=os.environ.get("RIN_TOKEN", ""),
                        help="rin 网关的令牌（也可用环境变量 RIN_TOKEN）")
    parser.add_argument("--data", default="data", help="数据目录")
    parser.add_argument("--name", default="", help="名字（只用来记日志）")
    parser.add_argument("--interval", type=float, default=0,
                        help="多少秒看一轮；0 = 用设置里的 xdao_feed_interval")
    parser.add_argument("--once", action="store_true",
                        help="只看一轮就退出（排查用）")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.token:
        # 没给就用数据目录里那份（和网关同一个令牌文件，省得在 unit 里写死）
        try:
            with open(os.path.join(args.data, "web-token"), encoding="utf-8") as handle:
                args.token = handle.read().strip()
        except OSError:
            args.token = ""
    if not args.token:
        print("要给 --token（或设 RIN_TOKEN，或让 <数据>/web-token 可读）",
              file=sys.stderr)
        return 2
    config = LoopConfig(name=args.name or "", data_dir=args.data)
    stored = settings.load(config.data_dir)
    name = str(stored.get("name") or args.name or "").strip()
    if name:
        config.name = name

    def log(text: str) -> None:
        print(text, flush=True)

    if args.once:
        added = feed.poll_once(config, args.gateway, args.token, log)
        log("[岛] 这一轮新推了 %d 条" % added)
        return 0
    interval = args.interval or feed.interval_of(config)
    try:
        feed.serve(config, args.gateway, args.token, interval, log)
    except KeyboardInterrupt:
        print("\n[岛] 退出", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
