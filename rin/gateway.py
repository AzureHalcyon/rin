"""网页面：说、听、看。

    POST /say       {"text": "..."}       → 进 inbox，立刻返回
    GET  /feed?since=3:12&step=40         → 增量原文 + 增量用量
    GET  /context                         → 当前上下文（sink / 旧段 / 两块原文）
    GET  /graph                           → 事件图（一页 HTML，数据内嵌）
    POST /edits                           → 把页面攒的编辑清单存到 <data>/edits/
    GET  /state                           → 它现在在干什么、今天用了多少
    POST /alerts/clear                    → 把顶上那条提示点掉（删掉那个文件）
    GET  /                                → 单页界面

★ 这一层**只读库、只往 inbox 写**，从不碰 blocks/entries/bands 的写路径：
  单写者仍然是那个循环进程。所以网页崩了顶多是看不见，不会把记录弄坏。
★ 每条连接各自开一个 LoopStore（SQLite 连接不能跨线程共享）；库开了 WAL，
  读不挡写。
★ 鉴权是 Bearer token：没有令牌一律 401，界面本身也走同一道门。
"""

from __future__ import annotations

import base64
import io
import hmac
import json
import os
import subprocess
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


def _owner_ids(data_dir: str) -> list:
    """设置里「我的账号」：逗号（中文逗号、顿号、分号、空格都认）分开的号或昵称。"""
    raw = str(settings.load(data_dir).get("owner_ids") or "")
    for sep in ("，", "、", "；", ";", " ", "\t"):
        raw = raw.replace(sep, ",")
    return [item.strip() for item in raw.split(",") if item.strip()]


def _is_owner(owner: list, payload: dict) -> bool:
    """这条算不算「我的账号」说的：号或昵称对得上就算。"""
    sender = str(payload.get("sender_id") or "").strip()
    name = str(payload.get("sender_name") or "").strip()
    return any(item == sender or item == name for item in owner)


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


# ── 图：落地 + 要不要顺手看一眼 ───────────────────────────────────────────────
# 图片从通道进来时是字节或一个地址，落到**图片库**（`<数据>/图片/<年-月>/`），进件里
# 只写一行事实。**图片本身不进上下文**：她的记录是文本流，`_turns()` 每步都重新渲染成
# 字符串，压缩也只吃文字；把图塞进去要同时改存储、渲染、压缩、预算四处，而旧块会
# 一直背着它。她自己就看得见——核心拼请求时会把路径变成 image_url 段。
#
# ★ 图片库在**工作区外面**（和工作区平级）：`run` 的沙箱只能写工作区，`write` 也只能
#   写工作区，所以她和她那堆脚本都动不了这里。记录里只存路径，文件要是被删了图片就
#   永远找不回来——所以这里不能是"她随手放东西的地方"，备份也覆盖它（`loop.backup`）。
_IMAGE_DIR = "图片"
_MAX_IMAGES = 4                      # 一条进件最多收几张：再多是刷屏，不是"发来一张图"
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_IMAGE_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
             "(KHTML, like Gecko) Chrome/131.0 Safari/537.36")


def _ext_of(raw: bytes) -> str:
    if raw[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if raw[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return ".webp"
    return ".jpg"


def _save_image(data_dir: str, raw: bytes, index: int) -> str:
    """存进图片库，返回记录里那种相对路径（`图片/2026-10/20261030-221305-2.jpg`）。"""
    month = time.strftime("%Y-%m")
    folder = os.path.join(os.path.abspath(data_dir), _IMAGE_DIR, month)
    name = "%s-%s-%d%s" % (time.strftime("%Y%m%d"), time.strftime("%H%M%S"),
                           index, _ext_of(raw))
    os.makedirs(folder, exist_ok=True)
    with open(os.path.join(folder, name), "wb") as handle:
        handle.write(raw)
    return "/".join((_IMAGE_DIR, month, name))


def _image_bytes(item) -> bytes:
    """进件里的一个图：`{"b64": ...}` 是她这头传上来的，`{"url": ...}` 是外面给的。"""
    if not isinstance(item, dict):
        return b""
    encoded = str(item.get("b64") or "").strip()
    if encoded:
        if "," in encoded[:64]:          # `data:image/png;base64,xxxx` 这种整串
            encoded = encoded.split(",", 1)[1]
        try:
            return base64.b64decode(encoded, validate=False)
        except Exception:
            return b""
    url = str(item.get("url") or "").strip()
    if not url.startswith(("http://", "https://")):
        return b""
    request = urllib.request.Request(url, headers={"User-Agent": _IMAGE_UA})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return response.read(_MAX_IMAGE_BYTES + 1)
    except Exception:
        return b""


def _size_of(raw: bytes) -> tuple:
    """(宽, 高)；读不出来就是 (0, 0)。"""
    try:
        from PIL import Image
        with Image.open(io.BytesIO(raw)) as image:
            return int(image.size[0]), int(image.size[1])
    except Exception:
        return 0, 0


def _image_block(data_dir: str, images) -> tuple:
    """把进件里的图片收下来。回来 `(要写进正文的那几行, 挂在 meta 上的图片清单)`。

    图片**不在这里看**：她那个模型自己就看得见，核心会把清单里那些路径拼成
    `image_url` 段。这一层只管两件事——落地、给正文留一行事实（多大的图片、在哪儿）。
    """
    if not isinstance(images, list) or not images:
        return "", []
    lines, landed = [], []
    for index, item in enumerate(images[:_MAX_IMAGES], start=1):
        raw = _image_bytes(item)
        if not raw:
            lines.append("（有一张图没取到）")
            continue
        if len(raw) > _MAX_IMAGE_BYTES:
            lines.append("（有一张图太大，不收了）")
            continue
        try:
            rel = _save_image(data_dir, raw, index)
        except OSError as exc:
            lines.append("（有一张图存不下：%s）" % exc)
            continue
        width, height = _size_of(raw)
        if width and height:
            lines.append("（一张 %d×%d 的图：%s）" % (width, height, rel))
        else:
            lines.append("（一张图片：%s）" % rel)
        landed.append({"path": rel, "w": width, "h": height})
    return "\n".join(lines), landed


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

    def finish(self):
        """一条连接用完就把这条线程的库连接关掉。

        ThreadingHTTPServer 每条连接一个线程，而每个线程都开一条 SQLite 连接；
        连接对象要等垃圾回收才关文件描述符。通道适配器一秒拉一次 /feed，
        一小时就能把 `ulimit -n`（1024）耗光，然后进程崩在
        "Too many open files: 'data/edits'" —— 2026-10-28 就这么崩过。
        """
        try:
            super().finish()
        finally:
            store = getattr(_local, "store", None)
            _local.store = None
            _local.path = ""
            if store is not None:
                try:
                    store.close()
                except Exception:      # 关不掉也别让连接收尾报错
                    pass

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
                return self._send(200, self._context(parse_qs(route.query)))
            if route.path == "/qq/status":
                return self._send(200, self._qq_status())
            if route.path == "/qq/qr":
                return self._qq_qr()
            if route.path == "/settings":
                return self._send(200, self._settings_payload())
            if route.path == "/channels":
                return self._send(200, {"channels": _store_of(self.server).channels()})
            if route.path == "/inbox":
                return self._send(200, self._inbox_view(parse_qs(route.query)))
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
            if route.path == "/archive":
                return self._archive()
            if route.path == "/alerts/clear":
                return self._clear_alert()
            if route.path == "/qq/restart":
                return self._qq_restart()
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
            if route.path == "/channels":
                return self._set_channel()
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
        # 网页这头就是人跟她说话：图收下来进工作区，清单挂进 meta，
        # 核心拼请求时会把它们变成 image_url 段——她自己就看得见。
        block, landed = _image_block(self.server.data_dir, payload.get("images"))
        if block:
            text = ("%s\n%s" % (text, block)).strip() if text else block
        if not text:
            return self._send(400, {"error": "没有正文"})
        # 网页说的一句：正文直接进记录（她就坐在这头跟我说话），
        # 但来源照实写——网页「消息」页上要看得见它是从哪儿来的。
        item_id = _store_of(self.server).push(
            "user", text, source="网页", direct=True,
            meta={"images": landed} if landed else None)
        return self._send(200, {"ok": True, "id": item_id})

    def _set_channel(self):
        """改一个会话的投递策略：wake / batch / mute。

        `chat` 与 `chat_id` 都认：核心里的名字是 chat_id，网页那边写着写着
        就成了 chat —— 两边对不上就是 400，用户点一下策略发现改不动。
        """
        payload = self._body()
        chat = str(payload.get("chat_id") or payload.get("chat") or "").strip()
        policy = str(payload.get("policy") or "").strip().lower()
        if not chat or policy not in ("wake", "batch", "mute"):
            return self._send(400, {"error": "要 chat_id 和 policy（wake / batch / mute）"})
        store = _store_of(self.server)
        try:
            changed = store.set_policy(chat, policy)
        except KeyError:
            return self._send(404, {"error": "没有这个会话：%s" % chat})
        except ValueError as exc:
            return self._send(400, {"error": str(exc)})
        return self._send(200, {"ok": True, "changed": changed,
                                "channels": store.channels()})

    def _inbox(self):
        """通道适配器往这里推一条进件（QQ、电报、脚本都一样）。

        字段：text 必填；kind 默认 user；其余 source / chat_type / chat_id /
        chat_name / sender_id / sender_name / policy / meta 可选。
        policy 不传就继承这个会话在 chats 里的策略。
        images 可选：`[{"b64": …}]` 或 `[{"url": …}]`，收下来的图进工作区，
        进件正文里只留一行事实（私聊里顺带把"图里是什么"也写进去）。
        """
        payload = self._body()
        text = str(payload.get("text") or "").strip()
        images = payload.get("images")
        landed = []
        if isinstance(images, list) and images:
            block, landed = _image_block(self.server.data_dir, images)
            if block:
                text = ("%s\n%s" % (text, block)).strip() if text else block
        if not text:
            return self._send(400, {"error": "没有正文"})
        meta = payload.get("meta")
        if not isinstance(meta, dict):
            meta = {}
        if landed:
            meta = dict(meta, images=landed)
        store = _store_of(self.server)
        source = str(payload.get("source") or "")
        raw_ext = str(payload.get("ext_id") or "")
        ext_id = ("%s:%s" % (source or "外部", raw_ext)) if raw_ext else ""
        if ext_id and store.inbox_has_ext(ext_id):
            # 同一条外部消息推过一遍了（补拉历史、重连补漏）：不再插，也不再叫醒她
            return self._send(200, {"ok": True, "duplicate": True, "id": 0})
        # 「这条是对方说的」：设置里填了「我的账号」就以它为准，没填就用通道报的兜底
        # （QQ 桥按白名单标）。墙钟拿它算「距上次对方说话」，群里别人、岛上的回帖不算。
        from_user = bool(payload.get("from_user"))
        owner = _owner_ids(self.server.data_dir)
        if owner:
            from_user = _is_owner(owner, payload)
        item_id = store.push(
            str(payload.get("kind") or "user"), text,
            policy=str(payload.get("policy") or ""),
            source=source,
            chat_type=str(payload.get("chat_type") or ""),
            chat_id=str(payload.get("chat_id") or ""),
            chat_name=str(payload.get("chat_name") or ""),
            sender_id=str(payload.get("sender_id") or ""),
            sender_name=str(payload.get("sender_name") or ""),
            meta=meta,
            direct=bool(payload.get("direct")),
            from_user=from_user,
            ext_id=ext_id)
        return self._send(200, {"ok": True, "id": item_id})

    def _inbox_view(self, query: dict) -> dict:
        """网页看的那一份收件箱：最近的进件 + 每个会话的策略。

        跟她自己看到的不一样：她只看得见没读的正文；这里把读过的、按 mute
        藏起来的也列出来——网页是仪表盘，人得能核对"谁说了什么、她看没看"。
        """
        store = _store_of(self.server)
        try:
            limit = int((query.get("limit") or ["60"])[0])
        except ValueError:
            limit = 60
        items = store.inbox_recent(min(300, max(1, limit)))
        return {"items": items, "channels": store.channels(),
                "counts": store.inbox_counts()}

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
                   "embed_model", "embed_dim")}
        settings.apply(config, stored)
        loop = getattr(self.server, "loop", None)

        client_config = getattr(getattr(loop, "client", None), "config", None)
        if client_config is not None:
            client_config.model = config.model
            client_config.temperature = config.temperature
            client_config.max_tokens = config.max_tokens
            client_config.reasoning_effort = str(
                getattr(config, "reasoning_effort", "") or "")

        if before["summary_model"] != config.summary_model:
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
        # 工具那边的凭据（B 站的 cookie / ffmpeg / 转写模型、QQ 的令牌）存完就生效，
        # 不用等重启：它们每次调用现读。
        try:
            from rin.tools import bilibili as bili_tool
            bili_tool.configure(settings.channel(stored))
        except Exception as exc:
            self._note("[工具] 应用 B 站设置失败：%s" % exc)
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
        text = str(payload.get("text") or "")
        if key == "persona-written":
            # 她自己写的那段：这里改等于人替她改，同时把"上次改写时间"往后推，
            # 免得刚改完又被后台整理覆盖掉。
            try:
                persona.write(self.server.config, text)
                persona.touch_stamp(self.server.config)
            except Exception as exc:
                return self._send(400, {"error": "写不进去：%s" % exc})
            return self._send(200, {"ok": True})
        try:
            prompt_files.write_override(self.server.data_dir, key, text)
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
            url, key = credentials.maas_url, credentials.maas_key
            if not url:
                url, key = credentials.chat_url, credentials.chat_key
        else:
            # 摘要跟对话是一家：换端点就是换对话地址，不另选
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
    # 真实注入顺序：system 先（开场声音 → 她自己写的那段 → 机制与边界），再工具描述
    # （按工具面的顺序：记忆 → wait → 收件箱 → 上网 → 命令 → 工作区 → B 站 → 看图 → QQ → 岛），
    # 最后是"用到时才拼进去"的那几份。设置页按这个顺序分四块：常驻 / 工具 / 归档 / 其他。
    _PROMPTS_IN_ORDER = (
        ("voice", "常驻"),
        ("persona-written", "常驻"),
        ("sink", "常驻"),
        ("remember", "工具"),
        ("link", "工具"),
        ("recall", "工具"),
        ("recollect", "工具"),
        ("forget", "工具"),
        ("rename", "工具"),
        ("wait", "工具"),
        ("channels", "工具"),
        ("channels_set", "工具"),
        ("messages", "工具"),
        ("look", "工具"),
        ("fetch", "工具"),
        ("search", "工具"),
        ("run", "工具"),
        ("write", "工具"),
        ("myself", "工具"),
        ("bilibili_search", "工具"),
        ("bilibili_video", "工具"),
        ("bilibili_subtitles", "工具"),
        ("bilibili_playurl", "工具"),
        ("bilibili_transcript", "工具"),
        ("qq", "工具"),
        ("xdao", "工具"),
        ("compress-system", "归档"),
        ("compress", "归档"),
        ("watchdog", "归档"),
        ("persona", "归档"),
        ("ask", "其他"),
    )

    def _prompt_rows(self, config) -> list:
        """设置页的提示词清单，按真实注入顺序排；她自己写的那段也在里面。"""
        current = prompt_files.overrides()
        listed = {key for key, _ in self._PROMPTS_IN_ORDER}
        rows = []
        for key, section in self._PROMPTS_IN_ORDER:
            if key == "persona-written":
                rows.append({
                    "key": key, "section": section,
                    "label": "自我描述 · 她自己写的",
                    "desc": "她在后台整理出来的那一段：接在开场（声音）之后、机制之前。"
                            "「自我描述」关掉时不注入。",
                    "file": "persona.md", "default": "", "override": "",
                    "text": persona.read(config), "resettable": False,
                    "state": "她写的",
                    "note": "" if bool(getattr(config, "persona_enabled", False))
                            else "现在是关着的（节奏 → 自我描述）"})
                continue
            if key not in prompt_files.META:
                continue
            label, desc, filename = prompt_files.META[key]
            try:
                default = prompt_files.default_text(key)
            except Exception:
                default = ""
            override = str(current.get(key) or "")
            rows.append({"key": key, "section": section, "label": label,
                         "desc": desc, "file": filename, "default": default,
                         "override": override, "text": override or default,
                         "resettable": True, "note": "",
                         "state": "已覆盖" if override else "默认"})
        for key, (label, desc, filename) in prompt_files.META.items():
            if key in listed:
                continue
            try:
                default = prompt_files.default_text(key)
            except Exception:
                default = ""
            override = str(current.get(key) or "")
            rows.append({"key": key, "section": "其他", "label": label,
                         "desc": desc, "file": filename, "default": default,
                         "override": override, "text": override or default,
                         "resettable": True, "note": "",
                         "state": "已覆盖" if override else "默认"})
        return rows

    def _settings_payload(self) -> dict:
        config = self.server.config
        data_dir = self.server.data_dir
        store = _store_of(self.server)
        prompts = self._prompt_rows(config)
        values = settings.effective(config)
        creds = getattr(self.server, "credentials", None)
        hints = settings.hints(data_dir)
        if creds is not None:
            # 没在设置里写过的，就把凭证文件里生效的那份显示出来（不然框是空的）
            values["chat_url"] = values.get("chat_url") or creds.chat_url
            values["embed_url"] = (values.get("embed_url")
                                   or settings.embed_base_of(creds.maas_url))
            if not hints.get("chat_key"):
                hints["chat_key"] = settings.hint_of(creds.chat_key)
            if not hints.get("embed_key"):
                hints["embed_key"] = settings.hint_of(creds.maas_key)
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
                # 当前块：优先用循环量出来的真数（服务端 prompt_tokens 减固定部分）。
                # 只给估算的话，中文会被低估四成——页面上写着 39k/50k，实际早就 68k，
                # 于是"为什么块没满也在压"根本对不上（2026-10-30 用户就问到这个）。
                "current_tokens": int(getattr(loop, "current_real", 0) or 0)
                                  or estimate_tokens(current),
                "current_tokens_estimated": estimate_tokens(current),
                # 整次请求：有服务端报的真数就用真数（估算是给"还没有真数"的时候兜底的）
                "context_tokens": int(getattr(loop, "last_prompt_tokens", 0) or 0)
                                  or (estimate_tokens(context.system_text())
                                      + estimate_tokens(previous)
                                      + estimate_tokens(current)),
                "block_tokens": int(getattr(config, "block_tokens", 0)),
                "context_budget": int(getattr(config, "context_budget", 0)),
                "today": store.usage_today(),
                "totals": totals,
                "alert": self._alert()}

    def _archive(self) -> None:
        """点「提前清」：只置一个开关，真正的活留给循环。

        为什么不在这个线程里跑：记忆图是整份 JSON 覆写，只有一个写者（那个循环）。
        循环会在"没有新输入"的那一步看到开关，跑完把一行结论写进日志与状态。
        """
        control.request_archive()
        return self._send(200, {"queued": True})

    def _alert(self):
        """落地侧（NapCat 守护）写进数据目录的一条提示：网页顶上挂一条，点掉就删文件。
        为什么走文件而不是库：写它的是 root 下的守护进程（要调 docker），库是单写者
        （那个循环进程）。一条一次性的运维提示没必要挤进记录那条路。
        """
        path = os.path.join(str(getattr(self.server.config, "data_dir", "") or ""),
                            "alerts.json")
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            return None
        if not isinstance(data, dict):
            return None
        text = str(data.get("text") or "").strip()
        if not text:
            return None
        return {"text": text, "ts": str(data.get("ts") or ""),
                "kind": str(data.get("kind") or "")}

    def _clear_alert(self) -> None:
        path = os.path.join(str(getattr(self.server.config, "data_dir", "") or ""),
                            "alerts.json")
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        except OSError as exc:
            return self._send(500, {"error": "删不掉那条提示：%s" % exc})
        return self._send(200, {"ok": True})

    def _compressions(self) -> int:
        return sum(1 for block in _store_of(self.server).blocks()
                   if block["state"] == "compressed")

    def _context(self, query: dict = None) -> dict:
        """抽屉那一份。

        `previous`（上一块的原文）默认**不带条目**，只给条数与 token：那一块动辄
        四五百条、两三百 KB，抽屉默认看不到它，带上来纯属每次刷新都白搬一遍。
        要看原文就 `GET /context?prev=1`（页面上「上一块」点开时才问）。
        """
        want_prev = bool((query or {}).get("prev"))
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
        if not want_prev:
            previous["entries"] = []
            previous["loaded"] = False
        else:
            previous["loaded"] = True
        current = _block_view(state["current"], fold_keep, user_name)
        # 当前块的 token：优先用循环量出来的真数（中文估算会低估四成左右，
        # 页面上写 39k、实际 68k，就会让人以为"块还没满为什么在压"）
        real = int(getattr(getattr(self.server, "loop", None), "current_real", 0) or 0)
        if real:
            current["tokens_estimated"] = current["tokens"]
            current["tokens"] = real
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
                "context_tokens": int(getattr(getattr(self.server, "loop", None),
                                              "last_prompt_tokens", 0) or 0)
                                  or (system_tokens + previous["tokens"]
                                      + current["tokens"]),
                "limits": {"block_tokens": int(getattr(config, "block_tokens", 0)),
                           "context_budget": int(getattr(config, "context_budget", 0)),
                           "band_budget": int(getattr(config, "band_budget", 0)),
                           "band_count": len(getattr(config, "band_chars", ()) or ()),
                           "tool_fold_keep": fold_keep}}

    # ── QQ 通道：在线情况 + 扫码用的二维码 ────────────────────────────────
    # 网页上这两样是为了「手机上就能把号救回来」：NapCat 被踢之后要扫码，
    # 以前得登服务器翻容器日志里的二维码（或者进 WebUI）。
    def _qq_setting(self, key: str, fallback: str = "") -> str:
        config = self.server.config
        try:
            stored = settings.load(str(getattr(config, "data_dir", "") or ""))
        except Exception:
            stored = {}
        return str(stored.get(key) or "").strip() or fallback

    def _qq_base(self) -> str:
        base = self._qq_setting("qq_base", os.environ.get("NAPCAT_BASE", "")
                                or "http://127.0.0.1:3000")
        return base.rstrip("/") if "://" in base else "http://" + base.rstrip("/")

    def _qq_token(self) -> str:
        return self._qq_setting("qq_token", os.environ.get("NAPCAT_TOKEN", ""))

    def _qq_qr_path(self) -> str:
        return self._qq_setting("qq_qr_file", "/opt/napcat/data/cache/qrcode.png")

    def _qq_status(self) -> dict:
        base = self._qq_base()
        token = self._qq_token()
        online = good = False
        error = ""
        try:
            request = urllib.request.Request(base + "/get_status",
                                             data=b"{}", method="POST")
            request.add_header("Content-Type", "application/json")
            if token:
                request.add_header("Authorization", "Bearer " + token)
            with urllib.request.urlopen(request, timeout=4) as response:
                payload = json.loads(response.read() or b"{}")
            data = (payload or {}).get("data") or {}
            online = bool(data.get("online"))
            good = bool(data.get("good", online))
            if str((payload or {}).get("status") or "") == "failed":
                error = str((payload or {}).get("message") or "NapCat 说没成")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            error = str(exc) or type(exc).__name__
        qr = self._qq_qr_path()
        info = {"path": qr, "exists": os.path.exists(qr)}
        if info["exists"]:
            try:
                stamp = os.path.getmtime(qr)
                info["mtime"] = time.strftime("%H:%M:%S", time.localtime(stamp))
                info["age"] = int(time.time() - stamp)
                info["bytes"] = os.path.getsize(qr)
            except OSError:
                pass
        return {"ok": not error, "online": online, "good": good,
                "base": base, "error": error, "qr": info}

    def _qq_restart(self) -> None:
        """把 NapCat 重启一次，换一张新的二维码。

        用 `docker restart` 而不是 `start`：容器**已经在跑**的时候 `start` 什么
        都不做，页面就会一直显示那张过期的码（2026-10-30 15:21 就是这么"点了重起
        还是已过期"的）。NapCat 只在启动那一下生成二维码，所以必须真的重起。

        sudoers 里只放行了这两条（见 `deploy/sudoers-rin-napcat`）。
        """
        try:
            done = subprocess.run(["sudo", "-n", "docker", "restart", "napcat"],
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=180)
        except (OSError, subprocess.SubprocessError) as exc:
            return self._send(500, {"error": "重起不来：%s" % exc})
        out = done.stdout.decode("utf-8", "replace").strip()
        ok = done.returncode == 0
        return self._send(200 if ok else 500,
                          {"ok": ok,
                           "out": out[:300] or ("docker restart 退出码 %d"
                                                % done.returncode)})

    def _qq_qr(self) -> None:
        path = self._qq_qr_path()
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except OSError:
            return self._send(404, {"error": "还没有二维码：NapCat 没在等登录，"
                                             "或者那个路径不对（%s）" % path})
        return self._send(200, body, "image/png")

    def _persona(self) -> None:
        """清空她自己写的描述（人这边留一个后门）。"""
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
        """增量原文。

        三种问法：
        - `since=b:s`（默认）：从这个位子往后（旧网页断线重连也走这条）；
        - `tail=N`：最后 N 条，首屏用。整块从头重放要七百多 KB、九百多条，
          手机上得好几秒，而且先看见的是最早的话，最新的排在最后才出来；
        - `before=b:s&limit=N`：比这个位子更早的一页，往上翻用。
        """
        store = _store_of(self.server)
        try:
            since_step = int((query.get("step") or ["0"])[0])
        except ValueError:
            since_step = 0
        steps = store.usage_since(since_step)

        def pair_of(text: str):
            try:
                head, tail = str(text or "0:0").split(":")[:2]
                return int(head), int(tail)
            except (ValueError, AttributeError):
                return 0, 0

        if query.get("tail"):
            try:
                limit = max(1, min(300, int((query.get("tail") or ["60"])[0])))
            except ValueError:
                limit = 60
            entries = store.feed_tail(limit)
            cursor = {"block": entries[-1]["block"], "seq": entries[-1]["seq"]} \
                if entries else {"block": 0, "seq": 0}
            if entries:
                older = store.feed_before(entries[0]["block"], entries[0]["seq"], 1)
                more = bool(older)
            else:
                more = False
            return {"entries": entries, "steps": [], "cursor": cursor, "more": more}

        if query.get("before"):
            block, seq = pair_of(str((query.get("before") or ["0:0"])[0]))
            try:
                limit = max(1, min(300, int((query.get("limit") or ["60"])[0])))
            except ValueError:
                limit = 60
            entries = store.feed_before(block, seq, limit)
            more = bool(entries) and bool(
                store.feed_before(entries[0]["block"], entries[0]["seq"], 1))
            cursor = {"block": entries[-1]["block"], "seq": entries[-1]["seq"]} \
                if entries else {"block": block, "seq": seq}
            return {"entries": entries, "steps": steps, "cursor": cursor,
                    "more": more}

        block, seq = pair_of(str((query.get("since") or ["0:0"])[0]))
        entries = store.feed(block, seq)
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
    embed_url = str(getattr(credentials, "maas_url", "") or "")
    httpd.embed_host = urlsplit(embed_url).netloc if embed_url else ""
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
