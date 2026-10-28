"""QQ 通道适配器：OneBot 11 事件 → rin 的收件箱。

它只做三件事，别的都不管：

1. **收**：NapCat（或任何 OneBot 11 实现）把事件 POST 到 `/onebot`，这里挑出消息、
   过白名单、去掉 @ 和 CQ 码，整理成一段标准 JSON 推给网关的 `POST /inbox`；
2. **回**：轮询网关的 `/feed`，把新出现的 assistant 正文发回**最近说话的那个会话**；
3. **认人**：私聊记 QQ 号与昵称，群聊记群号、群名、发言人——核心那边靠这些字段
   区分"谁在哪个群说的"。

设计上刻意不碰 NapCat 的内部：它换协议、换传输（HTTP ↔ 反向 WS），改的都是这一层。
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, List, Optional

_CQ = re.compile(r"\[CQ:[^\]]+\]")
_SPACES = re.compile(r"[ \t]+")


class Config:
    def __init__(self, gateway: str, token: str, napcat: str, napcat_token: str,
                 listen: str, allow_users: List[str], allow_groups: List[str],
                 group_mode: str = "at", feed_seconds: float = 1.0):
        self.gateway = gateway.rstrip("/")
        self.token = token
        self.napcat = napcat.rstrip("/")
        self.napcat_token = napcat_token
        self.host, _, port = listen.partition(":")
        self.port = int(port or "8098")
        self.allow_users = [str(item) for item in allow_users if str(item).strip()]
        self.allow_groups = [str(item) for item in allow_groups if str(item).strip()]
        self.group_mode = group_mode if group_mode in ("at", "all") else "at"
        self.feed_seconds = max(0.3, float(feed_seconds))


# ── 与两端说话 ──────────────────────────────────────────────────────────────
def _post_json(url: str, payload: dict, token: str = "", timeout: int = 15) -> dict:
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": "Bearer " + token} if token else {})})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def _get_json(url: str, token: str = "", timeout: int = 15) -> dict:
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token} if token else {})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read() or b"{}")


def _plain_text(message) -> str:
    """OneBot 的 message 可能是字符串也可能是段数组；取文字、去 CQ 码。"""
    if isinstance(message, str):
        return _SPACES.sub(" ", _CQ.sub("", message)).strip()
    parts = []
    for segment in message or []:
        if not isinstance(segment, dict):
            continue
        kind = str(segment.get("type") or "")
        data = segment.get("data") or {}
        if kind == "text":
            parts.append(str(data.get("text") or ""))
        elif kind == "at":
            parts.append("@" + str(data.get("qq") or ""))
        elif kind in ("image", "face", "mface"):
            parts.append("[%s]" % kind)
    return _SPACES.sub(" ", _CQ.sub("", "".join(parts))).strip()


def _mentioned(message, self_id: str) -> bool:
    if isinstance(message, str):
        return ("[CQ:at,qq=%s]" % self_id) in message
    for segment in message or []:
        if isinstance(segment, dict) and segment.get("type") == "at":
            if str((segment.get("data") or {}).get("qq")) == str(self_id):
                return True
    return False


class Route:
    """回话目标：最近说话的那个会话。"""

    def __init__(self):
        self.kind = ""          # private / group
        self.chat_id = ""

    def remember(self, chat_type: str, chat_id: str) -> None:
        self.kind = "group" if chat_type == "group" else "private"
        self.chat_id = str(chat_id)

    @property
    def ready(self) -> bool:
        return bool(self.chat_id)


# ── 收：OneBot 事件 ─────────────────────────────────────────────────────────
class Bridge:
    def __init__(self, config: Config):
        self.config = config
        self.route = Route()
        self.seen: Dict[str, float] = {}
        self.self_id = ""
        self.cursor = {"block": 0, "seq": 0}
        self.lock = threading.Lock()
        self.stopping = False

    # 事件进来
    def on_event(self, event: dict) -> Optional[dict]:
        if str(event.get("post_type") or "") != "message":
            return None
        self_id = str(event.get("self_id") or "")
        if self_id:
            self.self_id = self_id
        if str((event.get("sender") or {}).get("user_id") or event.get("user_id") or "") == self_id:
            return None                                   # 自己说的
        message_id = str(event.get("message_id") or "")
        if message_id:
            now = time.time()
            with self.lock:
                if message_id in self.seen:
                    return None
                self.seen[message_id] = now
                if len(self.seen) > 500:
                    for key, stamp in list(self.seen.items()):
                        if now - stamp > 600:
                            self.seen.pop(key, None)

        chat_type = "group" if str(event.get("message_type") or "") == "group" else "private"
        sender = event.get("sender") or {}
        user_id = str(event.get("user_id") or sender.get("user_id") or "")
        name = str(sender.get("card") or sender.get("nickname") or user_id)
        text = _plain_text(event.get("message") or event.get("raw_message") or "")

        if chat_type == "group":
            group_id = str(event.get("group_id") or "")
            if not group_id or group_id not in self.config.allow_groups:
                return None
            if self.config.group_mode == "at" and not _mentioned(
                    event.get("message") or "", self.self_id):
                return None
            text = text.replace("@%s" % self.self_id, "").strip()
            chat_name = str(event.get("group_name") or "") or self._group_name(group_id)
        else:
            if not user_id or user_id not in self.config.allow_users:
                return None
            chat_id, chat_name = user_id, name
            group_id = ""
        if not text:
            return None

        chat_id = group_id if chat_type == "group" else user_id
        self.route.remember(chat_type, chat_id)
        return {
            "source": "QQ",
            "kind": "user",
            "chat_type": chat_type,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "sender_id": user_id,
            "sender_name": name,
            "text": text,
            "meta": {"message_id": message_id},
        }

    def _group_name(self, group_id: str) -> str:
        try:
            data = _post_json(self.config.napcat + "/get_group_info",
                              {"group_id": int(group_id)}, self.config.napcat_token, 5)
            info = data.get("data") or {}
            return str(info.get("group_name") or "")
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            return ""

    def push_inbox(self, item: dict) -> bool:
        try:
            _post_json(self.config.gateway + "/inbox", item, self.config.token)
            return True
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print("[qq] 推给网关失败：%s" % exc, flush=True)
            return False

    # 回：把她说的话发回最近的会话
    def pump_feed(self) -> None:
        since = "%d:%d" % (self.cursor["block"], self.cursor["seq"])
        url = (self.config.gateway + "/feed?since=" + urllib.parse.quote(since)
               + "&step=0")
        try:
            payload = _get_json(url, self.config.token)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print("[qq] 拉 feed 失败：%s" % exc, flush=True)
            return
        entries = payload.get("entries") or []
        cursor = payload.get("cursor") or {}
        for entry in entries:
            if str(entry.get("role") or "") != "assistant":
                continue
            text = str(entry.get("text") or "").strip()
            if text:
                self.send_back(text)
        if cursor:
            self.cursor = {"block": int(cursor.get("block") or 0),
                           "seq": int(cursor.get("seq") or 0)}

    def send_back(self, text: str) -> bool:
        if not self.route.ready:
            return False
        action = "send_group_msg" if self.route.kind == "group" else "send_private_msg"
        key = "group_id" if self.route.kind == "group" else "user_id"
        ok = True
        for piece in _split(text):
            try:
                _post_json(self.config.napcat + "/" + action,
                           {key: int(self.route.chat_id), "message": piece},
                           self.config.napcat_token, 20)
            except (urllib.error.URLError, OSError, ValueError) as exc:
                print("[qq] 发回 QQ 失败：%s" % exc, flush=True)
                ok = False
        return ok


def _split(text: str, limit: int = 800) -> List[str]:
    """QQ 单条有字节上限；先按空行再按长度切，别把一句话拦腰截断。"""
    text = str(text or "").strip()
    if len(text) <= limit:
        return [text]
    pieces, rest = [], text
    while len(rest) > limit:
        cut = rest.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        pieces.append(rest[:cut].strip())
        rest = rest[cut:].strip()
    if rest:
        pieces.append(rest)
    return pieces


# ── HTTP：接 OneBot 的事件 ──────────────────────────────────────────────────
class EventHandler(BaseHTTPRequestHandler):
    server_version = "rin-qq/0.1"

    def log_message(self, *_args):        # 安静点：事件很多
        return

    def do_POST(self):                    # noqa: N802
        if self.path.split("?")[0] not in ("/onebot", "/"):
            return self._send(404, {"error": "没有这个地址"})
        token = ""
        header = self.headers.get("Authorization") or ""
        if header.startswith("Bearer "):
            token = header[7:].strip()
        if not token:
            query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
            token = str((query.get("access_token") or [""])[0])
        expected = self.server.bridge.config.napcat_token
        if expected and token != expected:
            return self._send(401, {"error": "令牌不对"})
        length = int(self.headers.get("Content-Length") or 0)
        try:
            event = json.loads(self.rfile.read(min(length, 1 << 20)) or b"{}")
        except ValueError:
            return self._send(400, {"error": "不是 JSON"})
        item = self.server.bridge.on_event(event if isinstance(event, dict) else {})
        if item:
            self.server.bridge.push_inbox(item)
        return self._send(200, {"ok": True})

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def serve(config: Config) -> None:
    bridge = Bridge(config)
    httpd = ThreadingHTTPServer((config.host, config.port), EventHandler)
    httpd.bridge = bridge
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    print("[qq] 听 OneBot 事件：http://%s:%d/onebot" % (config.host, config.port),
          flush=True)
    print("[qq] 白名单：私聊 %s · 群 %s（%s）" % (
        ",".join(config.allow_users) or "无",
        ",".join(config.allow_groups) or "无",
        "只在被 @ 时" if config.group_mode == "at" else "全收"), flush=True)
    try:
        while not bridge.stopping:
            bridge.pump_feed()
            time.sleep(config.feed_seconds)
    except KeyboardInterrupt:
        print("\n[qq] 退出", flush=True)
    finally:
        httpd.shutdown()
