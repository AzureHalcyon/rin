"""入口：python -m rin.qq

把 NapCat 的 OneBot 事件转成 rin 的收件箱进件，再把她的回话发回 QQ。
"""

from __future__ import annotations

import argparse
import os
import sys

from rin.qq.bridge import Config, serve


def _list(value: str):
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="rin.qq", description="QQ 通道：OneBot 11 事件 → rin 收件箱")
    parser.add_argument("--gateway", default="http://127.0.0.1:8080",
                        help="rin 网关地址（默认 127.0.0.1:8080）")
    parser.add_argument("--token", default=os.environ.get("RIN_TOKEN", ""),
                        help="rin 网关的令牌（也可用环境变量 RIN_TOKEN）")
    parser.add_argument("--napcat", default="http://127.0.0.1:3000",
                        help="NapCat 的 OneBot HTTP 服务端地址")
    parser.add_argument("--napcat-token", default=os.environ.get("NAPCAT_TOKEN", ""),
                        help="NapCat 的 OneBot token（也可用 NAPCAT_TOKEN）")
    parser.add_argument("--listen", default="127.0.0.1:8098",
                        help="在哪里接 NapCat 的事件（HTTP 客户端模式）")
    parser.add_argument("--allow-users", default="",
                        help="允许的私聊 QQ 号，逗号分隔")
    parser.add_argument("--allow-groups", default="",
                        help="允许的群号，逗号分隔")
    parser.add_argument("--group-mode", default="at", choices=("at", "all"),
                        help="群里只在被 @ 时收（at，默认），还是全收（all）")
    parser.add_argument("--feed-seconds", type=float, default=1.0,
                        help="多久拉一次网关的 /feed（秒）")
    return parser


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)
    if not args.token:
        print("要给 --token（或设 RIN_TOKEN）：网关的 Bearer 令牌", file=sys.stderr)
        return 2
    if not (args.allow_users or args.allow_groups):
        print("至少要给 --allow-users 或 --allow-groups，不然谁说话都不理",
              file=sys.stderr)
        return 2
    config = Config(
        gateway=args.gateway, token=args.token,
        napcat=args.napcat, napcat_token=args.napcat_token,
        listen=args.listen,
        allow_users=_list(args.allow_users), allow_groups=_list(args.allow_groups),
        group_mode=args.group_mode, feed_seconds=args.feed_seconds)
    serve(config)
    return 0


if __name__ == "__main__":
    sys.exit(main())
