"""QQ 通道适配器：OneBot 11 事件 → rin 的收件箱。

只管两件事：

1. **收**：NapCat（或任何 OneBot 11 实现）把事件 POST 到 `/onebot`，这里挑出消息、
   过白名单、去掉 @ 和 CQ 码，整理成一段标准 JSON 推给网关的 `POST /inbox`；
2. **认人**：私聊记 QQ 号与昵称，群聊记群号、群名、发言人——核心那边靠这些字段
   区分"谁在哪个群说的"。

发话不归它管：她要说给 QQ 上的人听，就自己调 `qq` 工具发。2026-10-30 之前这里还会
把她每句 assistant 正文自动发回"叫醒她的那个会话"，和她的工具发送两条路并着走——
同一句话发两遍，发几条、什么时候发都不在她手里（见 palimpsest 仓
`docs/决策记录.md` 的 2026-10-30 那一条）。

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
# 监听在这些地址上时，本机其他进程之外的机器连不进来。
_LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
# 好友备注缓存多久（秒）：人不常改备注，但改了下一分钟就该看见
_FRIEND_TTL = 600.0
# 群名缓存多久（秒）：绝大多数事件不带群名，每条消息都去问一次会堵住 handler
_GROUP_TTL = 3600.0
# 启动/重连时按白名单各拉多少条历史来对账（多拉几条没关系，网关按 ext_id 去重）
_CATCH_UP_COUNT = 10


def _with_scheme(url: str, fallback: str) -> str:
    """`127.0.0.1:8080` 这种没写 scheme 的补成 `http://`。

    以前缺 scheme 会 urlopen 报 "unknown url type"，而 `--gateway` 的帮助
    文案本身写的就是不带 scheme 的样子。
    """
    text = str(url or "").strip()
    if not text:
        return fallback
    return text if "://" in text else "http://" + text


class Config:
    def __init__(self, gateway: str, token: str, napcat: str, napcat_token: str,
                 listen: str, allow_users: List[str], allow_groups: List[str],
                 group_mode: str = "at", poll_seconds: float = 60.0):
        self.gateway = _with_scheme(gateway, "http://127.0.0.1:8080").rstrip("/")
        self.token = token
        self.napcat = _with_scheme(napcat, "http://127.0.0.1:3000").rstrip("/")
        self.napcat_token = napcat_token
        self.host, _, port = listen.partition(":")
        self.port = int(port or "8098")
        self.allow_users = [str(item) for item in allow_users if str(item).strip()]
        self.allow_groups = [str(item) for item in allow_groups if str(item).strip()]
        self.group_mode = group_mode if group_mode in ("at", "all") else "at"
        self.poll_seconds = max(1.0, float(poll_seconds))


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


def _image_urls(message) -> List[str]:
    """消息里那几张图的地址。

    图**不在这里下载**：适配器只管把地址递进去，存哪儿、要不要顺手看一眼，
    都是网关那侧的事（它知道工作区和视觉模型在哪儿）。
    """
    if isinstance(message, str):
        found = []
        for match in re.finditer(r"\[CQ:image,[^\]]*url=([^,\]]+)", message):
            found.append(urllib.parse.unquote(match.group(1)))
        return found
    urls = []
    for segment in message or []:
        if not isinstance(segment, dict) or segment.get("type") != "image":
            continue
        url = str((segment.get("data") or {}).get("url") or "").strip()
        if url:
            urls.append(url)
    return urls


def _mentioned(message, self_id: str) -> bool:
    if isinstance(message, str):
        return ("[CQ:at,qq=%s]" % self_id) in message
    for segment in message or []:
        if isinstance(segment, dict) and segment.get("type") == "at":
            if str((segment.get("data") or {}).get("qq")) == str(self_id):
                return True
    return False


# ── 收：OneBot 事件 ─────────────────────────────────────────────────────────
class Bridge:
    def __init__(self, config: Config):
        self.config = config
        # 收过的 message_id：**推给网关成功之后**才记（失败要能重放），
        # 只在这个进程活着的时候有效；跨进程的去重靠网关的 ext_id 台账。
        self.seen: Dict[str, float] = {}
        self.self_id = ""
        self.warned_auth = False
        # 好友备注缓存：事件里只有昵称，备注要从 NapCat 的好友列表拿
        self.friends: Dict[str, str] = {}
        self.friends_at = 0.0
        self.notes: Dict[str, float] = {}
        self.warns: Dict[str, float] = {}
        self.checks: Dict[str, float] = {}
        self.groups: Dict[str, tuple] = {}
        self.online = True
        self.down = 0              # 连续几次问不到 NapCat
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
            return self._drop(event, "这是我自己发的")
        message_id = str(event.get("message_id") or "")
        if message_id and self.known(message_id):
            return self._drop(event, "同一个 message_id 已经收过")

        chat_type = "group" if str(event.get("message_type") or "") == "group" else "private"
        sender = event.get("sender") or {}
        user_id = str(event.get("user_id") or sender.get("user_id") or "")
        nickname = str(sender.get("nickname") or user_id)
        # 群里：群名片（card）优先；私聊：好友备注（remark）优先——见 _friend_remark
        name = str(sender.get("card") or nickname)
        text = _plain_text(event.get("message") or event.get("raw_message") or "")
        images = _image_urls(event.get("message") or event.get("raw_message") or "")
        if images:
            # 图单独走 images 字段：正文里那个占位标记就不留了，
            # 「一张 1024×768 的图：收件/…」由网关那侧按真尺寸写。
            text = re.sub(r"\[image\]\s*", "", text).strip()

        policy = ""
        if chat_type == "group":
            group_id = str(event.get("group_id") or "")
            if not group_id or group_id not in self.config.allow_groups:
                return self._drop(event, "群 %s 不在白名单" % (group_id or "?"))
            mentioned = _mentioned(event.get("message") or "", self.self_id)
            if self.config.group_mode == "at" and not mentioned:
                return self._drop(event, "群里没 @ 我")
            # 被 @ 的这一条明确叫她；没 @ 的（group_mode=all 时）攒着，等她自个儿看
            policy = "wake" if mentioned else ""
            text = text.replace("@%s" % self.self_id, "").strip()
            chat_name = str(event.get("group_name") or "") or self._group_name(group_id)
        else:
            if not user_id or user_id not in self.config.allow_users:
                return self._drop(event, "私聊 %s 不在白名单" % (user_id or "?"))
            # 备注是她的人给他起的名字，比昵称更像"她认识的那个人"
            name = self._friend_remark(user_id) or nickname
            chat_name = name
            group_id = ""
        if not text and not images:
            return self._drop(event, "没有文字（表情/语音/文件之类）")

        chat_id = group_id if chat_type == "group" else user_id
        return {
            "source": "QQ",
            "kind": "user",
            "chat_type": chat_type,
            "chat_id": chat_id,
            "chat_name": chat_name,
            "sender_id": user_id,
            "sender_name": name,
            # 这条是不是「对方」说的：白名单里那些人就是他本人 / 她认得的人。
            # 墙钟里那句「距上次对方说话」只认它；群里别人的话不带这个标。
            "from_user": user_id in self.config.allow_users,
            "text": text,
            # 图只递地址：存哪儿、看不看，网关那侧决定（它知道工作区和视觉模型）
            "images": [{"url": url} for url in images[:4]],
            # 群里能走到这里就说明被 @ 了：这一条明确叫她（wake）；没 @ 的群消息
            # 走的是 batch，攒着等她自己看，不打断她。
            "policy": policy,
            "ext_id": message_id,
            "meta": {"message_id": message_id},
        }

    def known(self, message_id: str) -> bool:
        with self.lock:
            return str(message_id) in self.seen

    def remember(self, message_id: str) -> None:
        """收下了才记：推网关失败的那种要能重放（不然那条消息就永久丢了）。"""
        key = str(message_id or "")
        if not key:
            return
        with self.lock:
            self.seen[key] = time.time()
            if len(self.seen) > 500:
                # 按时间砍到 400 条：以前只在"超过 500 条"时删"600 秒前的"，
                # 消息密的时候一条也删不掉，表会一直长。
                ordered = sorted(self.seen.items(), key=lambda pair: pair[1])
                for old, _ in ordered[:len(self.seen) - 400]:
                    self.seen.pop(old, None)

    def note(self, kind: str) -> bool:
        """同一种动静五分钟说一次日志（输入状态一秒好几条，不能逐条打）。"""
        return self._allow(self.notes, kind, 300.0)

    def warn(self, key: str, text: str, seconds: float = 60.0) -> None:
        """同一种毛病隔一阵说一次：一秒一次的轮询失败不能刷屏。"""
        if self._allow(self.warns, key, seconds):
            print("[qq] " + text, flush=True)

    def _allow(self, box: Dict[str, float], key: str, seconds: float) -> bool:
        stamp = time.time()
        with self.lock:
            if stamp - box.get(key, 0.0) < seconds:
                return False
            box[key] = stamp
        return True

    def _drop(self, event: dict, why: str) -> None:
        """一条消息没被收下：把原因写出来。静默丢弃最难查。"""
        print("[qq] 没收 %s/%s「%s」：%s" % (
            event.get("post_type") or "?",
            event.get("message_type") or event.get("notice_type") or "?",
            str(event.get("raw_message") or "")[:40] or "-", why), flush=True)
        return None

    def _group_name(self, group_id: str) -> str:
        """群名：绝大多数事件不带，得问一次。按群缓存一小时——每条群消息都问
        一次会把 handler 线程堵在那儿（NapCat 一慢，事件就排队）。"""
        key = str(group_id or "")
        stamp = time.time()
        with self.lock:
            cached = self.groups.get(key)
        if cached and stamp - cached[1] < _GROUP_TTL:
            return str(cached[0])
        name = ""
        try:
            data = _post_json(self.config.napcat + "/get_group_info",
                              {"group_id": int(key)}, self.config.napcat_token, 5)
            info = data.get("data") or {}
            name = str(info.get("group_name") or "")
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            name = ""
        with self.lock:
            self.groups[key] = (name, stamp)
        return name

    def _friend_remark(self, user_id: str) -> str:
        """好友备注：事件里只带昵称，备注得问 NapCat 要（一分钟一条消息不值得每句都问）。"""
        stamp = time.time()
        if stamp - self.friends_at > _FRIEND_TTL:
            self.friends = self._load_friends()
            self.friends_at = stamp
        return str(self.friends.get(str(user_id)) or "")

    def _load_friends(self) -> Dict[str, str]:
        try:
            data = _post_json(self.config.napcat + "/get_friend_list", {},
                              self.config.napcat_token, 10)
        except (urllib.error.URLError, OSError, ValueError, TypeError):
            return {}
        out: Dict[str, str] = {}
        for friend in (data.get("data") or []):
            if not isinstance(friend, dict):
                continue
            remark = str(friend.get("remark") or "").strip()
            if remark:
                out[str(friend.get("user_id") or "")] = remark
        return out

    def push_inbox(self, item: dict) -> tuple:
        """推给网关；回来 `(进件 id, 推成了没有)`。

        `id == 0 且 ok` 表示网关说这条编号已经有了（补拉历史时的常态）；
        `ok` 为假才需要重放——调用方别再拿 0 当"失败"。
        """
        try:
            reply = _post_json(self.config.gateway + "/inbox", item, self.config.token)
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print("[qq] 推给网关失败：%s" % exc, flush=True)
            return 0, False
        if (reply or {}).get("duplicate"):
            return 0, True
        return int((reply or {}).get("id") or 0), True

    def catch_up(self) -> int:
        """对账：把最近的历史消息里我们没收过的补进来。

        掉线那段时间进来的消息，QQ 不会在重连后重新推事件（2026-10-28 有一条就这么
        丢了）。这里按白名单挨个问一遍历史，靠 `ext_id`（message_id）去重——
        收过的网关直接忽略，所以补多补少都不会重复叫她。
        """
        added = 0
        for user_id in self.config.allow_users:
            try:
                reply = _post_json(self.config.napcat + "/get_friend_msg_history",
                                   {"user_id": int(user_id),
                                    "count": _CATCH_UP_COUNT},
                                   self.config.napcat_token, 10)
            except (urllib.error.URLError, OSError, ValueError, TypeError):
                continue
            messages = ((reply or {}).get("data") or {}).get("messages") or []
            for message in messages:
                if not isinstance(message, dict):
                    continue
                if str(message.get("post_type") or "") != "message":
                    continue                       # 她自己发的、系统消息都不算
                if str((message.get("sender") or {}).get("user_id") or
                       message.get("user_id") or "") != str(user_id):
                    continue
                event = dict(message)
                event.setdefault("message_type", "private")
                item = self.on_event(event)
                if not item:
                    continue
                item_id, ok = self.push_inbox(item)
                if not ok:
                    continue                       # 下一轮再补，别记成"收过"
                self.remember(str(item.get("ext_id") or ""))
                if item_id:
                    added += 1
                    print("[qq] 补上漏掉的一条（%s）：%s" % (
                        item.get("sender_name") or user_id,
                        str(item.get("text") or "")[:40]), flush=True)
        if added:
            print("[qq] 对账补了 %d 条" % added, flush=True)
        return added

    def check_online(self) -> bool:
        """问 NapCat 一句账号在不在线；一分钟最多问一次。

        掉线时 NapCat 照样收不到任何事件、也不报错——不主动问就只会"静悄悄地没反应"。
        """
        if not self._allow(self.checks, "online", 60.0):
            return self.online
        try:
            reply = _post_json(self.config.napcat + "/get_status", {},
                               self.config.napcat_token, 5)
        except (urllib.error.URLError, OSError, ValueError, TypeError) as exc:
            # 等扫码登录的时候 NapCat 的 HTTP 服务根本没起来：问不到也算"不在线"。
            # 连续三次问不到才改判（一次超时不算掉线），但**返回值要跟着改**，
            # 否则 serve() 里"账号回来了→再对一遍账"永远触发不了。
            self.down += 1
            if self.down >= 3:
                self.online = False
                self.warn("napcat-down",
                          "问不到 NapCat（%s）：多半在等扫码登录，消息进不来" % exc,
                          seconds=600)
            return self.online
        self.down = 0
        data = (reply or {}).get("data") or {}
        self.online = bool(data.get("online", True))
        if not self.online:
            self.warn("offline",
                      "账号不在线：消息进不来。docker restart napcat 会重新快速登录，"
                      "它要是还要二维码，就得扫码了", seconds=600)
        return self.online


# ── HTTP：接 OneBot 的事件 ──────────────────────────────────────────────────
class EventHandler(BaseHTTPRequestHandler):
    server_version = "rin-qq/0.1"

    def log_message(self, *_args):        # 安静点：事件很多
        return

    def _caller_token(self) -> str:
        """NapCat 各版本带令牌的方式不一样：Bearer、裸值、access_token 都认。"""
        header = (self.headers.get("Authorization") or "").strip()
        if header.lower().startswith("bearer "):
            return header[7:].strip()
        if header:
            return header
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query)
        return str((query.get("access_token") or [""])[0]).strip()

    def do_POST(self):                    # noqa: N802
        if self.path.split("?")[0] not in ("/onebot", "/"):
            return self._send(404, {"error": "没有这个地址"})
        bridge = self.server.bridge
        expecting = bridge.config.napcat_token
        token = self._caller_token()
        if not expecting:
            # 没设令牌：只有监听在回环上才说得过去；对外监听等于谁都能灌收件箱。
            if bridge.config.host not in _LOOPBACK_HOSTS:
                print("[qq] 拒收上报：没设 NAPCAT_TOKEN，而监听不在回环地址"
                      "（%s）" % bridge.config.host, flush=True)
                return self._send(403, {"error": "对外监听必须设令牌"})
        elif token != expecting:
            # 监听在回环地址时，没带令牌也放行：这条通道是本机 NapCat 推的，
            # 外面进不来。带错了才拒——错令牌说明配置对不上，得让人看见。
            if token or bridge.config.host not in _LOOPBACK_HOSTS:
                print("[qq] 拒收上报：%s" % ("令牌不对（%r）" % token[:16] if token
                                              else "没带令牌，而监听不在回环地址"),
                      flush=True)
                return self._send(401, {"error": "令牌不对"})
            if not bridge.warned_auth:
                bridge.warned_auth = True
                print("[qq] NapCat 上报没带令牌，监听在回环地址，按本机信任放行",
                      flush=True)
        raw = self._read_body()
        try:
            event = json.loads(raw or b"{}")
        except ValueError:
            print("[qq] 上报的正文不是 JSON（%d 字节）：%r"
                  % (len(raw), raw[:120]), flush=True)
            return self._send(400, {"error": "不是 JSON"})
        if not isinstance(event, dict):
            return self._send(400, {"error": "要一个对象"})
        try:
            item = bridge.on_event(event)
            if item:
                item_id, ok = bridge.push_inbox(item)
                if not ok:
                    # 网关没接住（重启、忙）：回 5xx 让 NapCat 重发，
                    # 别在这里回 200——回了它就以为送到了。
                    return self._send(503, {"error": "网关没接住，稍后重试"})
                bridge.remember(str(item.get("ext_id") or ""))
                print("[qq] 收下 %s「%s」%s：%s（进件 %d）" % (
                    "群" if item.get("chat_type") == "group" else "私聊",
                    item.get("chat_name") or item.get("chat_id") or "?",
                    item.get("sender_name") or "?",
                    str(item.get("text") or "")[:40],
                    item_id or 0), flush=True)
            elif str(event.get("post_type") or "") != "message":
                # 不是消息（输入状态、戳一戳之类）：同一种五分钟说一次，够判断通道通不通
                kind = "%s/%s" % (
                    event.get("post_type") or "?",
                    event.get("notice_type") or event.get("request_type") or "-")
                if bridge.note(kind):
                    print("[qq] 收到 %s（不是消息，忽略）" % kind, flush=True)
        except Exception as exc:          # 一条事件处理炸了，不能让连接挂在那儿
            print("[qq] 处理上报出错（%s）：%s" % (type(exc).__name__, exc), flush=True)
            return self._send(500, {"error": "处理出错"})
        return self._send(200, {"ok": True})

    def _read_body(self) -> bytes:
        """按 Content-Length 读正文；没有这个头就按 chunked 读。

        以前只认 Content-Length：碰上分块发送的客户端会读成 0 字节，
        于是"收到空事件 → 不是消息 → 回 200"，看起来一切正常，消息却没了。
        """
        chunked = "chunked" in (self.headers.get("Transfer-Encoding") or "").lower()
        if chunked:
            pieces = []
            while True:
                line = self.rfile.readline(65536).strip()
                try:
                    size = int(line.split(b";")[0] or b"0", 16)
                except ValueError:
                    break
                if size <= 0:
                    self.rfile.readline(65536)          # 结尾的空行
                    break
                pieces.append(self.rfile.read(size))
                self.rfile.readline(65536)              # 每块末尾的 CRLF
            return b"".join(pieces)
        length = int(self.headers.get("Content-Length") or 0)
        return self.rfile.read(min(length, 1 << 20)) if length else b""

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionError, OSError):
            # NapCat 那头把连接关了（重启、超时）：没什么可回的，别在日志里炸一行
            return


def _gateway_ready(config: Config) -> bool:
    """网关能答话了吗。同一台机器上两个 unit，`After=` 只管起的前后、不管就绪；
    网关那边还在开库的时候，这边先把历史推过去只会拿到 Connection refused。"""
    try:
        _get_json(config.gateway + "/state", config.token, 5)
        return True
    except (ValueError, OSError, urllib.error.URLError):
        return False


def serve(config: Config) -> None:
    """起 HTTP 收事件；顺带每分钟看一眼账号在不在线。

    这条通道**只收不发**：她要说给 QQ 上的人听，自己在 `qq` 工具里发。
    """
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
    online = True
    caught_up = False
    try:
        while not bridge.stopping:
            # 启动对账：掉线那段时间进来的消息，QQ 不会重新推事件。等网关先能答话，
            # 否则这一遍白推、也不重来（2026-10-30 改完当场撞见过）。
            if not caught_up and _gateway_ready(config):
                caught_up = True
                bridge.catch_up()
            # 账号掉线时消息根本进不来，日志里看不出任何异常——只能主动问一句。
            if not bridge.check_online():
                online = False
            elif not online:
                online = True
                print("[qq] 账号回来了，通道可用", flush=True)
                bridge.catch_up()      # 又断过一次：再对一遍
            time.sleep(config.poll_seconds)
    except KeyboardInterrupt:
        print("\n[qq] 退出", flush=True)
    finally:
        httpd.shutdown()
