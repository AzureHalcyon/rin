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

from rin import app, settings, tool_texts
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

        _, feed = get("/feed?since=0:0")
        check(any("在吗" in item["text"] for item in feed["entries"]),
              "流里该有刚说的话")
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
        store.close()
    finally:
        if httpd is not None:
            httpd.shutdown()
            httpd.server_close()
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
        check(len(payload["prompts"]) == len(prompt_files.META),
              "每份提示词都要出现在设置里")

        fields = {item["key"]: item for item in payload["fields"]}
        check("embed_model" in fields and fields["embed_model"]["group"] == "记忆",
              "设置里该有嵌入模型")
        check(fields["embed_model"].get("picker") == "embed", "嵌入模型该能从列表挑")
        check(fields["model"].get("picker") == "chat", "主模型该能从列表挑")
        check(fields["summary_provider"]["type"] == "select", "摘要端点该是下拉")
        check(payload["memory"]["embed_dim"] > 0, "设置里该报嵌入维度")
        check(call("/models?kind=chat")["ok"] is False,
              "没凭证时拉模型列表该老实说不行，而不是 500")

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


