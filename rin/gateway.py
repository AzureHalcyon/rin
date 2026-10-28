"""网页面：说、听、看。

    POST /say       {"text": "..."}       → 进 inbox，立刻返回
    GET  /feed?since=3:12&step=40         → 增量原文 + 增量用量
    GET  /context                         → 当前上下文（sink / 旧段 / 两块原文）
    GET  /graph                           → 事件图（一页 HTML，数据内嵌）
    POST /edits                           → 把页面攒的编辑清单存到 <data>/edits/
    GET  /state                           → 它现在在干什么、今天用了多少
    GET  /                                → 单页界面

★ 这一层**只读库、只往 inbox 写**，从不碰 blocks/entries/bands 的写路径：
  单写者仍然是那个循环进程。所以网页崩了顶多是看不见，不会把记录弄坏。
★ 每条连接各自开一个 LoopStore（SQLite 连接不能跨线程共享）；库开了 WAL，
  读不挡写。
★ 鉴权是 Bearer token：没有令牌一律 401，界面本身也走同一道门。
"""

from __future__ import annotations

import hmac
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from loop import control, persona, prompt_files
from rin import settings
from loop.context import Context, estimate_tokens, serialize_entries
from loop.store import LoopStore

_INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web", "index.html")
_local = threading.local()


def _store_of(server) -> LoopStore:
    """每线程一条连接：SQLite 的连接不能跨线程用。"""
    store = getattr(_local, "store", None)
    path = getattr(_local, "path", "")
    if store is None or path != server.db_path:
        store = LoopStore(server.db_path)
        _local.store = store
        _local.path = server.db_path
    return store


def _entry_view(entry) -> dict:
    message = entry.message
    return {"role": str(message.get("role") or ""),
            "text": str(message.get("content") or ""),
            "calls": list(message.get("tool_calls") or []),
            "call_id": str(message.get("tool_call_id") or ""),
            "seq": int(getattr(entry, "seq", 0) or 0),
            "ts": str(entry.ts),
            "meta": dict(entry.meta or {})}


def _mtime(path: str) -> str:
    try:
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(os.path.getmtime(path)))
    except OSError:
        return ""


def _block_view(block, fold_keep: int, user_name: str = "") -> dict:
    rendered = serialize_entries(block.entries, fold_keep=fold_keep,
                                 user_name=user_name)
    return {"start_ts": str(block.start_ts), "end_ts": str(block.end_ts),
            "count": len(block.entries),
            "chars": len(rendered),
            "tokens": estimate_tokens(rendered),
            "entries": [_entry_view(entry) for entry in block.entries]}


class _Handler(BaseHTTPRequestHandler):
    server_version = "palimpsest"
    protocol_version = "HTTP/1.1"

    # 命令行里不要刷访问日志：它跟聊天记录混在一起看不清。
    def log_message(self, format, *args):  # noqa: A002
        return

    # ── 工具 ──────────────────────────────────────────────────────────────
    def _send(self, code: int, payload, content_type="application/json; charset=utf-8"):
        body = payload if isinstance(payload, bytes) else \
            json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            # 浏览器关页、刷新、点走：这条连接已经没了，没什么可做的。
            return

    def _authorized(self) -> bool:
        token = str(self.server.token or "")
        if not token:
            return True
        given = str(self.headers.get("Authorization") or "")
        prefix = "Bearer "
        if not given.startswith(prefix):
            return False
        return hmac.compare_digest(given[len(prefix):].strip(), token)

    def _guard(self) -> bool:
        if self._authorized():
            return True
        self._send(401, {"error": "令牌不对"})
        return False

    # ── GET ───────────────────────────────────────────────────────────────
    def do_GET(self):  # noqa: N802
        route = urlsplit(self.path)
        if route.path in ("/", "/index.html"):
            try:
                with open(_INDEX, "rb") as handle:
                    body = handle.read()
            except OSError:
                return self._send(500, {"error": "界面文件不见了"})
            return self._send(200, body, "text/html; charset=utf-8")
        if route.path == "/favicon.ico":
            return self._send(204, b"")
        if not self._guard():
            return
        try:
            if route.path == "/state":
                return self._send(200, self._state())
            if route.path == "/context":
                return self._send(200, self._context())
            if route.path == "/settings":
                return self._send(200, self._settings_payload())
            if route.path == "/models":
                return self._send(200, self._models(parse_qs(route.query)))
            if route.path == "/feed":
                return self._send(200, self._feed(parse_qs(route.query)))
            if route.path == "/graph":
                body = self._graph(parse_qs(route.query))
                return self._send(200, body.encode("utf-8"),
                                  "text/html; charset=utf-8")
        except Exception as exc:                     # 页面不该因为一条查询挂掉
            return self._send(500, {"error": "内部出错：%s" % exc})
        return self._send(404, {"error": "没有这个地址"})

    def do_POST(self):  # noqa: N802
        if not self._guard():
            return
        route = urlsplit(self.path)
        try:
            if route.path == "/say":
                return self._say()
            if route.path == "/inbox":
                return self._inbox()
            if route.path == "/edits":
                return self._save_edits()
            if route.path == "/persona":
                return self._persona()
            if route.path == "/restart":
                return self._restart()
        except Exception as exc:
            return self._send(500, {"error": "内部出错：%s" % exc})
        return self._send(404, {"error": "没有这个地址"})

    def do_DELETE(self):  # noqa: N802
        if not self._guard():
            return
        route = urlsplit(self.path)
        if route.path != "/entries":
            return self._send(404, {"error": "没有这个地址"})
        query = parse_qs(route.query)
        try:
            block = int((query.get("block") or ["0"])[0])
            seq = int((query.get("seq") or ["0"])[0])
            removed = _store_of(self.server).delete_entry(block, seq)
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        # 正在跑的那个进程手里还留着旧的块：让它重读一遍，不用重启。
        control.request_reload()
        return self._send(200, {"ok": True, "removed": removed, "restart": False})

    def do_PUT(self):  # noqa: N802
        if not self._guard():
            return
        route = urlsplit(self.path)
        try:
            if route.path == "/settings":
                return self._save_settings()
            if route.path == "/prompts":
                return self._save_prompt()
        except Exception as exc:
            return self._send(500, {"error": "内部出错：%s" % exc})
        return self._send(404, {"error": "没有这个地址"})

    # ── 写 ────────────────────────────────────────────────────────────────
    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(min(length, 512 * 1024)) if length else b"{}"
        data = json.loads(raw.decode("utf-8") or "{}")
        if not isinstance(data, dict):
            raise ValueError("要一个对象")
        return data

    def _say(self):
        payload = self._body()
        text = str(payload.get("text") or "").strip()
        if not text:
            return self._send(400, {"error": "没有正文"})
        item_id = _store_of(self.server).push("user", text)
        return self._send(200, {"ok": True, "id": item_id})

    def _inbox(self):
        """通道适配器往这里推一条进件（QQ、电报、脚本都一样）。

        字段：text 必填；kind 默认 user；其余 source / chat_type / chat_id /
        chat_name / sender_id / sender_name / policy / meta 可选。
        policy 不传就继承这个会话在 chats 里的策略。
        """
        payload = self._body()
        text = str(payload.get("text") or "").strip()
        if not text:
            return self._send(400, {"error": "没有正文"})
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        item_id = _store_of(self.server).push(
            str(payload.get("kind") or "user"), text,
            policy=str(payload.get("policy") or ""),
            source=str(payload.get("source") or ""),
            chat_type=str(payload.get("chat_type") or ""),
            chat_id=str(payload.get("chat_id") or ""),
            chat_name=str(payload.get("chat_name") or ""),
            sender_id=str(payload.get("sender_id") or ""),
            sender_name=str(payload.get("sender_name") or ""),
            meta=meta)
        return self._send(200, {"ok": True, "id": item_id})

    def _save_settings(self):
        payload = self._body()
        before_channel = settings.channel(settings.load(self.server.data_dir))
        try:
            stored = settings.save(self.server.data_dir, payload.get("values") or {})
        except settings.SettingsError as exc:
            return self._send(400, {"error": str(exc)})
        self._apply_live(stored)
        if settings.channel(stored) != before_channel:
            self._rebuild_channel(stored)
        return self._send(200, {"ok": True, "stored": settings.mask(stored),
                                "restart": False})

    def _apply_live(self, stored: dict) -> None:
        """旋钮改了立刻生效：这几项都是每步现读的，不用等重启。

        有两处不能只改配置：主模型藏在客户端自己的配置里；摘要客户端和嵌入器
        是**造好就带着模型名**的，得按新配置重建一个。
        """
        config = self.server.config
        before = {key: getattr(config, key, None) for key in
                  ("model", "temperature", "max_tokens", "summary_model",
                   "summary_provider", "embed_model", "embed_dim")}
        settings.apply(config, stored)
        loop = getattr(self.server, "loop", None)

        client_config = getattr(getattr(loop, "client", None), "config", None)
        if client_config is not None:
            client_config.model = config.model
            client_config.temperature = config.temperature
            client_config.max_tokens = config.max_tokens
            client_config.reasoning_effort = str(
                getattr(config, "reasoning_effort", "") or "")

        if (before["summary_model"] != config.summary_model
                or before["summary_provider"] != config.summary_provider):
            compressor = getattr(loop, "compressor", None)
            rebuild = getattr(compressor, "rebuild", None)
            if callable(rebuild):
                rebuild()

        if (before["embed_model"] != config.embed_model
                or before["embed_dim"] != config.embed_dim):
            factory = getattr(self.server, "embed_factory", None)
            embedder = None
            if callable(factory):
                try:
                    embedder = factory()
                except Exception:
                    embedder = None
            memory = getattr(loop, "memory", None)
            if memory is not None:
                memory.embedder = embedder

    def _note(self, text: str) -> None:
        """网页这边出的岔子记进循环的日志（没有循环就打到 stdout）。"""
        loop = getattr(self.server, "loop", None)
        log = getattr(loop, "log", None)
        try:
            if callable(log):
                log(text)
            else:
                print(text, flush=True)
        except Exception:
            pass

    def _rebuild_channel(self, stored: dict) -> None:
        """渠道换了：盖到凭证上，重建对话/摘要/嵌入三处客户端，清掉模型缓存。"""
        credentials = getattr(self.server, "credentials", None)
        settings.apply_credentials(credentials, stored)
        loop = getattr(self.server, "loop", None)
        config = self.server.config
        if credentials is not None and loop is not None:
            from loop.compress import make_client

            try:
                loop.client = make_client(config, credentials, config.model,
                                          temperature=config.temperature,
                                          max_tokens=config.max_tokens)
            except Exception as exc:
                self._note("[接口] 重建对话客户端失败：%s" % exc)
            loop._persona_client = None
            rebuild = getattr(getattr(loop, "compressor", None), "rebuild", None)
            if callable(rebuild):
                rebuild()
            factory = getattr(self.server, "embed_factory", None)
            if callable(factory):
                try:
                    embedder = factory()
                except Exception as exc:
                    self._note("[接口] 重建嵌入器失败：%s" % exc)
                    embedder = None
                memory = getattr(loop, "memory", None)
                if memory is not None:
                    memory.embedder = embedder
        self.server.models_cache = {}       # 模型清单按渠道缓存，换渠道要重问

    def _save_prompt(self):
        payload = self._body()
        key = str(payload.get("key") or "")
        try:
            prompt_files.write_override(self.server.data_dir, key,
                                        str(payload.get("text") or ""))
        except prompt_files.PromptError as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(200, {"ok": True})

    def _restart(self):
        control.request_stop()
        return self._send(200, {"ok": True})

    # ── 模型列表 ──────────────────────────────────────────────────────────
    def _models(self, query: dict) -> dict:
        """问端点要一份模型清单；缓存两分钟，点两次「列表」不重复走网络。"""
        kind = str((query.get("kind") or ["chat"])[0] or "chat")
        if kind not in ("chat", "summary", "embed"):
            kind = "chat"
        cache = getattr(self.server, "models_cache", None)
        if cache is None:
            cache = {}
            self.server.models_cache = cache
        hit = cache.get(kind)
        if hit and time.time() - float(hit.get("at") or 0) < 120:
            return hit["payload"]
        payload = self._fetch_models(kind)
        cache[kind] = {"at": time.time(), "payload": payload}
        return payload

    def _fetch_models(self, kind: str) -> dict:
        credentials = getattr(self.server, "credentials", None)
        if credentials is None:
            return {"ok": False, "kind": kind, "models": [], "url": "",
                    "error": "没有凭证，问不了模型列表"}
        config = self.server.config
        if kind == "chat":
            url, key = credentials.chat_url, credentials.chat_key
        elif kind == "embed":
            # 嵌入固定走嵌入端点（百炼那组）；它跟对话渠道不是一家
            url, key = credentials.summary_url, credentials.summary_key
            if not url:
                url, key = credentials.chat_url, credentials.chat_key
        else:
            model = str(getattr(config, "summary_model", "") or "")
            provider = str(getattr(config, "summary_provider", "auto") or "auto")
            if provider == "auto":
                provider = "bailian" if "qwen" in model.lower() else "deepseek"
            if provider == "bailian" and credentials.summary_url:
                url = credentials.summary_url
                key = credentials.summary_key or credentials.chat_key
            else:
                url, key = credentials.chat_url, credentials.chat_key
        base = str(url or "").rstrip("/")
        for suffix in ("/chat/completions", "/embeddings"):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
        if not base:
            return {"ok": False, "kind": kind, "models": [], "url": "",
                    "error": "凭证里没有端点地址"}
        target = base + "/models"
        request = urllib.request.Request(target)
        request.add_header("Authorization", "Bearer " + str(key or ""))
        try:
            with urllib.request.urlopen(request, timeout=12) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return {"ok": False, "kind": kind, "models": [], "url": target,
                    "error": "端点回 %d：%s" % (exc.code, _short_error(exc))}
        except Exception as exc:
            return {"ok": False, "kind": kind, "models": [], "url": target,
                    "error": "%s：%s" % (type(exc).__name__, str(exc)[:160])}
        items = payload.get("data") if isinstance(payload, dict) else None
        ids = []
        for item in items or []:
            if isinstance(item, dict) and item.get("id"):
                ids.append(str(item["id"]))
            elif isinstance(item, str):
                ids.append(item)
        filtered = False
        if kind == "embed":
            embed_ids = [model for model in ids if "embed" in model.lower()]
            if embed_ids:                # 百炼那份清单里还有几十个对话/画图模型
                ids, filtered = embed_ids, True
        return {"ok": True, "kind": kind, "models": ids, "url": target,
                "filtered": filtered, "error": ""}

    def _embed_status(self) -> dict:
        loop = getattr(self.server, "loop", None)
        embedder = getattr(getattr(loop, "memory", None), "embedder", None)
        config = self.server.config
        return {"embed_model": str(getattr(config, "embed_model", "") or ""),
                "embed_dim": int(getattr(config, "embed_dim", 0) or 0),
                "embed_ok": bool(embedder is not None
                                 and getattr(embedder, "available", False)),
                "embed_host": str(getattr(self.server, "embed_host", "") or "")}

    # ── 设置视图 ──────────────────────────────────────────────────────────
    def _settings_payload(self) -> dict:
        config = self.server.config
        data_dir = self.server.data_dir
        store = _store_of(self.server)
        prompts = []
        current = prompt_files.overrides()
        for key, (label, desc, filename) in prompt_files.META.items():
            try:
                default = prompt_files.default_text(key)
            except Exception:
                default = ""
            override = str(current.get(key) or "")
            prompts.append({"key": key, "label": label, "desc": desc,
                            "file": filename, "default": default,
                            "override": override, "text": override or default})
        values = settings.effective(config)
        creds = getattr(self.server, "credentials", None)
        hints = settings.hints(data_dir)
        if creds is not None:
            # 没在设置里写过的，就把凭证文件里生效的那份显示出来（不然框是空的）
            values["chat_url"] = values.get("chat_url") or creds.chat_url
            values["embed_url"] = (values.get("embed_url")
                                   or settings.embed_base_of(creds.summary_url))
            if not hints.get("chat_key"):
                hints["chat_key"] = settings.hint_of(creds.chat_key)
            if not hints.get("embed_key"):
                hints["embed_key"] = settings.hint_of(creds.summary_key)
        hints = {k: v for k, v in hints.items() if v}
        return {"values": values,
                "hints": hints,
                "stored": settings.load(data_dir),
                "defaults": settings.defaults(),
                "fields": settings.FIELDS,
                "groups": list(settings.GROUPS),
                "prompts": prompts,
                "history": settings.history(data_dir),
                "usage": {"today": store.usage_today(),
                          "totals": store.usage_totals()},
                "memory": self._embed_status(),
                "data_dir": data_dir}

    # ── 三个视图 ──────────────────────────────────────────────────────────
    def _state(self) -> dict:
        store = _store_of(self.server)
        config = self.server.config
        state = store.load_state()
        context = Context(config)
        context.set_state(state["bands"], state["previous"], state["current"])
        fold_keep = int(getattr(config, "tool_fold_keep", 0) or 0)
        user_name = str(getattr(config, "user_name", "") or "")
        current = serialize_entries(state["current"].entries, fold_keep=fold_keep,
                                    user_name=user_name)
        previous = serialize_entries(state["previous"].entries, fold_keep=fold_keep,
                                     user_name=user_name)
        totals = store.usage_totals()
        live = store.usage_live()
        embed = self._embed_status()
        loop = getattr(self.server, "loop", None)
        draft = {
            "text": str(getattr(loop, "draft_text", "") or "")[-1200:],
            "think": str(getattr(loop, "draft_think", "") or "")[-600:],
        }
        return {"name": getattr(config, "name", ""),
                "draft": draft,
                "model": getattr(config, "model", ""),
                "models": {"chat": getattr(config, "model", ""),
                           "summary": getattr(config, "summary_model", ""),
                           "embed": embed["embed_model"],
                           "embed_ok": embed["embed_ok"]},
                "status": store.status(),
                # 步数与用量给两份：live 只算还挂在记录上的步（删了就退），
                # totals 是 usage 表全部，网页只在设置里当总账看。
                "steps": live["steps"],
                "live": live,
                "compressions": self._compressions(),
                "current_tokens": estimate_tokens(current),
                "context_tokens": (estimate_tokens(context.system_text())
                                   + estimate_tokens(previous)
                                   + estimate_tokens(current)),
                "block_tokens": int(getattr(config, "block_tokens", 0)),
                "context_budget": int(getattr(config, "context_budget", 0)),
                "today": store.usage_today(),
                "totals": totals}

    def _compressions(self) -> int:
        return sum(1 for block in _store_of(self.server).blocks()
                   if block["state"] == "compressed")

    def _context(self) -> dict:
        store = _store_of(self.server)
        config = self.server.config
        fold_keep = int(getattr(config, "tool_fold_keep", 0) or 0)
        user_name = str(getattr(config, "user_name", "") or "")
        state = store.load_state()
        context = Context(config)
        context.set_state(state["bands"], state["previous"], state["current"])
        bands = [{"level": band.level, "start_ts": band.start_ts,
                  "end_ts": band.end_ts, "text": band.text,
                  "chars": len(str(band.text or "")),
                  "tokens": estimate_tokens(str(band.text or ""))}
                 for band in state["bands"]]
        previous = _block_view(state["previous"], fold_keep, user_name)
        current = _block_view(state["current"], fold_keep, user_name)
        written = persona.read(config)
        script = persona.path_of(config)
        sink = context.sink()
        system_tokens = estimate_tokens(context.system_text())
        return {"sink": {"text": sink, "chars": len(sink),
                         "tokens": estimate_tokens(sink)},
                "bands": bands,
                "previous": previous,
                "current": current,
                "persona": {"text": written, "chars": len(written),
                            "limit": persona.limit_of(config),
                            "min_chars": persona.min_chars_of(config),
                            "feed_chars": persona.feed_chars_of(config),
                            "pending": len(persona.new_material(context, config)),
                            "hours": persona.hours_of(config),
                            "at": _mtime(script)},
                "block_tokens": previous["tokens"] + current["tokens"],
                "context_tokens": system_tokens + previous["tokens"] + current["tokens"],
                "limits": {"block_tokens": int(getattr(config, "block_tokens", 0)),
                           "context_budget": int(getattr(config, "context_budget", 0)),
                           "band_budget": int(getattr(config, "band_budget", 0)),
                           "band_count": len(getattr(config, "band_chars", ()) or ()),
                           "tool_fold_keep": fold_keep}}

    def _persona(self) -> None:
        """清空她自己写的描述（人这边留一个后门；模型那边不给她改的入口）。"""
        payload = self._body()
        config = self.server.config
        if payload.get("reset"):
            persona.write(config, "")
            persona.touch_stamp(config)
            return self._send(200, {"ok": True, "text": ""})
        return self._send(400, {"error": "只支持 reset"})

    def _graph(self, query: dict) -> str:
        """事件图：直接借 memory_v2 的查看器把 `<data>/memory` 渲染成一页。

        画不出来（没有记忆目录、图坏了）也回一页 HTML 说清原因——iframe 里至少看得见字。
        """
        from memory_v2 import view as graph_view

        folder = getattr(self.server, "memory_dir", "") or self.server.data_dir
        label = str((query.get("label") or [""])[0] or
                    getattr(self.server.config, "name", "") or "")
        try:
            # 记忆目录还没建（它一次 remember 都没调过）：建个空的，画一张空图，
            # 总比在 iframe 里显示一段报错好。
            os.makedirs(folder, exist_ok=True)
            return graph_view.render_page(folder, label)
        except Exception as exc:                     # 没目录、图坏了都走这里
            return ("<!doctype html><meta charset=\"utf-8\">"
                    "<body style=\"background:#0a0d13;color:#cfd3d6;font:14px sans-serif;"
                    "padding:24px\"><b>画不出来</b><pre>%s\n\n%s</pre></body>"
                    % (folder, exc))

    def _save_edits(self) -> None:
        """页面里攒的改名/删除/剪边：写进 `<数据>/edits/`，由循环在下一步落到图上。

        网页是只读的（图只有循环一个写者），所以这里只排队；`_apply_edits` 取走并
        挪进 `edits/applied/`。
        """
        payload = self._body()
        ops = payload.get("ops")
        if not isinstance(ops, list) or not ops:
            return self._send(400, {"error": "清单里没有 ops"})
        folder = os.path.join(os.path.abspath(self.server.data_dir), "edits")
        os.makedirs(folder, exist_ok=True)
        name = "edits-%s.json" % time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(folder, name)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump({"data": self.server.memory_dir, "ops": ops},
                      handle, ensure_ascii=False, indent=2)
        return self._send(200, {"ok": True, "path": path, "count": len(ops),
                                "note": "存下了，循环下一步就落到图里"})

    def _feed(self, query: dict) -> dict:
        store = _store_of(self.server)
        since = str((query.get("since") or ["0:0"])[0])
        try:
            block, seq = [int(part) for part in since.split(":")]
        except ValueError:
            block, seq = 0, 0
        try:
            since_step = int((query.get("step") or ["0"])[0])
        except ValueError:
            since_step = 0
        entries = store.feed(block, seq)
        steps = store.usage_since(since_step)
        cursor = {"block": block, "seq": seq}
        if entries:
            cursor = {"block": entries[-1]["block"], "seq": entries[-1]["seq"]}
        return {"entries": entries, "steps": steps, "cursor": cursor,
                "more": len(entries) >= 300}


class _Server(ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        exc = sys.exc_info()[1]
        if isinstance(exc, (ConnectionError, BrokenPipeError)):
            return                    # 浏览器刷新、关页：不值得记一行
        super().handle_error(request, client_address)


def _short_error(exc) -> str:
    """把端点的报错正文截一小段，别把整页 HTML 塞进界面。"""
    try:
        return exc.read()[:200].decode("utf-8", "replace").replace("\n", " ")
    except Exception:
        return str(exc)


def serve(config, path: str, token: str = "",
          host: str = "127.0.0.1", port: int = 8080,
          data_dir: str = "", memory_dir: str = "", loop=None, credentials=None,
          embed_factory=None) -> ThreadingHTTPServer:
    """起一个后台线程；返回的 httpd 交给调用方（自检里用来取端口）。"""
    httpd = _Server((host, int(port)), _Handler)
    httpd.config = config
    httpd.db_path = path
    httpd.token = token
    httpd.data_dir = data_dir or os.path.dirname(os.path.abspath(path))
    httpd.memory_dir = memory_dir or os.path.join(httpd.data_dir, "memory")
    httpd.loop = loop
    httpd.credentials = credentials
    httpd.embed_factory = embed_factory
    httpd.models_cache = {}
    summary_url = str(getattr(credentials, "summary_url", "") or "")
    httpd.embed_host = urlsplit(summary_url).netloc if summary_url else ""
    threading.Thread(target=httpd.serve_forever, daemon=True, name="web").start()
    return httpd


def make_token(path: str) -> str:
    """第一次起服务时生成一个令牌，写进数据目录（600），下次直接用。"""
    import secrets
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as handle:
                token = handle.read().strip()
            if token:
                return token
        except OSError:
            pass
    token = secrets.token_urlsafe(24)
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(token)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return token
