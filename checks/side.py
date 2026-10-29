"""落地侧的自检：工具、网关、设置、记忆工具面。

这些用例原本在核心的 `loop/selfcheck.py` 里，搬过来的理由和代码一样：它们测的是
「这个实例怎么跑起来」，不是算法。不联网、不用凭证：假模型、假嵌入、临时目录。
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import sys
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime

from loop.compress import CompressError, Compressor
from loop.config import LoopConfig, LoopCredentials
from loop.context import (Band, Block, BlockEntry, Context, estimate_tokens,
                          serialize_entries)
from loop import control, prompt_files
from loop.store import LoopStore
from memory_v2.api import Memory

from rin import app, alerts, limit, settings, tool_texts
from rin import gateway as web
from rin.cli import _clean_line
from rin.memory_bridge import OFF_TEXT, MemoryBridge
from rin.tools import shell, web_access, webtools, workspace

CHECKS = [0]


def check(condition, message: str) -> None:
    CHECKS[0] += 1
    if not condition:
        raise AssertionError(message)


def tempdir(prefix: str) -> str:
    """在仓里的 `data/` 下建临时目录（和核心那边同规矩：失败时留现场）。"""
    root = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "data")
    os.makedirs(root, exist_ok=True)
    path = os.path.join(root, prefix + uuid.uuid4().hex[:8])
    os.makedirs(path)
    return path


from loop.driver import Loop as _Loop


def Loop(config, credentials=None, store=None, compressor=None, memory=None,
         client=None, **kwargs):
    """落地侧的用例一律带上这一套工具——核心那边默认是空表。"""
    kwargs.setdefault("toolsets", app.make_toolsets(config))
    return _Loop(config, credentials, store=store, compressor=compressor,
                 memory=memory, client=client, **kwargs)

def _loop_config(**kwargs) -> LoopConfig:
    """自检默认不打盹：这些用例没有外部输入，打盹会把它们提前停掉。"""
    kwargs.setdefault("watchdog_idle", 0)
    kwargs.setdefault("user_name", "")
    return LoopConfig(**kwargs)


class FakeClient:
    """一次说一句、一次调工具、一次空回复；usage 按消息内容估。"""

    def __init__(self):
        self.last_usage = {}
        self.calls = 0
        self.prompts = []

    def chat(self, messages, tools=None, on_delta=None):
        prompt = "\n".join(str(message.get("content") or "") for message in messages)
        prompt += "\n".join(str(message.get("tool_calls") or "") for message in messages)
        self.prompts.append(prompt)
        number = self.calls
        self.calls += 1
        content, calls = self._reply(number)
        sent = estimate_tokens(prompt)
        if tools:
            sent += estimate_tokens(json.dumps(tools, ensure_ascii=False))
        self.last_usage = {"prompt_tokens": sent,
                           "completion_tokens": estimate_tokens(content)}
        message = {"role": "assistant", "content": content}
        if calls:
            message["tool_calls"] = calls
        return message

    @staticmethod
    def _reply(number: int):
        slot = number % 4
        if slot == 0:
            return "第 %d 步：说一句。" % number, None
        if slot == 1:
            return "", [{"id": "call_%d" % number, "type": "function",
                         "function": {"name": "recall",
                                      "arguments": "{\"keywords\": [\"预算\"]}"}}]
        if slot == 2:
            return "", None
        return "第 %d 步：再说一句。" % number, None


class BothClient(FakeClient):
    """一条回复里既有正文又有工具调用。"""

    @staticmethod
    def _reply(number: int):
        if number == 0:
            return "我先查一下。", [{"id": "both-1", "type": "function",
                                     "function": {"name": "recall",
                                                  "arguments": "{\"keywords\": [\"预算\"]}"}}]
        return "", None


def fake_summarizer(prompt: str) -> dict:
    return {"summary": "现在：在自检。", "keywords": ["自检"], "unfinished": "等输入"}


class BadSummarizer:
    def __call__(self, prompt: str) -> dict:
        raise CompressError("故意失败")


def check_tools() -> None:
    workdir = tempdir("loop-tools-")
    try:
        config = _loop_config(name="自检", block_tokens=100000, max_steps=4,
                            park_seconds=0.001)
        store = LoopStore(os.path.join(workdir, "loop.sqlite3"))
        loop = Loop(config, credentials=None, store=store,
                    compressor=Compressor(config, summarizer=fake_summarizer),
                    memory=MemoryBridge.disabled(), client=FakeClient())
        loop.run()
        prompts = loop.client.prompts
        # 原生格式：调用走 tool_calls，结果走 role=tool 的消息（不再是"调用 x / 结果：…"文字）
        check(any("recall" in prompt for prompt in prompts),
              "工具调用要以原生 tool_calls 进记录：%r" % prompts[-1][-120:])
        check(any(OFF_TEXT in prompt for prompt in prompts),
              "工具结果要以 tool 消息进记录")
        check("[T]" not in loop.context.block_text()
              and "[S]" not in loop.context.block_text()
              and "[C]" not in loop.context.block_text(),
              "记录里不该再有方括号动作")
        check(len(store.entries(loop.current_id)) > 0, "原文应按条落盘")
        store.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_memory() -> None:
    workdir = tempdir("loop-memory-")
    try:
        bridge = MemoryBridge(
            os.path.join(workdir, "memory"),
            writer=lambda material: "我记得：" + material.replace("\n", " ")[:40],
            lexical=True)
        reply = bridge.call("remember", json.dumps(
            {"name": "预算二十八万", "note": "预算二十八万，已确认。",
             "scope": "long"}, ensure_ascii=False))
        check("已记" in reply, "remember 应成功：%s" % reply)
        found = bridge.call("recall", json.dumps(
            {"keywords": ["预算"]}, ensure_ascii=False))
        check("预算二十八万" in found, "recall 应找到名字：%s" % found)
        event_id = next(iter(bridge.graph.events))
        got = bridge.call("recollect", json.dumps({"event": event_id}))
        check(got.startswith("我记得："), "recollect 应走编写：%s" % got)

        bridge.call("remember", json.dumps(
            {"name": "临时事", "note": "只留一天", "scope": "short", "days": -1},
            ensure_ascii=False))
        again = bridge.call("recall", json.dumps(
            {"keywords": ["临时事"]}, ensure_ascii=False))
        check("临时事" not in again, "过期的 short 不该被 recall 到")

        # 最近 N 步：默认 0（关）；开了才挡新鲜事件。
        bridge.step = 5
        bridge.recent_steps = 3
        bridge.call("remember", json.dumps(
            {"name": "新鲜事", "note": "刚写下的。", "scope": "long"},
            ensure_ascii=False))
        fresh = bridge.call("recall", json.dumps(
            {"keywords": ["新鲜"]}, ensure_ascii=False))
        check("新鲜事" not in fresh, "最近 3 步内的事件应被挡：%s" % fresh)
        bridge.step = 10
        fresh = bridge.call("recall", json.dumps(
            {"keywords": ["新鲜"]}, ensure_ascii=False))
        check("新鲜事" in fresh, "过了 N 步应能 recall 到：%s" % fresh)
        bridge.recent_steps = 0

        # 词面默认关：没有嵌入时查不到，而不是偷偷退回词面。
        plain = MemoryBridge(os.path.join(workdir, "memory-plain"))
        plain.call("remember", json.dumps(
            {"name": "只按词面", "note": "这条只能靠词面找。"}, ensure_ascii=False))
        blocked = plain.call("recall", json.dumps(
            {"keywords": ["词面"]}, ensure_ascii=False))
        check("查不到" in blocked, "词面关且无嵌入时应查不到：%s" % blocked)

        # 新建前查重：名字很像就不建，候选里带着那一件的 id
        first = bridge.call("remember", json.dumps(
            {"name": "Ranchel 讲起名字 Rin 的由来", "note": "名字来自《Shelter》。"},
            ensure_ascii=False))
        check("已记" in first, "第一件该建起来：%s" % first)
        before = len(bridge.graph.events)
        second = bridge.call("remember", json.dumps(
            {"name": "Ranchel 讲起名字 Rin 的来历", "note": "同一件事换个说法。"},
            ensure_ascii=False))
        check("很像" in second and "没新建" in second,
              "名字很像时该拦下来：%s" % second)
        check(len(bridge.graph.events) == before, "拦下来就不该多一件")
        check("e-" in second, "候选里要给出那件的 id：%s" % second)

        # 给 event 合并是另一条路，不能被查重挡住
        merged = bridge.call("remember", json.dumps(
            {"event": "Ranchel 讲起名字 Rin 的由来", "note": "补一句：那是他中学喜欢的歌。"},
            ensure_ascii=False))
        check("已更新" in merged, "给 event 时该改写那一件：%s" % merged)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_recall_log() -> None:
    """每次 recall 记一行：以后调检索质量要用。"""
    workdir = tempdir("loop-recalllog-")
    try:
        seen = []
        bridge = MemoryBridge(os.path.join(workdir, "memory"), lexical=True)
        bridge.on_recall = lambda question, ids: seen.append((question, ids))
        bridge.call("remember", json.dumps(
            {"name": "预算二十八万", "note": "定下了。"}, ensure_ascii=False))
        bridge.call("recall", json.dumps({"keywords": ["预算"]}, ensure_ascii=False))
        check(len(seen) == 1, "recall 该回调一次：%r" % seen)
        check(seen[0][0] == "预算", "问题要记下：%r" % (seen[0],))
        check(len(seen[0][1]) >= 1, "命中的 id 要记下：%r" % (seen[0],))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_input_line() -> None:
    """管道里的 BOM、回车要去掉：PowerShell 的 UTF-8 管道会加 BOM。"""
    check(_clean_line("\ufeff第一条\r\n") == "第一条", "BOM 和回车要去掉")
    check(_clean_line("  \t") == "", "空白行不算消息")
    check(_clean_line(None) == "", "空值不炸")


def check_web() -> None:
    """网页面：没令牌就 401；说一句能进 inbox；流与上下文读得出东西。"""
    import urllib.error
    import urllib.request

    workdir = tempdir("loop-web-")
    httpd = None
    try:
        db_path = os.path.join(workdir, "loop.sqlite3")
        config = _loop_config(name="自检", block_tokens=100000, standby=True,
                              data_dir=workdir)
        store = LoopStore(db_path)
        loop = Loop(config, credentials=None, store=store,
                    compressor=Compressor(config, summarizer=fake_summarizer),
                    memory=MemoryBridge.disabled(), client=FakeClient())
        httpd = web.serve(config, db_path, "tok", "127.0.0.1", 0)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        def get(path, token="tok"):
            request = urllib.request.Request(base + path)
            if token:
                request.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())

        def post(path, payload, token="tok"):
            data = json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(base + path, data=data, method="POST")
            request.add_header("Content-Type", "application/json")
            if token:
                request.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())

        try:
            get("/state", token="")
        except urllib.error.HTTPError as exc:
            check(exc.code == 401, "没令牌该 401，拿到 %d" % exc.code)
        else:
            raise AssertionError("没令牌也放过去了")

        status, _ = post("/say", {"text": "在吗"})
        check(status == 200, "说一句该成功")
        loop.step()                      # 把 inbox 里那句读进记录
        _, state = get("/state")
        check(state["name"] == "自检", "状态里该有名字")
        check(state["steps"] >= 1, "状态里该有步数：%r" % state["steps"])
        check(state["block_tokens"] == 100000, "状态里该有块限额")
        check("kind" in state["status"], "状态里该有它现在在干什么")
        check(state["models"]["chat"] == config.model, "状态里该报对话模型")
        check(state["models"]["summary"] == config.summary_model, "状态里该报摘要模型")
        check(state["models"]["embed"] == config.embed_model, "状态里该报嵌入模型")

        with urllib.request.urlopen(base + "/", timeout=5) as response:
            page = response.read().decode("utf-8")
        check('id="gutter"' in page, "页面里该有行号槽")
        check("data-theme" in page and 'id="theme"' in page, "页面里该有主题切换")
        check('id="picker"' in page, "页面里该有选模型的弹窗")
        check('id="mirror"' in page, "页面里该有量行数的镜像墙")
        check('id="tab-msg"' in page and 'id="inboxview"' in page,
              "页面里该有「消息」那一页（外部来信的仪表盘）")
        check('id="alert"' in page, "顶栏下面该留一条放提示的位置")

        # 落地侧那条提示（NapCat 登不上之类）：写进数据目录就出现在 /state 里，
        # 点掉就是把它删掉——提示不该变成第二条真相。
        alert_path = os.path.join(workdir, "alerts.json")
        with open(alert_path, "w", encoding="utf-8") as handle:
            json.dump({"kind": "qq_login_failed", "ts": "2026-10-29 16:00:00",
                       "text": "QQ 登不上：连着 10 次登录失败，已停下。"}, handle)
        _, state = get("/state")
        check((state.get("alert") or {}).get("text", "").startswith("QQ 登不上"),
              "状态里该带出落地侧的提示：%r" % state.get("alert"))
        status, _ = post("/alerts/clear", {})
        check(status == 200, "点掉提示该成功")
        _, state = get("/state")
        check(state.get("alert") is None, "点掉之后状态里就没有了：%r" % state.get("alert"))

        # 「归档」按钮：网页面只排队，真正的活留给循环（图只有一个写者）
        status, body = post("/archive", {})
        check(status == 200 and body.get("queued"), "点归档要排上：%r" % body)
        check(control.archive_requested(), "开关要置上")
        control.clear_archive()
        check(not control.archive_requested(), "清掉之后就不该还排着")

        _, feed = get("/feed?since=0:0")
        check(any("在吗" in item["text"] for item in feed["entries"]),
              "流里该有刚说的话")
        check(any(item["text"] == "在吗" for item in feed["entries"]),
              "网页说的话原样进记录，不该盖来源前缀：%r"
              % [item["text"] for item in feed["entries"]][:4])
        check(any(item["step"] >= 1 for item in feed["entries"]),
              "流里每条要带步号")
        check(feed["cursor"]["seq"] >= 1, "游标要往前走")

        _, context = get("/context")
        check(context["current"]["count"] >= 1, "上下文里该有当前块")
        check(context["sink"]["chars"] > 0, "上下文里该有 sink")
        check(context["limits"]["block_tokens"] == 100000, "上下文里该有块限额")
        check("bands" in context and "previous" in context, "上下文里该有旧段与上一块")

        # 抽屉里每条工具结果是一个可折叠的小块，靠 seq 当键。没有 seq 的话全挤成
        # "result:undefined"，点开一个别的跟着开，重画时也认不出谁开着。
        seqs = [entry.get("seq") for entry in context["current"]["entries"]]
        check(all(isinstance(seq, int) and seq > 0 for seq in seqs),
              "上下文里每条该带库里的行号：%r" % seqs[:4])
        check(len(set(seqs)) == len(seqs), "行号不该重复（折叠块拿它当键）")

        # 这两条是渲染事故的护栏，别删：
        # ① 抽屉里 role 标签的类名就是 note/user/tool，页面上不能再有裸的 .note，
        #    否则那个标签会被撑成带橙杠、字挤成一列竖排；
        # ② 「结果」「调 N 次」这种标签不能被摘要挤窄，窄了只该摘要收省略号。
        check(".sysnote{" in page and not re.search(r"(?<![\w.\-])\.note\{", page),
              "时间线那行提醒该叫 .sysnote：裸 .note 会和抽屉里的 role 标签撞名")
        check("details.sub>summary .sl{flex:none" in page,
              "抽屉里「结果/调 N 次」不许被挤窄")

        cursor = "%d:%d" % (feed["cursor"]["block"], feed["cursor"]["seq"])
        _, more = get("/feed?since=" + cursor)
        check(more["entries"] == [], "游标之后不该重复给同一条")

        # 「消息」那一页的数据：本机说的话不算外部来的，外部进件要带来源与状态
        post("/inbox", {"text": "外部的问候", "source": "QQ", "chat_type": "private",
                        "chat_id": "10001", "chat_name": "她的人",
                        "sender_name": "Azure"})
        _, box = get("/inbox?limit=50")
        check(any(row["text"] == "外部的问候" for row in box["items"]),
              "收件箱列表里该有外部那条：%r" % [row["text"] for row in box["items"]])
        check(box["counts"]["outside"] == 1, "外部来过几条要数对：%r" % box["counts"])
        check(box["counts"]["waiting"] >= 1, "还没读的条数要数对：%r" % box["counts"])
        checked = [row for row in box["items"] if row["text"] == "外部的问候"][0]
        check(checked["source"] == "QQ" and checked["chat_id"] == "10001",
              "外部进件要带来源和会话：%r" % checked)
        web_row = [row for row in box["items"] if row["text"] == "在吗"][0]
        check(web_row["source"] == "网页" and web_row["direct"] == 1,
              "网页说的要标成「网页」且算本机直投：%r" % web_row)
        check(box["counts"]["outside"] == 1,
              "本机直投不该算进「外部来过」：%r" % box["counts"])

        # 同一个外部编号再推一次：认出来、不重复入库（补拉历史靠它去重）
        post("/inbox", {"text": "补拉回来的一条", "source": "QQ", "ext_id": "m-1"})
        _, again = post("/inbox", {"text": "补拉回来的一条", "source": "QQ",
                                   "ext_id": "m-1"})
        check(again.get("duplicate") is True,
              "同一个 ext_id 再推要认出来：%r" % again)
        _, box = get("/inbox?limit=100")
        check(len([row for row in box["items"] if row["ext_id"] == "QQ:m-1"]) == 1,
              "重复的只该存一条，编号带通道前缀：%r"
              % [row["ext_id"] for row in box["items"]])
        check("notified" in checked and "consumed" in checked,
              "仪表盘要能看出叫没叫过、读没读：%r" % checked)
        check(any(row["chat_id"] == "10001" for row in box["channels"]),
              "会话清单里该有这个会话")

        # 每条连接用完要关掉库连接。以前它等垃圾回收才关：通道适配器一秒拉一次
        # /feed，一小时就能把 ulimit -n（1024）耗光，进程崩在 "Too many open files"。
        if os.path.isdir("/proc/self/fd"):
            def fds() -> int:
                return len(os.listdir("/proc/self/fd"))
            before_fds = fds()            # 别叫 base：那是上面那个 URL 前缀，撞了会 TypeError
            for _ in range(30):
                get("/state")
            grown = fds() - before_fds
            check(grown <= 5, "每条连接用完要关掉库连接，文件描述符不该越用越多"
                              "（30 次请求涨了 %d 个）" % grown)
        store.close()
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        shutil.rmtree(workdir, ignore_errors=True)


def check_qq_bridge() -> None:
    """QQ 通道：回环上的上报不带令牌也认；她说的话只回给叫醒她的那个会话。"""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    from rin.qq import bridge as qq

    def start(host: str, token: str):
        config = qq.Config(gateway="http://127.0.0.1:1", token="tok",
                           napcat="http://127.0.0.1:1", napcat_token=token,
                           listen="127.0.0.1:0", allow_users=["10001"],
                           allow_groups=[], group_mode="at")
        config.host = host                       # 只改判定用到的那个名字
        bridge = qq.Bridge(config)
        bridge.taken = []
        # 网关在测试里不存在：收下什么记在 bridge.taken 里，好核字段对不对
        bridge.push_inbox = lambda item: (bridge.taken.append(item) or 7, True)
        httpd = ThreadingHTTPServer(("127.0.0.1", 0), qq.EventHandler)
        httpd.bridge = bridge
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
        return bridge, httpd

    def post(httpd, payload, token=""):
        request = urllib.request.Request(
            "http://127.0.0.1:%d/onebot" % httpd.server_address[1],
            data=json.dumps(payload).encode("utf-8"), method="POST")
        request.add_header("Content-Type", "application/json")
        if token:
            request.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status

    event = {"post_type": "message", "message_type": "private",
             "self_id": "10000", "user_id": "10001", "message_id": "m1",
             "sender": {"user_id": "10001", "nickname": "Azure"},
             "message": [{"type": "text", "data": {"text": "hi rin"}}]}

    # ① 回环 + NapCat 没带令牌：放行（NapCat 各版本带令牌的方式不一样）
    bridge, httpd = start("127.0.0.1", "secret")
    try:
        check(post(httpd, event) == 200, "回环上不带令牌的上报该放行")
        check(bridge.warned_auth, "放行过要记下来，别每次都说一遍")
        check(len(bridge.taken) == 1, "放行之后要真的收下这一条：%r" % bridge.taken)
        taken = bridge.taken[0] if bridge.taken else {}
        check(taken.get("source") == "QQ" and taken.get("chat_id") == "10001",
              "收下的进件要带来源与会话：%r" % taken)
        check(taken.get("sender_name") == "Azure" and taken.get("text") == "hi rin",
              "收下的进件要带发言人和正文：%r" % taken)
        try:
            post(httpd, event, token="wrong-token")
        except urllib.error.HTTPError as exc:
            check(exc.code == 401, "带错令牌该 401，拿到 %d" % exc.code)
        else:
            raise AssertionError("带错令牌也放过去了")
    finally:
        httpd.shutdown()
        httpd.server_close()

    # ② 监听不在回环：没带令牌就拒（这条路本机以外能连进来）
    _, httpd = start("0.0.0.0", "secret")
    try:
        try:
            post(httpd, event)
        except urllib.error.HTTPError as exc:
            check(exc.code == 401, "对外监听时没带令牌该 401，拿到 %d" % exc.code)
        else:
            raise AssertionError("对外监听也放过去了")
    finally:
        httpd.shutdown()
        httpd.server_close()

    # ③ 回发：只把「回给外部」的那几句发出去，按记录上的 via 定向
    config = qq.Config(gateway="http://127.0.0.1:1", token="tok",
                       napcat="http://127.0.0.1:1", napcat_token="",
                       listen="127.0.0.1:0", allow_users=["10001"],
                       allow_groups=["999"], group_mode="all")
    bridge = qq.Bridge(config)
    got = []
    bridge.send_back = lambda text, chat_type="private", chat_id="": got.append(
        (text, chat_type, chat_id)) or True
    feeds = [
        {"entries": [], "cursor": {"block": 2, "seq": 301}},        # 首轮只对齐
        {"entries": [
            {"role": "assistant", "text": "回私聊的一句",
             "meta": {"via": {"source": "QQ", "chat_type": "private",
                              "chat_id": "10001", "chat_name": "她的人"}}},
            {"role": "assistant", "text": "网页里聊的一句", "meta": {}},
            {"role": "assistant", "text": "回群里的一句",
             "meta": {"via": {"source": "QQ", "chat_type": "group",
                              "chat_id": "999", "chat_name": "群甲"}}},
            {"role": "user", "text": "别人说的", "meta": {}},
        ], "cursor": {"block": 2, "seq": 305}},
    ]
    bridge.feed.get = lambda path: feeds.pop(0)
    try:
        bridge.pump_feed()
        check(got == [], "首轮只对齐游标，历史不该重发：%r" % got)
        bridge.pump_feed()
    finally:
        pass
    check(got == [("回私聊的一句", "private", "10001"),
                  ("回群里的一句", "group", "999")],
          "只该发回给叫醒她的那几个会话：%r" % got)
    check(bridge.cursor == {"block": 2, "seq": 305}, "游标要跟着走到最后一条")

    # ③' 对齐游标要读完所有分页：/feed 一轮最多 300 条，只对一轮的话，
    #     第二轮那些"旧记录"会被当成新话发出去（重启一次就把历史复述一遍）
    page_bridge = qq.Bridge(config)
    early = []
    page_bridge.send_back = lambda text, chat_type="private", chat_id="": \
        early.append(text) or True
    pages = [
        {"entries": [{"role": "assistant", "text": "很久以前的一句",
                      "meta": {"via": {"source": "QQ", "chat_type": "private",
                                       "chat_id": "10001"}}}],
         "cursor": {"block": 1, "seq": 300}, "more": True},
        {"entries": [{"role": "assistant", "text": "更近的一句",
                      "meta": {"via": {"source": "QQ", "chat_type": "private",
                                       "chat_id": "10001"}}}],
         "cursor": {"block": 2, "seq": 5}, "more": False},
        {"entries": [{"role": "assistant", "text": "新的这一句",
                      "meta": {"via": {"source": "QQ", "chat_type": "private",
                                       "chat_id": "10001"}}}],
         "cursor": {"block": 2, "seq": 6}, "more": False},
    ]
    page_bridge.feed.get = lambda path: pages.pop(0)
    page_bridge.pump_feed()
    check(early == [], "对齐游标时一条历史都不该发：%r" % early)
    check(page_bridge.primed and page_bridge.cursor == {"block": 2, "seq": 5},
          "对齐要读到最后一页：%r" % (page_bridge.cursor,))
    page_bridge.pump_feed()
    check(early == ["新的这一句"], "对齐之后的新话才发：%r" % early)

    # ④ 私聊的名字：备注优先（事件里只带昵称，备注要问好友列表），拿不到退昵称
    remark_bridge = qq.Bridge(config)
    remark_bridge._load_friends = lambda: {"10001": "你给他起的备注"}
    named = remark_bridge.on_event(event)
    check(named and named["chat_name"] == "你给他起的备注",
          "私聊要先显示备注：%r" % (named and named.get("chat_name")))
    check(named and named["sender_name"] == "你给他起的备注",
          "发言人那栏也用备注：%r" % (named and named.get("sender_name")))
    plain_bridge = qq.Bridge(config)
    plain_bridge._load_friends = lambda: {}
    plain = plain_bridge.on_event(event)
    check(plain and plain["chat_name"] == "Azure",
          "没备注就退昵称：%r" % (plain and plain.get("chat_name")))

    # ⑤ 分块发送（有的客户端不写 Content-Length）：以前会读成 0 字节 —— 收下空事件、
    #    当"不是消息"回 200，看起来一切正常，消息却没了。
    import socket
    chunk_bridge, chunk_httpd = start("127.0.0.1", "secret")
    try:
        body = json.dumps(event).encode("utf-8")
        head = ("POST /onebot HTTP/1.1\r\nHost: x\r\n"
                "Content-Type: application/json\r\n"
                "Transfer-Encoding: chunked\r\n\r\n").encode()
        payload = b""
        for index in range(0, len(body), 40):
            piece = body[index:index + 40]
            payload += ("%x\r\n" % len(piece)).encode() + piece + b"\r\n"
        payload += b"0\r\n\r\n"
        sock = socket.create_connection(("127.0.0.1", chunk_httpd.server_address[1]),
                                        timeout=5)
        try:
            sock.sendall(head + payload)
            reply = sock.recv(4096)
        finally:
            sock.close()
        check(b"200" in reply.split(b"\r\n")[0], "分块发送也要正常收：%r" % reply[:60])
        check(len(chunk_bridge.taken) == 1,
              "分块发送的正文要读全：%r" % chunk_bridge.taken)
    finally:
        chunk_httpd.shutdown()
        chunk_httpd.server_close()

    # ⑥ 发出去的话要核 NapCat 的返回：HTTP 200 但 retcode 非 0 = 没发出去
    #    （2026-10-28 账号掉线时就是这样：她"回了"，可 QQ 上什么都没有）
    sent = []
    send_bridge = qq.Bridge(config)
    original_post = qq._post_json

    def fake_post(url, payload, token="", timeout=15):
        sent.append(payload)
        return {"status": "failed", "retcode": 1400, "message": "账号已离线"}

    qq._post_json = fake_post
    try:
        ok = send_bridge.send_back("在呢", "private", "10001")
    finally:
        qq._post_json = original_post
    check(ok is False, "被拒的发送不该当成功：%r" % ok)
    check(sent and sent[0].get("user_id") == 10001,
          "发私聊要用 user_id：%r" % sent)

    qq._post_json = lambda url, payload, token="", timeout=15: {
        "status": "ok", "retcode": 0, "data": {"message_id": 1}}
    try:
        check(send_bridge.send_back("在呢", "private", "10001") is True,
              "发成功要报成功")
        check(send_bridge.send_back("在呢", "private", "不是号码") is False,
              "会话号不是数字要当场拒")
    finally:
        qq._post_json = original_post

    # ⑦ 账号掉线要主动说：NapCat 掉线时不报错，只是"什么都没发生"
    qq._post_json = lambda url, payload, token="", timeout=15: {
        "status": "ok", "retcode": 0, "data": {"online": False, "good": False}}
    try:
        online_bridge = qq.Bridge(config)
        check(online_bridge.check_online() is False, "掉线要认得出来")
        check(online_bridge.online is False, "掉线状态要记住")
        # 等扫码登录时 NapCat 的 HTTP 服务根本不在：问不到也要能判断出来
        def boom(url, payload, token="", timeout=15):
            raise OSError("Connection refused")
        qq._post_json = boom
        down_bridge = qq.Bridge(config)
        for _ in range(3):
            down_bridge.checks.clear()          # 每分钟问一次：这里当作三次间隔
            down_bridge.check_online()
        check(down_bridge.down == 3, "连续问不到要计数：%r" % down_bridge.down)
    finally:
        qq._post_json = original_post

    # ⑨ 发不出去就别推游标：这一批下一轮还要重发，推了就永久丢
    stuck = qq.Bridge(config)
    stuck.send_back = lambda text, chat_type="private", chat_id="": False
    stuck.feed.get = lambda path: {"entries": [
        {"role": "assistant", "text": "没发出去的一句",
         "meta": {"via": {"source": "QQ", "chat_type": "private",
                          "chat_id": "10001"}}}],
        "cursor": {"block": 9, "seq": 9}, "more": False}
    stuck.primed = True
    stuck.pump_feed()
    check(stuck.cursor == {"block": 0, "seq": 0},
          "发不出去就不该推游标：%r" % stuck.cursor)
    check(stuck.send_failed == 1, "失败要记一笔（下一轮重试）：%r" % stuck.send_failed)

    # ⑩ 推网关失败：回 5xx 让 NapCat 重发，而且不能记成"收过"
    fail_bridge, fail_httpd = start("127.0.0.1", "")
    try:
        fail_bridge.push_inbox = lambda item: (0, False)
        try:
            post(fail_httpd, event)
        except urllib.error.HTTPError as exc:
            check(exc.code == 503, "网关没接住要回 5xx：%d" % exc.code)
        else:
            raise AssertionError("推失败还回 200，NapCat 就不会重发了")
        check(not fail_bridge.known("m1"), "推失败不该记成收过（要能重放）")
    finally:
        fail_httpd.shutdown()
        fail_httpd.server_close()

    # 团体策略：被 @ 才明确 wake；没 @ 的不传策略（跟会话默认走，群默认 batch）
    group_cfg = qq.Config(gateway="http://127.0.0.1:1", token="tok",
                          napcat="http://127.0.0.1:1", napcat_token="",
                          listen="127.0.0.1:0", allow_users=[],
                          allow_groups=["999"], group_mode="all")
    group_bridge = qq.Bridge(group_cfg)
    group_bridge._group_name = lambda gid: "群甲"
    base = {"post_type": "message", "message_type": "group", "group_id": "999",
            "self_id": "10000", "user_id": "10001",
            "sender": {"user_id": "10001", "nickname": "Azure", "card": "甲"}}
    plain = dict(base, message_id="g1",
                 message=[{"type": "text", "data": {"text": "随便聊聊"}}])
    item = group_bridge.on_event(plain)
    check(item and item.get("policy") == "", "没 @ 的群消息不传策略：%r" % item)
    at_msg = dict(base, message_id="g2",
                  message=[{"type": "at", "data": {"qq": "10000"}},
                           {"type": "text", "data": {"text": " 叫她"}}])
    item = group_bridge.on_event(at_msg)
    check(item and item.get("policy") == "wake", "被 @ 的群消息要 wake：%r" % item)

    # ⑧ 对账补拉：掉线那段时间进来的消息 QQ 不会重新推事件，只能自己问历史
    catch_bridge = qq.Bridge(config)
    taken = []
    catch_bridge.push_inbox = lambda item: (taken.append(item) or 9, True)
    history = {"data": {"messages": [
        {"post_type": "message", "message_type": "private", "user_id": 10001,
         "sender": {"user_id": 10001, "nickname": "Azure"}, "message_id": 11,
         "message": [{"type": "text", "data": {"text": "掉线时漏掉的一条"}}]},
        {"post_type": "message_sent", "message_type": "private",
         "user_id": 10000, "sender": {"user_id": 10000}, "message_id": 12,
         "message": [{"type": "text", "data": {"text": "她自己发的那句"}}]},
    ]}}
    qq._post_json = lambda url, payload, token="", timeout=15: history
    try:
        added = catch_bridge.catch_up()
    finally:
        qq._post_json = original_post
    check(added == 1, "对账该补上一条：%r" % added)
    check(len(taken) == 1 and taken[0]["text"] == "掉线时漏掉的一条",
          "对账只补对方发来的消息：%r" % taken)
    check(taken and taken[0].get("ext_id") == "11",
          "补进来的要带外部编号（去重靠它）：%r" % taken)


def check_bili_tool() -> None:
    """B 站也是"一个入口 + 动作"：help 给动作表，未知动作当场说清，不跑网络。"""
    from rin.tools import bilibili as bili

    check(bili.NAMES == ("bilibili",), "B 站只该有一个入口：%r" % (bili.NAMES,))
    help_text = bili.call("bilibili", json.dumps({"action": "help"}))
    for action in ("search", "video", "subtitles", "playurl", "transcript"):
        check(action in help_text, "help 里该有 %s：%r" % (action, help_text[:80]))
    check("keyword" in help_text and "bvid" in help_text, "help 里该有参数名")

    empty = bili.call("bilibili", "{}")
    check("search" in empty, "不给 action 就回动作表：%r" % empty[:60])

    unknown = bili.call("bilibili", json.dumps({"action": "下载全部"}))
    check("没有这个动作" in unknown, "未知动作要说清：%r" % unknown[:80])

    bad = bili.call("bilibili", json.dumps({"action": "search", "params": "x"}))
    check("params 要是一个对象" in bad, "params 不是对象要拒：%r" % bad)

    tools = bili.tools()
    check(len(tools) == 1 and tools[0]["function"]["name"] == "bilibili",
          "工具面只该有一个 bilibili：%r" % [tool["function"]["name"] for tool in tools])


def check_qq_tool() -> None:
    """QQ 那个入口：渐进披露（先组、再展开一组）、按动作名调、不给的和未知的都拒。"""
    from rin.app import make_toolsets
    from rin.tools import qq as qq_tool
    from rin.tools import qq_actions, workspace

    workdir = tempdir("loop-qq-")
    try:
        config = _loop_config(name="自检", data_dir=workdir, block_tokens=100000)
        paths = qq_tool.write_docs(config)
        doc = qq_tool.doc_path(config)
        check(os.path.isfile(doc), "索引要写进工作区：%r" % doc)
        check(len(paths) == len(qq_actions.CATALOG) + 1,
              "索引 + 每组分一页：%d 个文件" % len(paths))
        with open(doc, encoding="utf-8") as handle:
            index = handle.read()
        check("send_private_msg" in index, "索引里该交代最常用的那几个")
        check("set_group_ban" not in index, "索引**不许**把动作全铺开：该只有组名")
        group_name = "群设置与成员处置"
        group_file = os.path.join(workspace.root_of(config),
                                  "QQ-动作", "%s.md" % group_name)
        check(os.path.isfile(group_file), "分组页要落盘：%r" % group_file)
        with open(group_file, encoding="utf-8") as handle:
            body = handle.read()
        check("set_group_ban" in body and "set_group_kick" in body,
              "分组页里才是那一组的动作：%r" % body[:80])
        check("get_group_list" not in body,
              "只读的群信息不该和踢人禁言混在一组：%r" % body[:80])

        # 工具面的第一层：index 只回组名与条数
        first = qq_tool.call("qq", json.dumps({"action": "index"}), config)
        check("组" in first and "send_private_msg" not in first.replace("最常用", ""),
              "index 只该给组：%r" % first[:160])
        # 第二层：help 展开一组
        second = qq_tool.call("qq", json.dumps(
            {"action": "help", "params": {"group": group_name}}), config)
        check("set_group_ban" in second and "set_group_name" in second,
              "help 要展开那一组：%r" % second[:120])
        check("send_private_msg" not in second, "help 不该顺带别组：%r" % second[:120])
        missing = qq_tool.call("qq", json.dumps(
            {"action": "help", "params": {"group": "没有这组"}}), config)
        check("没有这一组" in missing, "问不存在的组要说清楚：%r" % missing)

        # 工具面里要有它（说明文字登记过、schema 拼得出来）
        store = LoopStore(os.path.join(workdir, "loop.sqlite3"))
        try:
            loop = Loop(config, credentials=None, store=store,
                        compressor=Compressor(config, summarizer=fake_summarizer),
                        memory=MemoryBridge.disabled(), client=FakeClient(),
                        toolsets=make_toolsets(config))
            names = [tool["function"]["name"] for tool in loop.tools()]
            check("qq" in names, "工具面里要有 qq：%r" % names)
        finally:
            store.close()

        got = {}

        def fake_post(action, params, config=None):
            got.update(action=action, params=params)
            return {"status": "ok", "retcode": 0, "data": {"user_id": 1}}

        original = qq_tool._post
        qq_tool._post = fake_post
        try:
            out = qq_tool.call("qq", json.dumps(
                {"action": "get_friend_list", "params": {}}), config)
        finally:
            qq_tool._post = original
        check(got.get("action") == "get_friend_list", "该按动作名调：%r" % got)
        check("get_friend_list" in out and "ok" in out, "结果要带动作与返回：%r" % out)

        called = []
        qq_tool._post = lambda action, params, config=None: (
            called.append(action) or {"status": "ok", "retcode": 0})
        try:
            blocked = qq_tool.call("qq", json.dumps({"action": "set_restart"}), config)
            unknown = qq_tool.call("qq", json.dumps({"action": "hack_all"}), config)
            empty = qq_tool.call("qq", json.dumps({"params": {}}), config)
            ok_reply = qq_tool.call("qq", json.dumps(
                {"action": "send_private_msg",
                 "params": {"user_id": 1, "message": "x"}}), config)
        finally:
            qq_tool._post = original
        check("不给" in blocked, "不给的动作要拒：%r" % blocked)
        check("清单里没有" in unknown, "未知动作要拒并指出清单：%r" % unknown)
        check("要给一个 action" in empty, "缺 action 要提醒：%r" % empty)
        check("send_private_msg" in ok_reply and "retcode" in ok_reply,
              "成功要把返回带回来：%r" % ok_reply)
        check(called == ["send_private_msg"], "被拒的动作不该真的发出去：%r" % called)

        # retcode 不是 0 = 没做成：别把失败报成成功
        qq_tool._post = lambda action, params, config=None: {"status": "failed", "retcode": 1400,
                                                "message": "账号已离线"}
        try:
            bad = qq_tool.call("qq", json.dumps(
                {"action": "send_private_msg", "params": {"user_id": 1}}), config)
        finally:
            qq_tool._post = original
        check("没做成" in bad, "retcode 非 0 要说没做成：%r" % bad)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_settings() -> None:
    """设置页：能读、能写、越界拒绝；改提示词不重启就生效；重启是请求不是硬杀。"""
    import urllib.error
    import urllib.request

    workdir = tempdir("loop-set-")
    httpd = None
    try:
        db_path = os.path.join(workdir, "loop.sqlite3")
        config = _loop_config(name="自检", block_tokens=100000, data_dir=workdir)
        LoopStore(db_path).close()
        prompt_files.use_overrides(workdir)
        httpd = web.serve(config, db_path, "tok", "127.0.0.1", 0, data_dir=workdir)
        base = "http://127.0.0.1:%d" % httpd.server_address[1]

        def call(path, payload=None, method="GET"):
            data = None if payload is None else json.dumps(payload).encode("utf-8")
            request = urllib.request.Request(base + path, data=data, method=method)
            request.add_header("Authorization", "Bearer tok")
            if data is not None:
                request.add_header("Content-Type", "application/json")
            try:
                with urllib.request.urlopen(request, timeout=5) as response:
                    return json.loads(response.read())
            except urllib.error.HTTPError as exc:
                # 有意的 400 由用例自己接住。先把服务端那句话读出来打一行方便定位，
                # 再把 body 装回去——不然用例里的 `exc.read()` 只能读到空。
                detail = exc.read()
                print("  HTTP %s %s -> %s"
                      % (exc.code, path, detail.decode("utf-8", "replace")), flush=True)
                raise urllib.error.HTTPError(exc.url, exc.code, exc.reason,
                                             exc.headers, io.BytesIO(detail)) from None

        payload = call("/settings")
        check(len(payload["fields"]) > 10, "设置里该有旋钮清单")
        check(payload["values"]["block_tokens"] == 100000, "设置里该有当前值")
        check(any(item["key"] == "sink" for item in payload["prompts"]),
              "设置里该有提示词")
        # 工具说明一个工具一份：设置面板要能一份一份地改，而不是一整块两千字
        files = {item["key"]: item["file"] for item in payload["prompts"]}
        for tool in prompt_files.tool_names():
            check(files.get(tool) == "tools/%s.txt" % tool,
                  "设置里该有 %s 那一份：%r" % (tool, files.get(tool)))
        check(len(payload["prompts"]) == len(prompt_files.META) + 1,
              "提示词要齐全（多了她自己写的那段）：%d 份" % len(payload["prompts"]))

        # 提示词自己一项，顺序照真实注入顺序：声音 → sink → 她写的 → 工具 → 按需
        keys = [item["key"] for item in payload["prompts"]]
        check("persona-written" in keys, "她自己写的那段也要列在提示词里")
        check(keys.index("voice") < keys.index("persona-written") < keys.index("sink"),
              "顺序要照注入顺序（开场 → 她自己写的 → 机制与边界）：%r" % keys[:4])
        sections = [item["section"] for item in payload["prompts"]]
        check(sections[:3] == ["常驻"] * 3,
              "前三份都是常驻：%r" % sections[:4])
        order = ["常驻", "工具", "归档", "其他"]
        ranks = [order.index(item) if item in order else -1 for item in sections]
        check(all(rank >= 0 for rank in ranks) and ranks == sorted(ranks),
              "提示词要按 常驻 / 工具 / 归档 / 其他 分段（且不回头）：%r" % sections)
        check(sections.count("常驻") == 3 and "工具" in sections and "归档" in sections,
              "四段都在：%r" % sections)
        check(payload["groups"][-2:] == ["提示词", "改动记录"]
              and "QQ" in payload["groups"] and "B 站" in payload["groups"],
              "提示词、改动记录、QQ、B 站各是一项：%r" % (payload["groups"],))

        fields = {item["key"]: item for item in payload["fields"]}
        check("embed_model" in fields and fields["embed_model"]["group"] == "记忆",
              "设置里该有嵌入模型")
        check(fields["embed_model"].get("picker") == "embed", "嵌入模型该能从列表挑")
        check(fields["model"].get("picker") == "chat", "主模型该能从列表挑")
        check(fields["summary_provider"]["type"] == "select", "摘要端点该是下拉")
        check(payload["memory"]["embed_dim"] > 0, "设置里该报嵌入维度")
        check(fields["qq_token"].get("secret") and fields["bili_sessdata"].get("secret"),
              "两个工具的凭据字段要标成 secret")
        check(fields["qq_base"]["group"] == "QQ" and fields["bili_ffmpeg"]["group"] == "B 站",
              "QQ 和 B 站各有自己那一组")
        check(call("/models?kind=chat")["ok"] is False,
              "没凭证时拉模型列表该老实说不行，而不是 500")

        # 存完就生效：工具那边是每次调用现读的，不用重启
        from rin.tools import bilibili as bili_tool
        call("/settings", {"values": {"bili_ffmpeg": "/opt/bin/ffmpeg",
                                      "bili_asr_model": "qwen3-asr-flash"}}, "PUT")
        check(bili_tool._OVERRIDES.get("bili_ffmpeg") == "/opt/bin/ffmpeg",
              "B 站的 ffmpeg 路径存完就该生效：%r" % bili_tool._OVERRIDES)
        check(bili_tool._asr_model() == "qwen3-asr-flash", "转写模型也要跟着变")

        # 她自己写的那段：设置里能改，存下去是 persona.md
        call("/prompts", {"key": "persona-written", "text": "我改的这句"}, "PUT")
        after = call("/settings")
        row = [item for item in after["prompts"] if item["key"] == "persona-written"][0]
        check(row["text"] == "我改的这句", "改完要读回来：%r" % row["text"])
        check(row["resettable"] is False, "她写的那段不给「恢复默认」（清空是另一回事）")

        call("/settings", {"values": {"summary_provider": "bailian"}}, "PUT")
        check(config.summary_provider == "bailian", "下拉的值要能存")
        try:
            call("/settings", {"values": {"summary_provider": "瞎写"}}, "PUT")
        except urllib.error.HTTPError as exc:
            check(exc.code == 400, "下拉的野值该 400，拿到 %d" % exc.code)
        else:
            raise AssertionError("下拉的野值也存进去了")

        # 400 要带原因：页面把它原样显示出来，不然只剩「HTTP 400」谁知道错在哪。
        try:
            call("/settings", {"values": {"max_tokens": "abc"}}, "PUT")
        except urllib.error.HTTPError as exc:
            body = json.loads(exc.read().decode("utf-8") or "{}")
            check(exc.code == 400 and str(body.get("error") or "").strip(),
                  "400 要带一句人话：%r" % body)
        else:
            raise AssertionError("非数字的 max_tokens 也存进去了")

        # 整张表一起存（页面就是这么发的）：空着的「整理用的模型」必须放行。
        # 它空着 = 跟主模型走，以前漏了 optional，于是整页保存一律 400——
        # 线上表现为「改思考强度回传 HTTP 400」，其实跟那一项无关。
        check(fields["persona_model"].get("optional") is True,
              "整理用的模型空着是合法的（空 = 跟主模型走）")
        whole = dict(payload["values"])
        whole["user_name"] = whole.get("user_name") or "对方"   # 名字空着不是这次的考点
        whole["persona_model"] = ""
        whole["reasoning_effort"] = "high"
        try:
            call("/settings", {"values": whole}, "PUT")
        except urllib.error.HTTPError as exc:
            raise AssertionError("整页保存被拒：%s %s" % (
                exc.code, exc.read().decode("utf-8", "replace"))) from None
        check(config.reasoning_effort == "high", "整页保存要能把思考强度改掉")
        check(settings.load(workdir).get("reasoning_effort") == "high",
              "思考强度要落盘：%r" % settings.load(workdir).get("reasoning_effort"))

        # 换嵌入模型：得当场重建嵌入器（它是造好就带着模型名的）。
        import types
        made = []

        def factory():
            made.append(1)
            return types.SimpleNamespace(available=True)

        httpd.loop = types.SimpleNamespace(
            memory=types.SimpleNamespace(embedder=None), client=None)
        httpd.embed_factory = factory
        call("/settings", {"values": {"embed_model": "qwen3.7-text-embedding-flash",
                                      "embed_dim": 2048}}, "PUT")
        check(config.embed_model == "qwen3.7-text-embedding-flash",
              "嵌入模型该立刻生效")
        check(config.embed_dim == 2048, "嵌入维度该立刻生效")
        check(made and httpd.loop.memory.embedder is not None,
              "换了嵌入模型该当场换个嵌入器")

        call("/settings", {"values": {"block_tokens": 5000}}, "PUT")
        check(settings.load(workdir).get("block_tokens") == 5000, "改的值要落盘")
        check(config.block_tokens == 5000, "存了就该立刻生效，不用等重启")

        try:
            call("/settings", {"values": {"block_tokens": 1}}, "PUT")
        except urllib.error.HTTPError as exc:
            check(exc.code == 400, "越界该 400，拿到 %d" % exc.code)
        else:
            raise AssertionError("越界的值也存进去了")

        call("/prompts", {"key": "sink", "text": "我是测试。"}, "PUT")
        check(prompt_files.text("sink") == "我是测试。", "改提示词该立刻生效")
        call("/prompts", {"key": "sink", "text": ""}, "PUT")
        check("$wait" in prompt_files.text("sink"), "清空该回到默认")

        # 会话与策略：推一条带来源的进件 → 出现在 /channels → 改策略生效
        call("/inbox", {"source": "外部", "chat_type": "group", "chat_id": "g1",
                        "chat_name": "群甲", "sender_name": "甲", "text": "在吗"}, "POST")
        rows = call("/channels")["channels"]
        check(any(row["chat_id"] == "g1" and row["unread"] == 1 for row in rows),
              "会话清单该有这一条：%r" % rows)
        changed = call("/channels", {"chat_id": "g1", "policy": "batch"}, "PUT")
        check(changed["changed"] >= 1, "改策略要影响未读的那条：%r" % changed)
        after = {row["chat_id"]: row["policy"] for row in call("/channels")["channels"]}
        check(after.get("g1") == "batch", "策略要落库：%r" % after)

        call("/restart", {}, "POST")
        check(control.stop_requested(), "重启是给循环一个停止请求")
        control.reset()
        check(not control.stop_requested(), "重置之后不该还停着")
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
        prompt_files.use_overrides("")
        shutil.rmtree(workdir, ignore_errors=True)


def check_memory_tools() -> None:
    """记忆工具面：写/找/取/删/改名。

    断言看**动词给的原因码**和**图上的实际状态**，不看中文句子：措辞属于展示层，
    改一句话不该让自检变红。句子本身只查一件事——该带的信息带没带（id、字数）。
    """
    workdir = tempdir("loop-memtools-")
    try:
        bridge = MemoryBridge(workdir, writer=None, lexical=True)

        def remember(**payload) -> dict:
            return bridge.call_result("remember",
                                      json.dumps(payload, ensure_ascii=False))

        first = remember(name="甲 记下了一件事", note="结论一")
        check(first["reason"] == "created", "第一次该是新建：%s" % first["reason"])
        said = bridge.say("remember", first)
        check(first["data"]["event_id"] in said, "回话要带事件 id：%r" % said)

        second = remember(name="乙 记下了另一件", note="另一条")
        check(second["reason"] == "created", "第二件也是新建：%s" % second["reason"])

        again = remember(name="甲 记下了一件事", note="结论一")
        check(again["reason"] == "duplicate_note", "一字不差该拒：%s" % again["reason"])
        added = remember(name="甲 记下了一件事", note="结论二")
        check(added["reason"] == "updated", "同名再记该是更新：%s" % added["reason"])
        # 一件事一条笔记：再记是改这条，不是另加一条
        kept = [item["text"] for item in
                bridge.graph.events[bridge.graph.resolve("甲 记下了一件事")]["notes"]]
        check(kept == ["结论二"], "再记要换掉原来那条，不追加：%r" % kept)
        check(added["data"]["before"] == "结论一", "改了要带原来那句：%r" % added["data"])

        # 老数据（以前是追加）挂着好几条：下一次写要收回一条
        old_id = bridge.graph.resolve("甲 记下了一件事")
        bridge.graph.events[old_id]["notes"].append(
            {"ts": "2026-10-25T00:00:00", "text": "旧的一条"})
        bridge.graph.save()
        merged = remember(name="甲 记下了一件事", note="合成这一条")
        check(len(bridge.graph.events[old_id]["notes"]) == 1,
              "老的多条笔记要合成一条：%r" % bridge.graph.events[old_id]["notes"])
        check(merged["data"]["chars"] == 5, "要报这条几个字：%r" % merged["data"])

        # 少参数要拒，而且拒了就别动图：含糊的一句会让它反复重调
        no_note = remember(name="丙 记了半句")
        check(no_note["reason"] == "empty_note", "缺 note 该拒：%s" % no_note["reason"])
        no_name = remember(note="半句话")
        check(no_name["reason"] == "name_required", "缺 name 该拒：%s" % no_name["reason"])
        # id 当名字：真发生过一次，建出一件叫 e-xxxx 的空壳
        id_as_name = remember(name="e-dcb934cc25b3", note="内容")
        check(id_as_name["reason"] == "name_looks_like_id",
              "id 当名字该拒：%s" % id_as_name["reason"])
        check("e-dcb934cc25b3" not in [event.get("name") for event in
                                       bridge.graph.events.values()],
              "id 不该真的变成一件记忆")

        # 关系是单独的工具：先建再连，不动内容
        remember(name="戊 说了一件事", note="原来的说法")
        left = bridge.graph.resolve("戊 说了一件事")
        remember(name="戊 又补了一句", note="补的那一句")
        right = bridge.graph.resolve("戊 又补了一句")
        linked = bridge.call_result("link", json.dumps({"a": right, "b": left}))
        check(linked["reason"] == "ok", "连边该成功：%s" % linked["reason"])
        check(linked["data"]["first_name"] == "戊 又补了一句"
              and linked["data"]["second_name"] == "戊 说了一件事",
              "连上要说清连了谁：%r" % linked["data"])
        edges = [edge for edge in bridge.graph.edges.values()
                 if {edge.get("a"), edge.get("b")} == {left, right}]
        check(len(edges) == 1, "两件之间要有一条边：%r" % bridge.graph.edges)
        twice = bridge.call_result("link", json.dumps({"a": left, "b": right}))
        check(twice["reason"] == "already_linked", "重连该拒：%s" % twice["reason"])
        keep = [item["text"] for item in bridge.graph.events[right]["notes"]]
        check(keep == ["补的那一句"], "连边不该动内容：%r" % keep)
        half = bridge.call_result("link", json.dumps({"a": right, "b": "没有这件"}))
        check(half["reason"] == "event_not_found",
              "连不上要说清哪头没找到：%s" % half["reason"])
        itself = bridge.call_result("link", json.dumps({"a": right, "b": right}))
        check(itself["reason"] == "same_event", "自己连自己该拒：%s" % itself["reason"])

        blocked = bridge.call_result("rename", json.dumps(
            {"event": "乙 记下了另一件", "name": "甲 记下了一件事"}))
        check(blocked["reason"] == "name_taken", "重名该拒：%s" % blocked["reason"])
        renamed = bridge.call_result("rename", json.dumps(
            {"event": "乙 记下了另一件", "name": "乙 改了名"}))
        check(renamed["reason"] == "ok", "改名该成功：%s" % renamed["reason"])
        check(renamed["data"]["old_name"] == "乙 记下了另一件",
              "改名要带旧名：%r" % renamed["data"])

        found = bridge.call_result("recall", json.dumps({"keywords": ["甲"]}))
        check(found["reason"] == "ok", "recall 该能找到：%s" % found["reason"])
        names = [item["name"] for item in found["data"]["items"]]
        check("甲 记下了一件事" in names, "该找到那件：%r" % names)
        # 只给指针也要给够判断的信息：字数
        sizes = {item["name"]: item["chars"] for item in found["data"]["items"]}
        check(sizes.get("甲 记下了一件事", 0) == 5, "每条该带字数：%r" % sizes)

        gone = bridge.call_result("forget", json.dumps({"event": "乙 改了名"}))
        check(gone["reason"] == "ok", "forget 该成功：%s" % gone["reason"])
        missing = bridge.call_result("recollect", json.dumps({"event": "乙 改了名"}))
        check(missing["reason"] == "event_not_found", "删了就取不到：%s" % missing["reason"])
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_page_edits() -> None:
    """页面上改完点「存到服务器」：清单进 `edits/`，循环下一步就落到图上。

    以前是让人照着提示去跑 `python -m memory_v2 apply --file …`——网页上的人根本没
    这个入口。
    """
    workdir = tempdir("loop-page-edits-")
    try:
        config = _loop_config(name="自检", data_dir=workdir, block_tokens=100000)
        store = LoopStore(os.path.join(workdir, "loop.sqlite3"))
        bridge = MemoryBridge(os.path.join(workdir, "memory"), lexical=True)
        loop = Loop(config, credentials=None, store=store,
                    compressor=Compressor(config, summarizer=fake_summarizer),
                    memory=bridge, client=FakeClient())
        loop.echo = lambda line: None
        bridge.call("remember", json.dumps({"name": "甲 记了一件事", "note": "内容"}))
        event_id = bridge.graph.resolve("甲 记了一件事")

        folder = os.path.join(workdir, "edits")
        os.makedirs(folder)
        path = os.path.join(folder, "edits-1.json")
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"ops": [{"op": "rename", "id": event_id, "name": "甲 改了名"}]},
                      handle, ensure_ascii=False)
        loop._apply_edits()
        check(bridge.graph.events[event_id]["name"] == "甲 改了名",
              "页面上的改名要落到图上：%r" % bridge.graph.events[event_id])
        check(not os.path.exists(path)
              and os.path.exists(os.path.join(folder, "applied", "edits-1.json")),
              "落完的清单要挪进 applied/，别重复应用")

        bad = os.path.join(folder, "edits-bad.json")
        with open(bad, "w", encoding="utf-8") as handle:
            handle.write("{不是 json")
        loop._apply_edits()
        check(os.path.exists(bad), "坏清单留在原地，下次还能看")
        store.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_channel_settings() -> None:
    """接口组：密钥留空不改、地址归一、打码显示、能盖到凭证上。"""
    workdir = tempdir("loop-chan-")
    try:
        settings.save(workdir, {"chat_key": "sk-first-0001",
                                "chat_url": "https://a.test/v1"}, who="自检")
        stored = settings.save(workdir, {"chat_key": "", "chat_url": ""}, who="自检")
        check(stored.get("chat_key") == "sk-first-0001", "密钥留空不该被清掉")
        values = settings.effective(LoopConfig(name="自检", data_dir=workdir))
        check(values.get("chat_key") == "", "界面上的密钥要打码：%r"
              % values.get("chat_key"))
        hint = settings.hints(workdir).get("chat_key", "")
        check("0001" in hint and "sk-first" not in hint, "提示只露尾巴：%r" % hint)

        creds = LoopCredentials(chat_key="old", chat_url="https://old.test/chat/completions")
        settings.apply_credentials(creds, {"chat_url": "https://b.test/v1",
                                           "embed_url": "https://c.test/v1",
                                           "embed_key": "ek"})
        check(creds.chat_url == "https://b.test/v1/chat/completions", "对话地址要归一")
        check(creds.chat_key == "old", "没给密钥就沿用凭证里的")
        check(creds.summary_url == "https://c.test/v1", "嵌入地址盖到百炼那组")
        check(creds.summary_key == "ek", "嵌入密钥也盖过去")
        check("secret" in str([f for f in settings.FIELDS
                               if f["key"] == "chat_key"][0]), "密钥字段要标记成 secret")
        check(settings.embed_base_of("https://x.test/v1/chat/completions")
              == "https://x.test/v1", "嵌入地址要收成基础地址")
        check(settings.embed_base_of("https://x.test/v1/embeddings")
              == "https://x.test/v1", "/embeddings 也要收")
        settings.apply_credentials(creds, {"embed_url": "https://d.test/v1/chat/completions"})
        check(creds.summary_url == "https://d.test/v1", "贴整条也存基础地址")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_channel() -> None:
    """渠道地址归一：写 `…/v1` 或整条 chat/completions 都认，空就用默认。"""
    from loop.config import DEFAULT_CHAT_URL, chat_url_of

    check(chat_url_of("https://api.example.com/v1")
          == "https://api.example.com/v1/chat/completions",
          "写 /v1 要补成 chat/completions")
    check(chat_url_of("https://x.test/v1/chat/completions")
          == "https://x.test/v1/chat/completions", "整条 URL 要原样")
    check(chat_url_of("https://x.test/v1/") == "https://x.test/v1/chat/completions",
          "尾斜杠要吃掉")
    check(chat_url_of("") == DEFAULT_CHAT_URL, "空就退回默认渠道")


def check_workspace() -> None:
    """工作区：write 只能写 <data>/workspace，绝对路径和 .. 都拒；run 拒读凭证。"""
    workdir = tempdir("loop-work-")
    try:
        cfg = _loop_config(name="自检", data_dir=workdir)
        cfg.write_enabled = False
        check(not workspace.tools(cfg), "关着就工具面里没有 write")
        cfg.write_enabled = True
        names = [tool["function"]["name"] for tool in workspace.tools(cfg)]
        check(names == ["write"], "开着才有 write：%r" % names)

        out = workspace.call("write", json.dumps(
            {"path": "草稿/a.md", "content": "你好"}), cfg)
        check("写好了" in out, "该写进去：%r" % out)
        target = os.path.join(workspace.root_of(cfg), "草稿", "a.md")
        check(os.path.exists(target), "文件该在：%s" % target)
        with io.open(target, encoding="utf-8") as handle:
            check(handle.read() == "你好", "内容要对")
        out = workspace.call("write", json.dumps(
            {"path": "草稿/a.md", "content": "再一句", "mode": "append"}), cfg)
        check("追加" in out, "append 该追加：%r" % out)
        with io.open(target, encoding="utf-8") as handle:
            check(handle.read() == "你好再一句", "追加内容要对")

        for bad in ("/etc/passwd", "../x", "a/../../b", "C:\\x", ""):
            out = workspace.call("write", json.dumps(
                {"path": bad, "content": "x"}), cfg)
            check(any(word in out for word in ("只写工作区", "不要有", "空的",
                                               "要给一个路径")),
                  "该拒：%r -> %r" % (bad, out))
        cfg.write_max_chars = 4
        out = workspace.call("write", json.dumps(
            {"path": "big.txt", "content": "12345"}), cfg)
        check("太长" in out, "超上限要拒：%r" % out)

        run_cfg = _loop_config(name="自检", data_dir=workdir)
        run_cfg.run_enabled = True
        run_cfg.run_allow = "cat ls"
        locked = shell.call("run", json.dumps(
            {"command": "cat /etc/palimpsest/credentials.yaml"}), run_cfg)
        check("不给读" in locked, "凭证该拒：%r" % locked)
        key = shell.call("run", json.dumps(
            {"command": "ls server.pem"}), run_cfg)
        check("不给读" in key, "密钥该拒：%r" % key)
        plain = shell.call("run", json.dumps(
            {"command": "cat loop/README.md"}), run_cfg)
        check("不给读" not in plain, "普通文件不该拒：%r" % plain)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_web_tools() -> None:
    """上网工具：闸门、解析、分页；真抓网络留给服务器上验。"""
    bad = ("file:///etc/passwd", "http://127.0.0.1/", "http://10.0.0.1/",
           "http://localhost:80/", "http://example.com:8080/",
           "http://user:pass@example.com/", "ftp://example.com/")
    for url in bad:
        try:
            web_access.validate_url(url)
        except web_access.WebAccessError:
            check(True, "")
        else:
            raise AssertionError("该拒绝的没拒绝：%s" % url)

    check(web_access.validate_url("http://93.184.216.34/a?b=1").endswith("?b=1"),
          "公开地址该放行，并保留查询串")

    rss = (b'<?xml version="1.0"?><rss><channel><item><title>A &amp; B</title>'
           b'<link>https://example.com/1</link><description>x</description></item>'
           b'<item><link>https://example.com/2</link></item></channel></rss>')
    hits = web_access.parse_bing_rss(rss)
    check(len(hits) == 2 and hits[0]["title"] == "A & B", "搜索 RSS 要解析得出来")
    check(hits[1]["title"] == "https://example.com/2", "没标题就拿链接顶上")

    page = web_access.extract_page(
        "<html><head><title>标题</title><style>x{}</style></head>"
        "<body><script>bad()</script><p>正文一</p><p>正文二</p></body></html>")
    check(page["title"] == "标题", "标题该抽出来：%r" % page["title"])
    check("正文一" in page["text"] and "bad()" not in page["text"],
          "正文要有、脚本要没：%r" % page["text"])

    body = "字" * 500
    part, total, more = web_access._slice(body, 0, 400)
    check(len(part) == 400 and total == 500 and more, "分页该按字切")
    tail, _, rest = web_access._slice(body, 400, 400)
    check(len(tail) == 100 and not rest, "末尾之后不该还有")

    check(web_access.needs_upgrade("短"), "抽出来太短就该走抓取接口")
    check(not web_access.needs_upgrade("字" * 400), "够长就不该花那个钱")

    ds = {"results": [
        {"title": "好", "url": "https://a.com", "summary": "摘要一", "rerank_score": 2.4},
        {"title": "差", "url": "https://b.com", "summary": "摘要二", "rerank_score": 0.2}]}
    ds_hits = web_access.parse_deepseek(ds)
    check(len(ds_hits) == 1 and ds_hits[0]["title"] == "好", "低分结果要被筛掉")
    check(len(web_access.parse_deepseek(
        {"results": [{"title": "差", "url": "https://b.com", "rerank_score": 0.2}]})) == 1,
        "全是低分时不要假装没搜到")
    check(ds_hits[0]["snippet"] == "摘要一", "摘要优先用 summary")

    tavily = {"results": [
        {"title": "备案须知", "url": "https://example.com/doc", "content": "需要申请备案"},
        {"title": "坏的", "url": "ftp://nope", "content": "x"}]}
    hits = web_access.parse_tavily(tavily)
    check(len(hits) == 1 and hits[0]["title"] == "备案须知", "Tavily 结果要解析得出来")
    check(hits[0]["snippet"] == "需要申请备案", "Tavily 片段要带上")

    check("上网失败" in webtools.call("fetch", '{"url": "http://127.0.0.1/"}'),
          "被拒的地址要变成一句给模型看的话")

    # 两路都失败时，自己抓为什么失败也要说出来：只留"抓取接口返回 HTTP 401"
    # 模型分不清是站点挡了还是 key 坏了，只能干看着（服务器上真发生过）。
    fetcher = web_access.WebAccess()
    saved = (web_access._fetch, web_access._DEEPSEEK_KEY, web_access.WebAccess._ds_read)
    web_access._DEEPSEEK_KEY = "k"

    def boom(*args, **kwargs):
        raise web_access.WebAccessError("自己抓取被挡了（HTTP 403）")

    def boom2(*args, **kwargs):
        raise web_access.WebAccessError("抓取接口返回 HTTP 401")

    web_access._fetch = boom
    web_access.WebAccess._ds_read = boom2
    try:
        try:
            fetcher.read("http://93.184.216.34/x")
        except web_access.WebAccessError as exc:
            message = str(exc)
        else:
            message = ""
        check("HTTP 403" in message and "HTTP 401" in message,
              "两路都失败要带上两条原因：%r" % message)
    finally:
        web_access._fetch, web_access._DEEPSEEK_KEY, web_access.WebAccess._ds_read = saved

    check(webtools.NAMES == ("fetch", "search"), "工具名别改")
    names = [tool["function"]["name"] for tool in webtools.tools()]
    check("fetch" in names and "search" in names, "工具面里该有这两个")


def check_shell() -> None:
    """命令工具：关进 Landlock（只有工作区能写）、超时杀进程、输出截断。

    自检要在这台机器上跑（可能是 Windows）：Windows 上没有 Landlock，所以先验
    fail-closed，再明确关掉沙箱验执行链路；Linux 上再把沙箱开回来验边界。
    """
    workdir = tempdir("loop-shell-")
    try:
        python = sys.executable or "python3"
        cfg = _loop_config(name="自检", data_dir=workdir)
        check(not shell.tools(cfg), "默认关：工具面里连 run 都不该有")
        check("没开" in shell.call("run", '{"command": "echo hi"}', cfg),
              "没开时调它要回一句，不该抛")
        cfg.run_enabled = True
        names = [tool["function"]["name"] for tool in shell.tools(cfg)]
        check(names == ["run"], "打开之后工具面里要有 run：%r" % names)

        # 这台机器的说明：现写一份进工作区，讲清哪能写、怎么装库
        written = shell.write_doc(cfg)
        check(os.path.exists(written), "说明要落到工作区里：%s" % written)
        with io.open(written, encoding="utf-8") as handle:
            doc = handle.read()
        check(shell.workspace_of(cfg) in doc, "说明里要写清工作区是哪：%r" % doc[:120])
        check("Landlock" in doc and "pip install --target" in doc,
              "说明里要提到沙箱范围与装库办法")

        ready = shell.sandbox_ready()
        if not ready:
            refused = shell.call("run", json.dumps({"command": "echo hi"}), cfg)
            check("跑不了" in refused,
                  "没有 Landlock 就该 fail-closed，不能裸跑：%r" % refused)

        # 下面验执行链路：明确关掉沙箱（Linux 上最后再把沙箱开回来验边界）。
        # 命令一律写成"跑一个脚本文件"，别用 `-c "..."`——Windows 的 cmd /c 会把嵌套引号拆坏。
        cfg.run_sandbox = False
        root = shell.workspace_of(cfg)
        os.makedirs(root, exist_ok=True)

        def script(name: str, body: str) -> str:
            with io.open(os.path.join(root, name), "w", encoding="utf-8") as handle:
                handle.write(body)
            return name

        out = shell.call("run", json.dumps(
            {"command": "%s %s" % (python, script("sum.py", "print(1 + 1)"))}), cfg)
        check("退出码 0" in out and "2" in out, "该跑起来并给出输出：%r" % out)

        refused = shell.call("run", json.dumps(
            {"command": "cat /etc/palimpsest/credentials.yaml"}), cfg)
        check("不给读" in refused, "凭证该拒：%r" % refused)

        where = shell.call("run", json.dumps(
            {"command": "%s %s" % (python, script(
                "cwd.py", "import os\nprint(os.getcwd())\n"))}), cfg)
        check(os.path.realpath(shell.workspace_of(cfg)) in
              os.path.realpath(where), "命令要在 workspace 里跑：%r" % where)

        bad = shell.call("run", json.dumps(
            {"command": "%s %s" % (python, script(
                "exit3.py", "raise SystemExit(3)\n"))}), cfg)
        check("退出码 3" in bad, "非零退出要如实说：%r" % bad)

        cfg.run_timeout = 0.6
        started = time.time()
        slow = shell.call("run", json.dumps(
            {"command": "%s %s" % (python, script(
                "slow.py", "import time\ntime.sleep(9)\n"))}), cfg)
        check("超时" in slow, "跑过头要杀：%r" % slow)
        check(time.time() - started < 6, "超时之后不能还等在那儿")
        cfg.run_timeout = 20.0

        cfg.run_output_chars = 100
        long = shell.call("run", json.dumps(
            {"command": "%s %s" % (python, script(
                "long.py", "print('x' * 600)\n"))}), cfg)
        check("省略" in long and len(long) < 400, "长输出要截断：%d 字" % len(long))
        marked = re.search(r"省略 (\d+) 字", long)
        check(marked and 400 < int(marked.group(1)) < 620,
              "要标出省了多少：%r" % long[:80])
        cfg.run_output_chars = 4000

        # 管道能用（关掉沙箱的这半边只是验 shell 通道）
        pipe_cmd = "%s %s | %s %s" % (
            python, script("value.py", "print(42)\n"),
            python, script("echo_stdin.py",
                           "import sys\nprint(sys.stdin.read().strip())\n"))
        piped = shell.call("run", json.dumps({"command": pipe_cmd}), cfg)
        check("42" in piped, "管道该通：%r" % piped)

        called = shell.call("other", "{}", cfg)
        check("没有这个工具" in called, "别的名字要回「没有」")

        if ready:
            # Linux：开回沙箱，验内核边界
            cfg.run_sandbox = True
            wrote = shell.call("run", json.dumps(
                {"command": "echo hi > 自检.txt; cat 自检.txt"}), cfg)
            check("hi" in wrote, "工作区里该能写：%r" % wrote)
            outside = shell.call("run", json.dumps(
                {"command": "echo x > ../escape.txt"}), cfg)
            check(("Permission denied" in outside) or ("denied" in outside.lower()),
                  "工作区外该被内核拒：%r" % outside)
            check(not os.path.exists(os.path.join(workdir, "escape.txt")),
                  "确实没写出去")
            read = shell.call("run", json.dumps({"command": "head -1 /etc/hostname"}), cfg)
            check("退出码 0" in read, "工作区外该能读：%r" % read)
            # 工作区里的删改不必绕弯：删文件、删目录、改名都该是普通的成功
            removed = shell.call("run", json.dumps(
                {"command": "mkdir -p 待删 && echo x > 待删/a.txt && rm -r 待删 "
                            "&& echo 删掉了"}), cfg)
            check("删掉了" in removed, "工作区里该能删：%r" % removed)
            devnull = shell.call("run", json.dumps(
                {"command": "echo hi > /dev/null 2>&1 && echo 空设备可用"}), cfg)
            check("空设备可用" in devnull, "`> /dev/null` 该能用：%r" % devnull)

        # 接到循环上：工具面里有它，分派也认它。
        loop_cfg = _loop_config(name="自检", data_dir=workdir,
                                block_tokens=100000)
        loop_cfg.run_enabled = True
        loop_cfg.run_sandbox = False
        wire = LoopStore(os.path.join(workdir, "wire.sqlite3"))
        try:
            loop = Loop(loop_cfg, credentials=None, store=wire,
                        compressor=Compressor(loop_cfg, summarizer=fake_summarizer),
                        memory=MemoryBridge.disabled(), client=FakeClient())
            tool_names = [tool["function"]["name"] for tool in loop.tools()]
            check("run" in tool_names, "循环的工具面里要有 run：%r" % tool_names)
            wired = loop._call_tool("run", json.dumps(
                {"command": "%s %s" % (python, script("seven.py", "print(7)"))}))
            check("7" in wired, "循环分派要能跑通：%r" % wired)
        finally:
            wire.close()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_cli() -> None:
    """循环只有步：入口不许再给步数上限。停是她自己的动作（`wait`）。"""
    from rin import cli

    parser = cli.build_parser()
    defaults = {action.dest: action.default for action in parser._actions}
    check(defaults.get("max_steps") == 0,
          "--max-steps 默认必须是 0（不设上限）：%r" % defaults.get("max_steps"))

    args = parser.parse_args(["--name", "自检"])
    check(args.max_steps == 0, "不传就是 0：%r" % args.max_steps)
    capped = parser.parse_args(["--name", "自检", "--max-steps", "3"])
    check(capped.max_steps == 3, "显式给了数才收尾（自检/短场用）：%r"
          % capped.max_steps)


def check_outbound() -> None:
    """出站闸门：发消息过一道闸，超了不发，并在网页顶上留一条提示。

    被牵着刷屏是收不回来的那种坏（账号被风控、群里被踢），所以这里卡的是"连着往外
    倒"这件事本身，不是内容。
    """
    from rin.tools import qq as qq_tool

    workdir = tempdir("out-")
    try:
        cfg = _loop_config(name="自检", data_dir=workdir)
        stamp = time.mktime(time.strptime("2026-10-29 12:00", "%Y-%m-%d %H:%M"))
        for index in range(limit.PER_MINUTE):
            allowed, why = limit.allow(cfg, moment=stamp + index)
            check(allowed, "第 %d 条该放行：%r" % (index + 1, why))
        allowed, why = limit.allow(cfg, moment=stamp + limit.PER_MINUTE)
        check(not allowed and "一分钟" in why, "撞上一分钟的闸该拒：%r" % why)
        allowed, _ = limit.allow(cfg, moment=stamp + limit.PER_MINUTE + 61)
        check(allowed, "过了这一分钟该能再发（窗口只数最近 60 秒）")
        used = limit.used(cfg, moment=stamp + limit.PER_MINUTE + 61)
        check(used["count"] == limit.PER_MINUTE + 1, "今天的计数要累着：%r" % used)
        tomorrow = stamp + 86400
        allowed, _ = limit.allow(cfg, moment=tomorrow)
        check(allowed and limit.used(cfg, moment=tomorrow)["count"] == 1,
              "跨天要清零：%r" % limit.used(cfg, moment=tomorrow))

        # 一天的闸满了：连 NapCat 都不该去调
        with io.open(limit.path_of(cfg), "w", encoding="utf-8") as handle:
            json.dump({"day": time.strftime("%Y-%m-%d"), "count": limit.PER_DAY,
                       "recent": []}, handle)
        refused = qq_tool.call(
            "qq", json.dumps({"action": "send_private_msg",
                              "params": {"user_id": 10000, "message": "在"}}), cfg)
        check("这条没发" in refused and "今天" in refused,
              "一天的发满了就该拒：%r" % refused)
        check(os.path.isfile(alerts.path_of(cfg)), "闸住之后网页顶上要有一条提示")
        with io.open(alerts.path_of(cfg), encoding="utf-8") as handle:
            data = json.load(handle)
        check(data.get("kind") == "qq_send_blocked", "提示要带 kind：%r" % data)
        check(not alerts.raise_alert(cfg, "qq_send_blocked", "又来一条"),
              "同一种事半小时内只挂一次")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_injection_guard() -> None:
    """凭证闸认 glob、软链接和拼出来的路径；子进程的环境里不带令牌。"""
    workdir = tempdir("cred-")
    try:
        cfg = _loop_config(name="自检", data_dir=workdir)
        folder = os.path.join(workdir, "etc")
        os.makedirs(folder)
        secret = os.path.join(folder, "credentials.yaml")
        with io.open(secret, "w", encoding="utf-8") as handle:
            handle.write("chat_key: sk-test\n")
        plain = os.path.join(workdir, "notes.txt")
        with io.open(plain, "w", encoding="utf-8") as handle:
            handle.write("hi\n")

        check(shell._sensitive("cat %s" % secret),
              "点到凭证文件的名字就该认出来")
        check(shell._sensitive("cat %s" % os.path.join(folder, "cred*")),
              "glob 展开之后是凭证，也要认出来")
        check(shell._sensitive("cat %s" % os.path.join(workdir, "etc", "..",
                                                       "etc", "credentials.yaml")),
              "拼出来的路径（`..`）也要认出来")
        check(not shell._sensitive("cat %s" % plain),
              "普通文件不许误伤：%r" % shell._sensitive("cat %s" % plain))
        if os.name != "nt":
            check(shell._sensitive("cat /etc/palimp*/cred*"),
                  "Linux 上 `/etc/palimp*/cred*` 这种也要认出来")

        out = shell.execute("cat %s" % secret, cfg)
        check("不给读" in out, "拦下的时候要说清楚：%r" % out)
        check(os.path.isfile(alerts.path_of(cfg)),
              "有人让 `run` 去碰凭证，网页顶上要留一条")

        os.environ["LOOP_CHAT_KEY"] = "sk-should-not-pass"
        os.environ["NAPCAT_TOKEN"] = "token-should-not-pass"
        try:
            env = shell._child_env()
        finally:
            os.environ.pop("LOOP_CHAT_KEY", None)
            os.environ.pop("NAPCAT_TOKEN", None)
        check("LOOP_CHAT_KEY" not in env and "NAPCAT_TOKEN" not in env,
              "子进程的环境里不该带令牌：%r" % sorted(env)[:6])
        check("PATH" in env, "别的一概留着（PATH 之类的不能少）")
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def check_image() -> None:
    """图：落地、写进 meta、正文只留一行事实；核心把清单拼成 image_url 段。

    这一侧**不再调视觉模型**——图跟着消息进对话，她自己的模型就看得见。
    """
    import base64

    from PIL import Image
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from loop.driver import _inbox_entry

    workdir = tempdir("img-")
    httpd = None
    try:
        cfg = _loop_config(name="自检", data_dir=workdir)
        buffer = io.BytesIO()
        Image.new("RGB", (2000, 1000), (240, 240, 235)).save(buffer, format="PNG")
        encoded = base64.b64encode(buffer.getvalue()).decode("ascii")

        block, landed = web._image_block(workdir, [{"b64": encoded}])
        check("（一张 2000×1000 的图：收件/" in block,
              "正文里要留一行事实：%r" % block)
        check(len(landed) == 1 and landed[0]["path"].startswith("workspace/收件/"),
              "清单里的路径要相对数据目录：%r" % (landed,))
        check(landed[0]["w"] == 2000 and landed[0]["h"] == 1000,
              "尺寸要记下来：%r" % (landed[0],))
        check(os.path.isfile(os.path.join(workdir, landed[0]["path"])),
              "说要落地就要真在盘上")
        check(web._image_block(workdir, []) == ("", []), "没有图就什么都不加")
        check("没取到" in web._image_block(
            workdir, [{"url": "http://127.0.0.1:1/x.png"}])[0],
            "取不到就说取不到")

        # 核心那侧：带图的进件变成段数组，纯文字的仍是字符串
        entry = _inbox_entry({"text": "看这个", "meta": {"images": landed}})
        check(isinstance(entry["content"], list), "带图的进件要变成段数组")
        check(entry["content"][0] == {"type": "text", "text": "看这个"},
              "第一段是文字：%r" % (entry["content"][0],))
        check(entry["content"][1]["type"] == "image"
              and entry["content"][1]["path"] == landed[0]["path"],
              "第二段是图：%r" % (entry["content"][1],))
        plain = _inbox_entry({"text": "就一句"})
        check(isinstance(plain["content"], str), "没有图还是字符串")

        # 真走一遍 /inbox：进件正文有一行事实，meta 里挂图清单
        db_path = os.path.join(workdir, "loop.sqlite3")
        store = LoopStore(db_path)
        httpd = web.serve(cfg, db_path, "tok", "127.0.0.1", 0)
        body = json.dumps({"source": "QQ", "chat_type": "private", "chat_id": "1",
                           "sender_name": "甲", "text": "",
                           "images": [{"b64": encoded}],
                           "ext_id": "img-1"}).encode("utf-8")
        request = urllib.request.Request(
            "http://127.0.0.1:%d/inbox" % httpd.server_address[1],
            data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", "Bearer tok")
        with urllib.request.urlopen(request, timeout=20) as response:
            check(response.status == 200, "只发一张图也算一条进件")
        items = store.inbox_recent(5)
        text = str((items[0] or {}).get("text") or "")
        meta = (items[0] or {}).get("meta") or {}
        check("（一张 2000×1000 的图：" in text, "进件正文里要有事实行：%r" % text)
        check(meta.get("images") and meta["images"][0]["path"].startswith("workspace/"),
              "图清单要挂进 meta：%r" % (meta,))
        check(store.inbox_has_ext("QQ:img-1"), "只带图的进件也要记账")
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/" % httpd.server_address[1], timeout=5) as response:
            page = response.read().decode("utf-8")
        check('id="pick"' in page and 'id="attached"' in page, "网页上要有发图的入口")
        store.close()
    finally:
        if httpd is not None:
            httpd.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)


def check_xdao() -> None:
    """X岛 只读那一层：HTML 摊平、板块/串/回复怎么拼、出错说什么。

    接口一律走假数据（`xdao.api` 是可以换掉的入口），所以不联网；真实响应形状是按
    2026-10-29 实测的字段抄的（`user_hash` 其实是显示的名字、`img`+`ext` 拼图床地址）。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from rin.tools import xdao

    workdir = tempdir("xdao-")
    real_api = xdao.api
    seen = []
    try:
        cfg = _loop_config(name="自检", data_dir=workdir)

        check(xdao.plain("<b>你好</b><br>第二行 &gt;&gt;No.1&amp;") ==
              "你好\n第二行 >>No.1&", "HTML 要摊平、实体要解开：%r"
              % xdao.plain("<b>你好</b><br>第二行 &gt;&gt;No.1&amp;"))
        check(xdao.plain("  a \u3000 b \n\n\n\nc ") == "a b\n\nc", "空白要收一收")
        check(xdao.image_of({"img": "2022-12-04/638c87770b738", "ext": ".jpg"}) ==
              "https://image.nmb.best/image/2022-12-04/638c87770b738.jpg", "图床地址")
        check(xdao.image_of({}) == "", "没有图就是空串")

        def fake(path, config=None):
            seen.append(path)
            if path.startswith("/Api/getForumList"):
                return [{"name": "综合", "forums": [
                    {"id": "-1", "name": "时间线"},
                    {"id": "4", "name": "综合版1"},
                    {"id": "20", "name": "欢乐恶搞"}]},
                        {"name": "创作", "forums": [{"id": "5", "name": "创作"}]}]
            if path.startswith("/Api/showf"):
                return [{"id": 52752005, "ReplyCount": 7,
                         "now": "2022-10-16(日)23:14:27", "title": "无标题",
                         "content": "欢迎来到X岛<br>网页端地址：www.nmbxd.com"},
                        {"id": 52752006, "ReplyCount": 0, "now": "2026-10-29 22:00:00",
                         "title": "一个标题", "content": "正文"},
                        # 回读用的：发出去那条要在板上找得到
                        {"id": 69700001, "ReplyCount": 0, "now": "2026-10-29 23:00:00",
                         "title": "无标题", "content": "第一条"},
                        {"id": 69700002, "ReplyCount": 0, "now": "2026-10-29 23:01:00",
                         "title": "无标题", "content": "中和的一条"}]
            if path.startswith("/Api/thread"):
                replies = [{"id": 9999999, "user_hash": "Tips",
                            "now": "2099-01-01 00:00:01", "name": "无名氏",
                            "content": "回一句<br>再一行", "img": "", "ext": ""}]
                return {"id": 50000002, "fid": 4, "ReplyCount": 41,
                        "title": "无标题", "now": "2026-10-01 10:00:00",
                        "user_hash": "Admin", "name": "无名氏",
                        "content": "置顶串正文", "img": "2026-02-12/abc", "ext": ".png",
                        "Replies": replies}
            raise AssertionError("没料到的地址：%s" % path)

        xdao.api = fake
        schemas = xdao.tools(cfg)
        check(len(schemas) == 1 and schemas[0]["function"]["name"] == "xdao",
              "xdao 该有且只有一个 schema")
        check(schemas[0]["function"]["parameters"]["properties"]["action"]["enum"] ==
              ["forums", "list", "read", "post", "reply", "search", "where"],
              "七个动作")

        forums = xdao.call("xdao", json.dumps({"action": "forums"}), cfg)
        check("综合：4 综合版1 · 20 欢乐恶搞" in forums, "板块要分组列 id：%r" % forums)
        check("时间线" not in forums, "-1 是时间线，不是能发串的板块")

        listing = xdao.call("xdao", json.dumps(
            {"action": "list", "params": {"fid": 4}}), cfg)
        check(seen[-1] == "/Api/showf?id=4&page=1", "翻页参数要带上：%r" % seen[-1])
        check("No.52752005 欢迎来到X岛" in listing and "（回 7，" in listing,
              "没有标题就拿正文开头当标题：%r" % listing[:120])
        check("No.52752006 一个标题（回 0，" in listing, "有标题就用标题")
        check("xdao(action=\"read\"" in listing, "列表末尾要告诉她怎么往下看")
        check("<br>" not in listing, "别把 HTML 漏进记录")

        thread = xdao.call("xdao", json.dumps(
            {"action": "read", "params": {"id": 50000002}}), cfg)
        check(thread.startswith("No.50000002「无标题」 · Admin"), "串头要有号和名字")
        check("置顶串正文" in thread, "串主要正文")
        check("图：https://image.nmb.best/image/2026-02-12/abc.png" in thread,
              "图要给能直接看的地址：%r" % thread[:200])
        check(">>No.9999999 · Tips · 2099-01-01 00:00:01" in thread, "回复要有号和人")
        check("回一句\n再一行" in thread, "回复里的换行要保下来")
        check("page=2" in thread, "回复没读完要提示翻页")

        # 页号夹在 1..50
        xdao.call("xdao", json.dumps({"action": "read", "params": {"id": 1, "page": 0}}), cfg)
        check(seen[-1].endswith("page=1"), "第 0 页按第 1 页算：%r" % seen[-1])
        xdao.call("xdao", json.dumps(
            {"action": "read", "params": {"id": 1, "page": 999}}), cfg)
        check(seen[-1].endswith("page=50"), "页号有上限：%r" % seen[-1])

        # 太长要截断，别把一次回复塞满整块记录
        xdao.MAX_OUT, real_max = 100, xdao.MAX_OUT
        try:
            long = xdao.call("xdao", json.dumps(
                {"action": "read", "params": {"id": 50000002}}), cfg)
            check("…（后面还有" in long and len(long) < 400,
                  "超长要截断并说清楚：%d 字" % len(long))
        finally:
            xdao.MAX_OUT = real_max

        # 出错与缺参数：都给一句能读的话，不抛
        def broken(path, config=None):
            raise xdao.XdaoError("岛上回了 502（%s）" % path)

        xdao.api = broken
        check("岛那边没成" in xdao.call("xdao", json.dumps({"action": "forums"}), cfg),
              "接口挂了要说一句实话")
        xdao.api = fake
        check("先 forums" in xdao.call("xdao", json.dumps({"action": "list"}), cfg),
              "没给 fid 就提示先去拿")
        check("给 id" in xdao.call("xdao", json.dumps({"action": "read"}), cfg),
              "没给 id 就提示")
        check("没有这个动作" in xdao.call("xdao", json.dumps({"action": "like"}), cfg),
              "不认识的动作要明说")
        check("不是 JSON" in xdao.call("xdao", "{", cfg), "坏参数也要回一句话")

        # ── 写：发串 / 回串 ─────────────────────────────────────────────────
        # 岛那边用一个小服务器假扮：它记录收到的 multipart 与 Cookie，
        # 正文里带「坏」就回错误页，其余回「发表成功」。
        posted = []
        reads = []

        class _Island(BaseHTTPRequestHandler):
            def log_message(self, *args):
                return

            def do_GET(self):
                reads.append({"path": self.path,
                              "cookie": self.headers.get("Cookie") or ""})
                if "id=27" in self.path:
                    payload = {"error": "该版面需要饼干", "success": False}
                else:
                    payload = [{"id": 1, "title": "t", "content": "x",
                                "ReplyCount": 0, "now": ""}]
                raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                posted.append({"path": self.path.split("?")[0],
                               "cookie": self.headers.get("Cookie") or "",
                               "type": self.headers.get("Content-Type") or "",
                               "body": body.decode("utf-8", "replace")})
                if "坏" in body.decode("utf-8", "replace"):
                    page = ('<div class="system-message"><h1>:(</h1>'
                            '<p class="error">内容和图片不能同时为空</p></div>')
                elif "中和" in body.decode("utf-8", "replace"):
                    # 既不报错也不说成功：模拟岛那种只回一张空跳转页的情况
                    page = '<div class="system-message"><p>好的</p></div>'
                else:
                    page = ('<div class="system-message"><h1>:)</h1>'
                            '<p class="success">发表成功</p>'
                            '<a href="/t/69703400">看看</a></div>')
                raw = page.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        island = ThreadingHTTPServer(("127.0.0.1", 0), _Island)
        threading.Thread(target=island.serve_forever, daemon=True).start()
        os.environ["XDAO_BASE"] = "http://127.0.0.1:%d" % island.server_address[1]
        for key in ("XDAO_COOKIE",):
            os.environ.pop(key, None)
        try:
            check("饼干" in xdao.call("xdao", json.dumps(
                {"action": "post", "params": {"fid": 57, "content": "在"}}), cfg),
                "没有饼干就不该往外发")
            os.environ["XDAO_COOKIE"] = "test-cookie"
            check("板块" in xdao.call("xdao", json.dumps(
                {"action": "post", "params": {"content": "在"}}), cfg), "发串要 fid")
            check("哪个串" in xdao.call("xdao", json.dumps(
                {"action": "reply", "params": {"content": "在"}}), cfg), "回串要 id")
            check("正文" in xdao.call("xdao", json.dumps(
                {"action": "post", "params": {"fid": 57, "content": "  "}}), cfg),
                "空正文不往外发")
            check("太长" in xdao.call("xdao", json.dumps(
                {"action": "post", "params": {"fid": 57, "content": "字" * 1201}}), cfg),
                "一条装不下的先拦住")

            # 清一下闸：这个临时目录是新的，桶是空的
            said = xdao.call("xdao", json.dumps(
                {"action": "post", "params": {"fid": 57, "title": "试试",
                                              "content": "第一条"}}), cfg)
            check("发出去了" in said and "69703400" in said,
                  "发出去要把串号带回来：%r" % said)
            sent = [record for record in posted
                    if record["path"] == "/Home/Forum/doPostThread.html"]
            check(bool(sent), "发串走的是网页表单那个地址：%r"
                  % [record["path"] for record in posted])
            check(sent[-1]["cookie"] == "userhash=test-cookie",
                  "要带 cookie：%r" % sent[-1]["cookie"])
            check(sent[-1]["type"].startswith("multipart/form-data"),
                  "正文要是 multipart：%r" % sent[-1]["type"])
            for piece in ('name="fid"', 'name="content"', 'name="title"',
                          'name="name"', "第一条"):
                check(piece in sent[-1]["body"], "multipart 里缺 %r" % piece)
            # 发完自动订阅：站上的"订阅"列表里有它，轮询那条路才认得
            followed = [record for record in posted if record["path"] == "/Api/addFeed"]
            check(bool(followed) and "tid=69703400" in followed[-1]["body"],
                  "发完要把这条串订阅上：%r" % (followed[-1]["body"][:80] if followed else None))
            check("uuid=" in followed[-1]["body"], "订阅要带订阅 ID")

            said = xdao.call("xdao", json.dumps(
                {"action": "reply", "params": {"id": 50000002, "content": "回一句"}}), cfg)
            check("发出去了" in said, "回串也要能发：%r" % said)
            replies = [record for record in posted
                       if record["path"] == "/Home/Forum/doReplyThread.html"]
            check(bool(replies), "回串走 doReplyThread")
            check('name="resto"' in replies[-1]["body"] and "50000002" in replies[-1]["body"],
                  "回串要带 resto")

            # 闸门：岛上这个桶一分钟两条
            check("没发" in xdao.call("xdao", json.dumps(
                {"action": "post", "params": {"fid": 57, "content": "第三条"}}), cfg),
                "一分钟两条之后要拦住")
            check(os.path.isfile(alerts.path_of(cfg)), "闸住之后网页顶上要留一条")
            with io.open(alerts.path_of(cfg), encoding="utf-8") as handle:
                check(json.load(handle).get("kind") == "xdao_post_blocked",
                      "提示要带 xdao_post_blocked")

            # 服务端拒了：把那句话带回来，别假装发成功
            limit_reset = os.path.join(workdir, "outbound.json")
            os.remove(limit_reset)
            said = xdao.call("xdao", json.dumps(
                {"action": "post", "params": {"fid": 57, "content": "坏的"}}), cfg)
            check("岛那边说" in said and "不能同时为空" in said,
                  "服务端拒了要照实说：%r" % said)

            # 回话里什么都不说（岛的真实情况）：回板上找一次，把号捞回来
            os.remove(limit_reset)
            said = xdao.call("xdao", json.dumps(
                {"action": "post", "params": {"fid": 57, "content": "中和的一条"}}), cfg)
            check("发出去了：No.69700002" in said,
                  "没回号就回读一次，别只说一句不确定：%r" % said)

            # 读也要带饼干（受限版面不带就回 {"error": …}），那句 error 要说人话
            xdao.api = real_api
            reads = []
            data = xdao.api("/Api/showf?id=4&page=1", cfg)
            check(isinstance(data, list), "读接口该回数组")
            check(reads and reads[-1]["cookie"] == "userhash=test-cookie",
                  "有饼干时读也要带上：%r" % (reads[-1] if reads else None))
            try:
                xdao.api("/Api/showf?id=27&page=1", cfg)
            except xdao.XdaoError as exc:
                check("需要饼干" in str(exc), "受限版面要把它那句话带出来：%r" % str(exc))
            # ── 搜索：社区那台全文检索（假数据） ─────────────────────────
            real_search = xdao._search_json
            try:
                body = {"total": {"threads": 2, "replies": 40, "truncated": False,
                                  "cap": 1000},
                        "results": [
                            {"id": 69696800, "kind": "thread", "fid": 27,
                             "title": "无标题", "content": "求肥哥参谋订阅",
                             "post_time": "2026-10-28T12:00:00", "reply_count": 5,
                             "user_hash": "2rAvVQK"},
                            {"id": 69688518, "kind": "reply", "fid": 27, "title": "",
                             "content": "随便找个agent许愿",
                             "post_time": "2026-10-27T12:00:00", "reply_count": 0,
                             "user_hash": "Je8IKY1"}]}
                seen_search = []

                def fake_search(path, params, config=None):
                    seen_search.append((path, dict(params)))
                    if path.startswith("/api/v1/thread-of/"):
                        if path.endswith("/64792841"):
                            return {"kind": "thread", "thread_id": 64792841}, None
                        return {"kind": "reply", "thread_id": 64792841}, None
                    return body, "147"

                xdao._search_json = fake_search
                said = xdao.call("xdao", json.dumps(
                    {"action": "search", "params": {"q": "显卡", "size": 5}}), cfg)
                check("搜「显卡」· 串 2 · 回复 40 · 全部 · 相关度 · 今天还剩 147 次"
                      in said, "搜索抬头要报份数、范围和余量：%r" % said.splitlines()[0])
                check("No.69696800 [27] 2026-10-28 · 回 5 · 2rAvVQK" in said,
                      "串那条要一行能读：%r" % said[:200])
                check("（回复）" in said and "where" in said,
                      "回复那条要标明是回复、并给出反查提示")
                check(seen_search[-1][0] == "/api/v1/search"
                      and seen_search[-1][1]["q"] == "显卡"
                      and seen_search[-1][1]["size"] == 5,
                      "参数要照传：%r" % (seen_search[-1],))
                check("要搜什么" in xdao.call("xdao", json.dumps(
                    {"action": "search", "params": {}}), cfg), "空搜索要给一句话")

                check(xdao.call("xdao", json.dumps(
                    {"action": "where", "params": {"id": 64792841}}), cfg) ==
                      "No.64792841 本身就是一条串。", "反查：串本身")
                check(xdao.call("xdao", json.dumps(
                    {"action": "where", "params": {"id": 64792888}}), cfg) ==
                      "No.64792888 是串 No.64792841 里的回复。", "反查：回复属于哪个串")
                check("要给编号" in xdao.call("xdao", json.dumps(
                    {"action": "where", "params": {"id": "abc"}}), cfg), "反查要数字")

                def broken_search(path, params, config=None):
                    raise xdao.XdaoError("搜索那边今天的额度用完了")

                xdao._search_json = broken_search
                check("额度用完" in xdao.call("xdao", json.dumps(
                    {"action": "search", "params": {"q": "x"}}), cfg),
                    "搜索被限流要照实说")
            finally:
                xdao._search_json = real_search
        finally:
            for key in ("XDAO_COOKIE", "XDAO_BASE"):
                os.environ.pop(key, None)
            island.shutdown()
    finally:
        xdao.api = real_api
        shutil.rmtree(workdir, ignore_errors=True)


def check_xdao_feed() -> None:
    """岛上那条"听"的通道：只问她自己的串、只攒着、不叫醒她。

    `xdao.api` 和网关都换成假的，所以不联网；但走的是真的 `collect` / `poll_once` / 记账。
    """
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    from rin.tools import xdao
    from rin.xdao import feed, threads

    workdir = tempdir("xfeed-")
    real_api = xdao.api
    inbox = []

    class _Gateway(BaseHTTPRequestHandler):
        def log_message(self, *args):
            return

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            inbox.append({"item": body,
                          "auth": self.headers.get("Authorization") or ""})
            raw = json.dumps({"ok": True, "id": len(inbox)}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

    gateway = ThreadingHTTPServer(("127.0.0.1", 0), _Gateway)
    threading.Thread(target=gateway.serve_forever, daemon=True).start()
    base = "http://127.0.0.1:%d" % gateway.server_address[1]
    try:
        cfg = _loop_config(name="自检", data_dir=workdir)
        settings.save(workdir, {"xdao_name": "wPFEjuL"})

        check(threads.known(cfg) == {}, "一开始没有她自己的串")
        threads.remember(cfg, "69703583", "117", "接入测试")
        check("69703583" in threads.known(cfg), "记下来的串要在账上")
        check(threads.seen(cfg, "69703583") == 0, "还没送过任何一条")
        threads.note_seen(cfg, "69703583", 100)
        threads.note_seen(cfg, "69703583", 90)
        check(threads.seen(cfg, "69703583") == 100, "只往前走，不后退")

        first = threads.feed_uuid(cfg)
        check(first and threads.feed_uuid(cfg) == first,
              "订阅 ID 只生成一次、之后复用（first=%r）" % (first,))
        check(first in json.dumps(threads.load(cfg)), "订阅 ID 跟这份账存在一起")

        def fake(path, config=None):
            if path.startswith("/Api/thread"):
                return {"id": 69703583, "ReplyCount": 3, "Replies": [
                    {"id": 101, "user_hash": "wPFEjuL", "now": "2026-10-30 10:00",
                     "content": "这是她自己回的一句"},
                    {"id": 102, "user_hash": "Tips", "now": "2026-10-30 10:01",
                     "content": "别人回了一句<br>还带换行"},
                    {"id": 103, "user_hash": "Boen", "now": "2026-10-30 10:02",
                     "content": "又一个人回"}]}
            raise AssertionError("没料到的地址：%s" % path)

        xdao.api = fake
        items, marks = feed.collect(cfg, lambda text: None)
        check(len(items) == 2, "自己说的那条不该再告诉她：%d 条" % len(items))
        check(items[0]["policy"] == "batch",
              "一律只攒着、不叫醒她：%r" % items[0]["policy"])
        check(items[0]["chat_type"] == "thread"
              and items[0]["chat_id"] == "xdao:69703583", "会话身份要带通道前缀")
        check(items[0]["ext_id"] == "r102" and items[0]["source"] == "岛",
              "外部编号要能去重：%r" % items[0])
        check("还带换行" in items[0]["text"], "回复正文要摊成纯文字：%r" % items[0]["text"])
        check(marks["69703583"] == 103, "这一轮看到的最大号：%r" % marks)

        added = feed.poll_once(cfg, base, "tok", lambda text: None)
        check(added == 2 and len(inbox) == 2, "两条都要推进去：%r" % added)
        check(inbox[0]["auth"] == "Bearer tok", "推给网关要带令牌")
        check(inbox[0]["item"]["chat_name"] == "接入测试", "会话名字要带上串标题")
        check(threads.seen(cfg, "69703583") == 103, "推进去了才记账")

        check(feed.poll_once(cfg, base, "tok", lambda text: None) == 0
              and len(inbox) == 2, "同一个号不该推第二遍")
    finally:
        xdao.api = real_api
        gateway.shutdown()
        shutil.rmtree(workdir, ignore_errors=True)


def check_guard() -> None:
    """NapCat 守护的判据：只数最后一次登录成功之后的 Login Error。"""
    import importlib.util

    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "deploy", "napcat-guard.py")
    check(os.path.exists(path), "守护脚本要在 deploy 里：%s" % path)
    spec = importlib.util.spec_from_file_location("napcat_guard", path)
    guard = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(guard)

    log = "\n".join([
        "13:00:00 [error] [Core] [Login] Login Error,ErrType: 1 ErrCode: 3",
        "13:02:00 [info] [OneBot] [HTTP Server Adapter] Start On 127.0.0.1:3000",
        "13:04:00 [error] [Core] [Login] Login Error,ErrType: 1 ErrCode: 3",
        "13:06:00 [error] [Core] [Login] Login Error,ErrType: 1 ErrCode: 3",
    ])
    check(guard.login_errors_since_success(log) == 2,
          "成功之前的账不算：%r" % guard.login_errors_since_success(log))
    check(guard.login_errors_since_success("") == 0, "没有日志就是 0")
    check(guard.login_errors_since_success(
        "13:00:00 [Login] Login Error\n13:01:00 [Login] Login Error") == 2,
        "没成功过就全算")
