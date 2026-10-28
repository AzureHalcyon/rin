"""命令行入口：把循环跑起来。

    python -m loop --name 林晚 --max-steps 20

步骤 0 只有想、说、挂起；记忆与真摘要还没接上（见 docs/持续在场-API实现.md 第 7 节）。
"""

from __future__ import annotations

import argparse
import os
import sys
import threading

from memory_v2.core.config import ChatConfig as MemoryChatConfig
from memory_v2.core.config import Credentials as MemoryCredentials
from memory_v2.core.embed import Embedder
from memory_v2.core.llm import ChatError
from memory_v2.read.archive import ask_model
from memory_v2.read.write import write_memory

from loop.compress import Compressor
from loop.config import LoopConfig, LoopConfigError, load_credentials
from loop.driver import Loop
from rin.memory_bridge import MemoryBridge
from loop.store import LoopStore
from rin.app import make_toolsets, register_prompts
from loop import control
from loop import logs
from loop import prompt_files
from rin import settings
from rin import gateway
from rin.tools import web_access


def _k(tokens: int) -> str:
    return "%.1fk" % (tokens / 1000.0)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rin", description="持续在线的循环运行时（落地侧）")
    parser.add_argument("--credentials", default="", help="凭证 YAML（默认 ~/.dsh/.credentials.yaml）")
    parser.add_argument("--data", default="data/loop", help="上下文数据目录（SQLite 落在这里）")
    parser.add_argument("--memory-data", default="data/memory",
                        help="memory_v2 的图目录")
    parser.add_argument("--name", required=True, help="sink 第一句的名字")
    parser.add_argument("--model", default="deepseek-flash")
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--max-tokens", type=int, default=4000)
    parser.add_argument("--max-steps", type=int, default=20, help="0 表示不设上限")
    parser.add_argument("--block-tokens", type=int, default=50000,
                        help="当前块攒到多少 token 就压（调试时调小）")
    parser.add_argument("--summary-model", default="deepseek-flash",
                        help="摘要模型；填 qwen-plus 就走凭证里的百炼端点")
    parser.add_argument("--summary-provider", default="auto",
                        choices=("auto", "deepseek", "bailian"),
                        help="摘要端点；auto 按模型名判断")
    parser.add_argument("--lexical", action="store_true",
                        help="打开词面检索；默认只用嵌入")
    parser.add_argument("--recent-steps", type=int, default=0,
                        help="最近 N 步写的事件暂不召回；0=关（默认）")
    parser.add_argument("--tool-fold-keep", type=int, default=2,
                        help="每块留最近几条超长工具结果原文；0=关（默认留 2）")
    parser.add_argument("--daily-token-cap", type=int, default=0,
                        help="每天 token 上限（输入+输出）；到了就等到第二天，0=不限")
    parser.add_argument("--daily-token-warn", type=int, default=0,
                        help="每天 token 提醒线；过了在输出里提一句，0=不提")
    parser.add_argument("--context-budget", type=int, default=200000)
    parser.add_argument("--park-seconds", type=float, default=0.0,
                        help="测试用：挂起后等多少秒再继续；0 表示挂起就停"
                             "（正常运行时等真实事件，不要空转）")
    parser.add_argument("--stdin", action="store_true",
                        help="从标准输入读消息进 inbox，并让 wait 真的等；"
                             "不给就只能等时间，无上界的等会直接停")
    parser.add_argument("--http", default="",
                        help="网页与接口：给 host:port（如 127.0.0.1:8080）就开；"
                             "空=不开。开了就等于有输入通道")
    parser.add_argument("--token", default="",
                        help="网页令牌；不给就自动生成并存在 <data>/web-token")
    parser.add_argument("--say", action="append", default=[],
                        help="开场塞进记录的用户消息，可重复")
    return parser


def _utf8_streams() -> None:
    """管道输出按 UTF-8 写，别让控制台代码页把中文弄坏。"""
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8")
            except (OSError, ValueError):
                pass


def _clean_line(line: str) -> str:
    """管道里可能带 BOM 和回车（PowerShell 的 UTF-8 管道会加 BOM）：只留正文。"""
    return str(line or "").replace("\ufeff", "").strip()


def _stdin_pump(db_path: str) -> None:
    """标准输入一行一条消息，写进 inbox（另开一条连接，别和主线程抢）。

    管道进来的编码看对方：按 UTF-8 读，读不出来的换成占位字符，别让线程死掉——
    线程一死，主循环就在 wait 里等一个永远不会来的消息。
    """
    try:
        if hasattr(sys.stdin, "reconfigure"):
            sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        pass
    store = LoopStore(db_path)
    try:
        for line in sys.stdin:
            text = _clean_line(line)
            if not text:
                continue
            try:
                store.push("user", text, source="命令行", direct=True)
            except Exception as exc:
                print("[输入出错] %s" % exc, file=sys.stderr, flush=True)
                continue
            print("[收到] %s" % text, flush=True)
    except Exception as exc:
        print("[输入断了] %s" % exc, file=sys.stderr, flush=True)
    finally:
        store.close()


def main(argv=None) -> int:
    _utf8_streams()
    args = build_parser().parse_args(argv)
    try:
        credentials = load_credentials(args.credentials)
    except LoopConfigError as exc:
        print("凭证不可用：%s" % exc, file=sys.stderr)
        return 1

    web_access.set_tavily_key(credentials.tavily_key)
    # 语音转写用凭证里那组百炼的键（摘要/嵌入同一个端点），不另配一份。
    if credentials.summary_key and not os.environ.get("ASR_KEY"):
        os.environ["ASR_KEY"] = credentials.summary_key
    web_access.set_deepseek_key(credentials.chat_key)

    config = LoopConfig(name=args.name, model=args.model,
                        temperature=args.temperature, max_tokens=args.max_tokens,
                        context_budget=args.context_budget,
                        block_tokens=args.block_tokens,
                        summary_model=args.summary_model,
                        summary_provider=args.summary_provider,
                        lexical=args.lexical,
                        recent_steps=args.recent_steps,
                        tool_fold_keep=args.tool_fold_keep,
                        standby=bool(args.stdin or args.http),
                        daily_token_cap=args.daily_token_cap,
                        daily_token_warn=args.daily_token_warn,
                        park_seconds=args.park_seconds,
                        max_steps=args.max_steps,
                        data_dir=args.data)
    # 网页上改过的旋钮存在 <data>/settings.json，盖掉命令行给的值。
    # 要赶在建嵌入器之前：嵌入模型/维度就是从这份配置里读的。
    stored_settings = settings.load(config.data_dir)
    settings.apply(config, stored_settings)
    # 渠道（对话/嵌入的地址与密钥）也认这份设置；没写就还是凭证文件里的
    settings.apply_credentials(credentials, stored_settings)

    memory_credentials = None
    try:
        memory_credentials = MemoryCredentials.load(args.credentials)
    except Exception as exc:
        print("记忆嵌入不可用：%s" % exc, file=sys.stderr)

    def make_embedder():
        """按当前配置造一个嵌入器；网页上换模型/换地址时也走这里。"""
        if memory_credentials is None:
            return None
        # 网页里改过嵌入地址/密钥就用它（没写就还是凭证文件里的）
        live = settings.load(config.data_dir)
        out = MemoryCredentials(api_key=memory_credentials.api_key,
                                chat_url=memory_credentials.chat_url,
                                embed_url=memory_credentials.embed_url,
                                embed_key=memory_credentials.embed_key)
        if str(live.get("embed_url") or "").strip():
            out.embed_url = str(live["embed_url"]).strip().rstrip("/")
        if str(live.get("embed_key") or "").strip():
            out.embed_key = str(live["embed_key"]).strip()
        memory_config = MemoryChatConfig()
        memory_config.embed_model = str(config.embed_model)
        memory_config.embed_dim = int(config.embed_dim)
        return Embedder(memory_config, out)

    embedder = None
    if memory_credentials is not None:
        try:
            embedder = make_embedder()
        except Exception as exc:
            print("记忆嵌入不可用：%s" % exc, file=sys.stderr)

    register_prompts()          # 工具描述随工具走：先登记这一侧的那 10 份
    store = LoopStore(config.db_path())
    memory = MemoryBridge(args.memory_data, embedder=embedder,
                          lexical=config.lexical,
                          recent_steps=config.recent_steps)
    logger = logs.setup(config.data_dir)
    prompt_files.use_overrides(config.data_dir)

    def on_speak(text: str) -> None:
        print("[说] %s" % text, flush=True)
        logger.info("[说] %s" % text)

    def on_step(info: dict) -> None:
        prompt = info["prompt_tokens"]
        hit = info["cache_hit"]
        rate = (" · 命中 %.0f%%" % (100.0 * hit / prompt)) if prompt else ""
        print("[%d] %s · 块 %s/%s · 上下文 %s/%s%s" % (
            info["step"], info["kind"],
            _k(info["current_tokens"]), _k(config.block_tokens),
            _k(prompt), _k(config.context_budget), rate), flush=True)
        logger.info("[步] %d %s 块=%d 上下文=%d 命中=%d" % (
            info["step"], info["kind"], info["current_tokens"],
            prompt, hit))

    def log_line(text: str) -> None:
        logger.info(text)

    loop = Loop(config, credentials, store, Compressor(config, credentials), memory,
                on_speak=on_speak, on_step=on_step,
                echo=lambda text: (print(text, flush=True), logger.info(text)),
                log=log_line, toolsets=make_toolsets(config))
    memory.writer = lambda material: write_memory(loop.client, material)
    memory.archiver = lambda material: ask_model(loop.client, material)
    memory.on_recall = store.add_recall

    print("名字：%s · 模型：%s · 摘要：%s · 嵌入：%s/%d · 词面：%s · 预算 %s · 块 %s · 库 %s · 记忆 %s" % (
        config.name, config.model, config.summary_model,
        config.embed_model, int(config.embed_dim),
        "开" if config.lexical else "关",
        _k(config.context_budget), _k(config.block_tokens),
        os.path.abspath(config.db_path()), os.path.abspath(args.memory_data)), flush=True)

    if args.stdin:
        print("[输入] 标准输入已接上：直接打字回车，一行一条消息；Ctrl-C 结束。",
              flush=True)
        threading.Thread(target=_stdin_pump, args=(config.db_path(),),
                         daemon=True).start()

    # QQ 的动作清单现写一份进工作区：她自己查得到，改了代码下次起来就更新
    try:
        from rin.tools import qq as qq_tool
        print("[QQ] 动作清单：%s" % qq_tool.write_doc(config), flush=True)
    except OSError as exc:
        print("[QQ] 动作清单写不进去：%s" % exc, flush=True)

    if args.http:
        host, _, port_text = args.http.rpartition(":")
        try:
            port = int(port_text)
        except ValueError:
            print("--http 要给 host:port，比如 127.0.0.1:8080", file=sys.stderr)
            return 1
        token = args.token or gateway.make_token(os.path.join(args.data, "web-token"))
        try:
            gateway.serve(config, config.db_path(), token, host or "127.0.0.1", port,
                      data_dir=config.data_dir, memory_dir=os.path.abspath(args.memory_data),
                      loop=loop, credentials=credentials, embed_factory=make_embedder)
        except OSError as exc:
            print("网页起不来：%s" % exc, file=sys.stderr)
            return 1
        print("[网页] http://%s:%d/ · 令牌 %s" % (host or "127.0.0.1", port, token),
              flush=True)

    for text in args.say:
        loop.inject_user(text)

    try:
        loop.run()
    except control.StopRequested:
        print("[重启] 收到停止请求，退出（记录不受影响）", flush=True)
        store.close()
        return 0
    except KeyboardInterrupt:
        print("\n[中断]", flush=True)
    except ChatError as exc:
        print("模型调用失败：%s" % exc, file=sys.stderr)
        return 1
    totals = store.usage_totals()
    print("[结束] %d 步，%d 次压缩；共 %s token（输入 %s + 输出 %s），"
          "缓存命中 %.0f%%，最大一次上下文 %s" % (
              loop.steps, loop.compressions, _k(totals["total"]),
              _k(totals["prompt"]), _k(totals["completion"]),
              100.0 * totals["hit_rate"], _k(totals["peak"])), flush=True)
    store.close()
    return 0
