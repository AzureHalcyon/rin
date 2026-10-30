"""X岛（`www.nmbxd1.com`）的入口：看板块、看串、看回复，以及发串、回串。

读的三个动作都是公开接口，**不要登录、不要饼干**：

    GET /Api/getForumList              板块（分组 → 板块 id）
    GET /Api/showf?id=<板块>&page=<n>  一个板块的串列表
    GET /Api/thread?id=<串>&page=<n>   一个串（串主 + 一页回复，一页 20 条）

字段是照着真响应写的（2026-10-29 实测）：

- 串：`id` / `fid` / `title` / `content` / `now` / `user_hash` / `name` /
  `ReplyCount` / `img` + `ext`
- 回复：`id` / `title` / `content` / `now` / `user_hash` / `name` / `img` + `ext`
- `content` 是**带 HTML 的**（`<br>`、`<span>`、`<a>`），这里一律摊成纯文字
- 叫 `user_hash` 的那个字段其实是**显示的名字**（串内匿名的标识），不是杂凑值
- 带图的帖子 `img` 是路径、`ext` 是后缀，拼成 `https://image.nmb.best/image/…`；
  回复里带图的，落地后当作附件推给她（见 `rin/xdao/feed.py`）

写（发串、回串）走网页那套表单：`multipart/form-data` + Cookie `userhash`，见
`_write`。带图就多一个 `image` 文件部分——表单里就这一格（2026-10-30 实测）。
"""

from __future__ import annotations

import gzip
import html as html_lib
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import List

from loop import prompt_files
from rin.tools import media

NAMES = ("xdao",)

BASE_DEFAULT = "https://www.nmbxd1.com"
IMAGE_BASE = "https://image.nmb.best/image/"
# 这个站的接口要一个像客户端的 UA；用浏览器的也行，但客户端那个更稳
UA = os.environ.get("XDAO_UA") or "NMBClient-yollpoll-android"
TIMEOUT = 25
PAGE = 20               # 一页 20 条（接口自己的分页）
MAX_OUT = 3000          # 一次往记录里放这么多字：够她判断，不够就翻页
SUMMARY = 80            # 串列表里每条正文留多少字

# 搜索：岛自己**没有**搜索接口，这是社区做的第三方全文检索（XDS）。
# 它只认两类 UA（2026-10-29 实测）：作者公告里那串公开测试版 UA，或者浏览器 UA。
# 自己起个 `Rin/0.1` 会被 403 挡掉（`reason: ua`）——所以默认用他给的那串，
# 想换成自己被登记的 UA 就在设置页填 `xdao_search_ua`。
# 限流：两次之间至少 5 秒、每天 150 次，响应头 `X-RateLimit-Remaining` 给余量。
SEARCH_BASE = "https://xds.wol.moe"
SEARCH_UA = "XdnmbSearch/1.0 (PublicTest 261029)"
SEARCH_GAP = 5.0        # 两次搜索之间至少隔这么久（对方 min_interval_ms=5000）
SEARCH_SIZE = 10        # 一次要几条：多了她看不完，也吃对方的额度
SEARCH_OUT = 2500
_last_search = [0.0]    # 上一次搜索的时刻（进程内）

_TAGS = re.compile(r"<[^>]+>")
_BREAK = re.compile(r"(?i)<br\s*/?>|</p>|</div>")
_SPACES = re.compile(r"[ \t\u3000]+")
_TAIL = re.compile(r" *\n")
_BLANKS = re.compile(r"\n{3,}")


class XdaoError(RuntimeError):
    """接口不通、返回不是 JSON，或者板块/串不存在。"""


def base_of(config=None) -> str:
    value = ""
    if config is not None:
        try:
            from rin import settings
            data_dir = str(getattr(config, "data_dir", "") or "")
            if data_dir:
                value = str(settings.load(data_dir).get("xdao_base") or "").strip()
        except Exception:
            value = ""
    value = value or str(os.environ.get("XDAO_BASE") or "").strip()
    return (value or BASE_DEFAULT).rstrip("/")


def plain(value) -> str:
    """HTML → 纯文字：`<br>` 换成换行，实体解码，多余空白收掉。"""
    text = _BREAK.sub("\n", str(value or ""))
    text = _TAGS.sub("", text)
    text = html_lib.unescape(text)
    text = _SPACES.sub(" ", text)
    text = _TAIL.sub("\n", text)
    text = _BLANKS.sub("\n\n", text)
    return text.strip()


def image_of(item: dict) -> str:
    """这条帖子挂的图（没有就是空串）。"""
    path = str((item or {}).get("img") or "").strip()
    if not path:
        return ""
    ext = str((item or {}).get("ext") or ".jpg").strip() or ".jpg"
    if not ext.startswith("."):
        ext = "." + ext
    return IMAGE_BASE + path + ext


def _get(path: str, config=None):
    """取一个 JSON。这个站不管 Accept-Encoding 写什么都回 gzip——先收字节再解。

    有饼干就带上：**受限版面**（比如 `27 AI`）不看饼干直接回
    `{"error": …, "success": false}`，带了才给串（2026-10-29 实测）。
    """
    url = base_of(config) + path
    headers = {"User-Agent": UA, "Accept-Encoding": "identity"}
    cookie = cookie_of(config)
    if cookie:
        headers["Cookie"] = "userhash=" + cookie
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read(6_000_000)
    except urllib.error.HTTPError as exc:
        raise XdaoError("岛上回了 %d（%s）" % (exc.code, path)) from exc
    except Exception as exc:
        raise XdaoError("连不上岛：%s" % type(exc).__name__) from exc
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        data = json.loads(raw.decode("utf-8", "replace") or "null")
    except ValueError as exc:
        raise XdaoError("岛上回的不是 JSON（%s）" % path) from exc
    if isinstance(data, dict) and data.get("error") and not data.get("id"):
        # 受限版面/没权限：把它自己那句话带出去，别只说"不是数组"
        raise XdaoError("岛上说：%s" % (plain(data.get("error"))[:120] or "没权限"))
    return data


def api(path: str, config=None):
    """留给自检替换的入口：`call` 只经这里取数据。"""
    return _get(path, config)


# ── 三个动作 ────────────────────────────────────────────────────────────────
def _forums(config=None) -> str:
    groups = api("/Api/getForumList", config)
    if not isinstance(groups, list):
        raise XdaoError("板块列表不是数组")
    lines = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        forums = []
        for forum in group.get("forums") or []:
            if not isinstance(forum, dict):
                continue
            ident = str(forum.get("id") or "").strip()
            name = str(forum.get("name") or "").strip()
            if not ident or not name or ident == "-1":
                continue                  # -1 是"时间线"，不是能发串的板块
            forums.append("%s %s" % (ident, name))
        if forums:
            lines.append("%s：%s" % (str(group.get("name") or "?"),
                                     " · ".join(forums)))
    return "\n".join(lines) or "板块列表是空的。"


def _listing(fid: str, page: int, config=None) -> str:
    items = api("/Api/showf?id=%s&page=%d" % (fid, page), config)
    if not isinstance(items, list):
        raise XdaoError("串列表不是数组")
    if not items:
        return "这个板块第 %d 页没有串。" % page
    lines = []
    for item in items:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or "").strip()
        body = plain(item.get("content"))
        flat = " ".join(body.split())          # 列表里一行一条，换行摊平
        label = title if title and title != "无标题" else flat[:30]
        lines.append("No.%s %s（回 %s，%s）" % (
            item.get("id"), label or "（没有标题）",
            item.get("ReplyCount") or 0, str(item.get("now") or "")[:16]))
        if flat:
            lines.append("    " + flat[:SUMMARY])
    tail = "\n看某个串：xdao(action=\"read\", params={\"id\": …})"
    return _clip("\n".join(lines) + tail)


def _thread(ident: str, page: int, config=None) -> str:
    data = api("/Api/thread?id=%s&page=%d" % (ident, page), config)
    if not isinstance(data, dict):
        raise XdaoError("串不是对象（可能没有这个号）")
    head = ["No.%s「%s」 · %s · %s · %s 回" % (
        data.get("id"), (str(data.get("title") or "").strip() or "无标题"),
        str(data.get("user_hash") or "") or "无名氏",
        str(data.get("now") or ""), data.get("ReplyCount") or 0)]
    body = plain(data.get("content"))
    if body:
        head.append(body)
    picture = image_of(data)
    if picture:
        head.append("图：%s" % picture)
    replies = [item for item in (data.get("Replies") or [])
               if isinstance(item, dict)]
    for reply in replies:
        line = [">>No.%s · %s · %s" % (
            reply.get("id"), str(reply.get("user_hash") or "") or "无名氏",
            str(reply.get("now") or ""))]
        text = plain(reply.get("content"))
        if text:
            line.append(text)
        picture = image_of(reply)
        if picture:
            line.append("图：%s" % picture)
        head.append("\n".join(line))
    total = int(data.get("ReplyCount") or 0)
    if total > PAGE * page:
        head.append("（还有更多回复：page=%d）" % (page + 1))
    return _clip("\n\n".join(head))


# ── 搜索：社区那台全文检索 ──────────────────────────────────────────────────
# 岛自己没有搜索接口。社区有人搭了一台（XDS，基于 FastAPI），四个端点：
# 检索 / 板块列表 / 规模 / 按编号反查所属串。它按来源 IP 限流，响应头
# `X-RateLimit-Remaining` 给当日余量；作者要求 UA 形如 `ClientName/Version`。
_SCOPES = {"all": "全部", "threads": "只串", "replies": "只回复"}
_SORTS = {"rank": "相关度", "time_desc": "新→旧", "time_asc": "旧→新",
          "replies_desc": "回复多→少", "replies_asc": "回复少→多"}


def _setting_of(config, key: str, env: str) -> str:
    value = ""
    if config is not None:
        try:
            from rin import settings
            data_dir = str(getattr(config, "data_dir", "") or "")
            if data_dir:
                value = str(settings.load(data_dir).get(key) or "").strip()
        except Exception:
            value = ""
    return value or str(os.environ.get(env) or "").strip()


def search_base_of(config=None) -> str:
    return (_setting_of(config, "xdao_search_base", "XDAO_SEARCH_BASE")
            or SEARCH_BASE).rstrip("/")


def search_ua_of(config=None) -> str:
    return _setting_of(config, "xdao_search_ua", "XDAO_SEARCH_UA") or SEARCH_UA


def _search_json(path: str, params: dict, config=None) -> tuple:
    """(正文, 当日剩余额度)。429 时说清楚是对方限流，别当成"没搜到"。"""
    import time as _time
    base = search_base_of(config)
    now = _time.time()
    if now - _last_search[0] < SEARCH_GAP:
        raise XdaoError("刚搜过一次，隔 %d 秒再来（那边一秒内只让问一次）。"
                        % int(SEARCH_GAP))
    _last_search[0] = now
    query = urllib.parse.urlencode({key: value for key, value in params.items()
                                    if value not in ("", None)})
    url = base + path + ("?" + query if query else "")
    request = urllib.request.Request(
        url, headers={"User-Agent": search_ua_of(config),
                      "Accept-Encoding": "identity"})
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            raw = response.read(4_000_000)
            left = response.headers.get("X-RateLimit-Remaining")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read()[:200].decode("utf-8", "replace")
        except Exception:
            detail = ""
        if exc.code == 429:
            raise XdaoError("搜索那边今天的额度用完了：%s"
                            % (detail.replace("\n", " ")[:120] or "过一阵再试")) from exc
        raise XdaoError("搜索回了 %d：%s" % (exc.code, detail[:120])) from exc
    except Exception as exc:
        raise XdaoError("连不上搜索：%s" % type(exc).__name__) from exc
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    try:
        return json.loads(raw.decode("utf-8", "replace") or "{}"), left
    except ValueError as exc:
        raise XdaoError("搜索回的不是 JSON") from exc


def _search(params: dict, config=None) -> str:
    q = str(params.get("q") or params.get("query") or "").strip()
    if not q:
        return "要搜什么？给 q（几个词用空格分开，意思是「都要有」）。"
    scope = str(params.get("scope") or "all").strip()
    sort = str(params.get("sort") or "rank").strip()
    try:
        size = int(params.get("size") or SEARCH_SIZE)
    except (TypeError, ValueError):
        size = SEARCH_SIZE
    body, left = _search_json("/api/v1/search", {
        "q": q,
        "scope": scope if scope in _SCOPES else "all",
        "sort": sort if sort in _SORTS else "rank",
        "mode": "exact" if str(params.get("mode") or "").strip() == "exact" else "terms",
        "match": "aggregate" if str(params.get("match") or "").strip() == "aggregate"
                 else "thread",
        "op": str(params.get("op") or "and").strip(),
        "fid": str(params.get("fid") or "").strip(),
        "since": str(params.get("since") or "").strip(),
        "until": str(params.get("until") or "").strip(),
        "page": _page(params),
        "size": max(1, min(50, size)),
    }, config)
    results = body.get("results") or []
    total = body.get("total")
    if isinstance(total, dict):
        count = "串 %s · 回复 %s" % (total.get("threads", 0), total.get("replies", 0))
        if total.get("truncated"):
            count += "（到 %s 条的上限了）" % total.get("cap")
    else:
        count = str(total if total is not None else len(results))
    head = "搜「%s」· %s · %s · %s" % (
        q, count, _SCOPES.get(scope, "全部"), _SORTS.get(sort, "相关度"))
    if left:
        head += " · 今天还剩 %s 次" % left
    if not results:
        return head + "\n（一条也没有）"
    lines = [head]
    for item in results:
        reply = str(item.get("kind") or "") == "reply"
        title = str(item.get("title") or "").strip()
        if not title or title == "无标题":
            title = ""
        lines.append("No.%s%s [%s]%s %s · 回 %s · %s" % (
            item.get("id"), "（回复）" if reply else "",
            item.get("fid") or "?", ("「%s」" % title) if title else "",
            str(item.get("post_time") or "")[:10],
            item.get("reply_count") or 0,
            str(item.get("user_hash") or "")))
        flat = " ".join(plain(item.get("content")).split())
        if flat:
            lines.append("    " + flat[:SUMMARY + 40])
        if reply:
            lines.append("    要跳到它所在的串：xdao(action=\"where\", params={\"id\": %s})"
                         % item.get("id"))
    text = "\n".join(lines)
    if len(text) > SEARCH_OUT:
        text = text[:SEARCH_OUT].rstrip() + "\n…（还有更多，翻页或把话说窄一点）"
    return text


def _where(ident: str, config=None) -> str:
    """反查：这个编号是串还是回复，属于哪个串。"""
    if not ident.isdigit():
        return "要给编号（No. 后面那串数字）。"
    body, _left = _search_json("/api/v1/thread-of/%s" % ident, {}, config)
    kind = str(body.get("kind") or "")
    if kind == "thread":
        return "No.%s 本身就是一条串。" % ident
    thread = body.get("thread_id") or body.get("id")
    if not thread:
        return "搜不到 No.%s 属于哪个串。" % ident
    return "No.%s 是串 No.%s 里的回复。" % (ident, thread)


def _clip(text: str) -> str:
    text = str(text or "").strip()
    if len(text) <= MAX_OUT:
        return text
    return text[:MAX_OUT].rstrip() + "\n…（后面还有，翻页或者只看某几条）"


# ── 工具面 ──────────────────────────────────────────────────────────────────
def tools(config=None) -> List[dict]:
    text = prompt_files.tool_texts()
    return [
        {"type": "function", "function": {
            "name": "xdao",
            "description": text["xdao"],
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "enum": ["forums", "list", "read", "post", "reply",
                                        "search", "where"],
                               "description": text["xdao.action"]},
                    "params": {"type": "object",
                               "description": text["xdao.params"]},
                },
                "required": ["action"],
            },
        }},
    ]


def call(name: str, arguments: str, config=None) -> str:
    if name not in NAMES:
        return "没有这个工具：%s" % name
    try:
        payload = json.loads(arguments or "{}")
    except ValueError:
        return "参数不是 JSON。"
    if not isinstance(payload, dict):
        return "参数不是对象。"
    action = str(payload.get("action") or "").strip()
    params = payload.get("params")
    if not isinstance(params, dict):
        params = {}
    try:
        if action == "forums":
            return _forums(config)
        if action == "list":
            fid = str(params.get("fid") or params.get("forum") or "").strip()
            if not fid:
                return "要看哪个板块？先 forums 一下拿 id。"
            return _listing(fid, _page(params), config)
        if action == "read":
            ident = str(params.get("id") or params.get("no") or "").strip()
            if not ident:
                return "要看哪个串？给 id（No. 后面那串数字）。"
            return _thread(ident, _page(params), config)
        if action in ("post", "reply"):
            return _write(action, params, config)
        if action == "search":
            return _search(params, config)
        if action == "where":
            return _where(str(params.get("id") or params.get("no") or "").strip(),
                          config)
    except XdaoError as exc:
        return "岛那边没成：%s" % exc
    except Exception as exc:               # 别让一次解析炸掉整步
        return "看岛的时候出错了：%s: %s" % (type(exc).__name__, exc)
    return "没有这个动作：%s（看有 forums / list / read，写有 post / reply）" % action


# ── 写：发串、回串 ──────────────────────────────────────────────────────────
# 走的是网页表单那条路（`doPostThread.html` / `doReplyThread.html`，multipart）：
# 这条是社区客户端走了很多年的路（`TransparentLC/xdcmd` 的 `postThread` 就是它），
# 比 App 端那套 JSON 接口省事。身份全靠 Cookie `userhash`——这块值就是站上
# 「复制饼干」复制出来的那串（URL 编码的），不是串里显示的那个名字。
def cookie_of(config=None) -> str:
    value = ""
    if config is not None:
        try:
            from rin import settings
            data_dir = str(getattr(config, "data_dir", "") or "")
            if data_dir:
                value = str(settings.load(data_dir).get("xdao_cookie") or "").strip()
        except Exception:
            value = ""
    return value or str(os.environ.get("XDAO_COOKIE") or "").strip()


def _multipart(fields: dict, files=None) -> tuple:
    """拼一个 multipart 正文：岛上发串/回串用的就是这个形状。

    `files` 是 `[{"field", "name", "mime", "data"}]`——岛上的表单里插图那一格叫
    `image`（`<input type="file" name="image">`，实测 2026-10-30），一个帖子只给一格。
    """
    boundary = "----rin" + os.urandom(8).hex()
    chunks = []
    for name, value in fields.items():
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                       % (boundary, name, value)).encode("utf-8"))
    for item in files or []:
        chunks.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"; "
                       "filename=\"%s\"\r\nContent-Type: %s\r\n\r\n"
                       % (boundary, item["field"], item["name"],
                          item["mime"])).encode("utf-8"))
        chunks.append(item["data"])
        chunks.append(b"\r\n")
    chunks.append(("--%s--\r\n" % boundary).encode("utf-8"))
    return b"".join(chunks), "multipart/form-data; boundary=" + boundary


_VERDICT_SUCCESS = ("成功", "发表成功", "发布成功")


def _verdict(final_url: str, text: str) -> dict:
    """看服务端回的那页是成是败。岛回的是「跳转提示」那种页面。"""
    flat = " ".join(plain(text).split())
    ident = ""
    match = re.search(r"/t/(\d{6,})", final_url or "") or \
        re.search(r"(?:/t/|>>No\.)(\d{6,})", text or "")
    if match:
        ident = match.group(1)
    for word in _VERDICT_SUCCESS:
        if word in flat:
            return {"state": "ok", "id": ident, "message": ""}
    for word in ("不能同时为空", "间隔", "太快", "禁止", "无权限", "失败", "错误",
                 "请确认饼干", "没有权限"):
        if word in flat:
            return {"state": "error", "id": "", "message": flat[:200]}
    return {"state": "unknown", "id": ident, "message": flat[:200]}


def _find_id(action: str, fields: dict, content: str, config=None) -> str:
    """发完回读一次，把刚那条的号捞回来。

    岛对发串的回话是一张跳转页，什么号都不给（实测），所以只能回板上找：
    正文开头对得上的那条就是它。找不着就算了，别猜。
    """
    head = " ".join(content.split())[:20]
    try:
        if action == "post":
            items = api("/Api/showf?id=%s&page=1" % fields.get("fid"), config)
            for item in items or []:
                if isinstance(item, dict) and \
                        " ".join(plain(item.get("content")).split()).startswith(head):
                    return str(item.get("id") or "")
            return ""
        info = api("/Api/thread?id=%s&page=1" % fields.get("resto"), config)
        replies = [item for item in ((info or {}).get("Replies") or [])
                   if isinstance(item, dict)]
        total = int((info or {}).get("ReplyCount") or 0)
        if len(replies) and total > 19:
            info = api("/Api/thread?id=%s&page=%d"
                       % (fields.get("resto"), total // 19 + 1), config)
            replies = [item for item in ((info or {}).get("Replies") or [])
                       if isinstance(item, dict)]
        for reply in reversed(replies):
            if " ".join(plain(reply.get("content")).split()).startswith(head):
                return str(reply.get("id") or "")
    except XdaoError:
        return ""
    return ""


def _write(action: str, params: dict, config=None) -> str:
    """发一条出去。发完把地址给她——公开的东西收不回来，至少让她知道在哪儿。"""
    from rin import alerts, limit

    cookie = cookie_of(config)
    if not cookie:
        return "岛上写东西要饼干：设置页「岛」组里把 userhash 那段填上。"
    content = str(params.get("content") or params.get("text") or "").strip()
    if len(content) > 1200:
        return "这条太长了（%d 字），岛上一条装不下，先说短一点。" % len(content)
    picture = None
    want = params.get("images")
    if want is None:
        want = params.get("image")
    if want:
        if isinstance(want, list):
            if len(want) > 1:
                return "岛上一帖只带一张图（表单里就一格 image）：挑一张。"
            want = want[0] if want else ""
        if want:
            try:
                picture = media.prepare(config, want)
            except media.MediaError as exc:
                return str(exc)
    if not content and picture is None:
        return "要发的话得先有正文（或者给一张图）。"
    name = str(params.get("name") or "").strip() or "无名氏"
    title = str(params.get("title") or "").strip()[:60]
    if action == "post":
        fid = str(params.get("fid") or params.get("forum") or "").strip()
        if not fid:
            return "发新串要指定板块：给 fid（先 forums 一下）。"
        fields = {"fid": fid, "name": name, "title": title or "无标题",
                  "content": content}
        kind_name = "发串"
    else:
        resto = str(params.get("id") or params.get("resto") or "").strip()
        if not resto:
            return "回串要指定是回哪个串：给 id。"
        fields = {"resto": resto, "name": name, "title": title or "无标题",
                  "content": content}
        kind_name = "回串"

    allowed, why = limit.allow(config, bucket="xdao")
    if not allowed:
        # 公开平台上连着往外倒会被删串、没收饼干；拦下要留痕
        alerts.raise_alert(config, "xdao_post_blocked",
                           "岛上的%s被闸住了：%s" % (kind_name, why))
        return "这条没发：%s" % why

    files = None
    if picture is not None:
        files = [{"field": "image", "name": picture["name"],
                  "mime": picture["mime"], "data": picture["data"]}]
    body, content_type = _multipart(fields, files)
    url = base_of(config) + ("/Home/Forum/doPostThread.html" if action == "post"
                             else "/Home/Forum/doReplyThread.html")
    request = urllib.request.Request(url, data=body, method="POST")
    request.add_header("Content-Type", content_type)
    request.add_header("User-Agent", UA)
    request.add_header("Referer", base_of(config) + "/Forum")
    request.add_header("Cookie", "userhash=" + cookie)
    try:
        with urllib.request.urlopen(request, timeout=40) as response:
            raw = response.read(2_000_000)
            final = response.geturl()
    except urllib.error.HTTPError as exc:
        return "岛那边没收这条（HTTP %d）" % exc.code
    except Exception as exc:
        return "发不出去：%s: %s" % (type(exc).__name__, str(exc)[:100])
    if raw[:2] == b"\x1f\x8b":
        raw = gzip.decompress(raw)
    verdict = _verdict(final, raw.decode("utf-8", "replace"))
    if verdict["state"] == "error":
        return "岛那边说：%s" % (verdict["message"] or "没发成（没读懂它的回话）")
    ident = verdict["id"] or (_find_id(action, fields, content, config)
                              if content else "")
    carried = ("带图：%s" % media.note_of(picture)) if picture is not None else ""
    # 记进"她自己的串"：轮询（`rin.xdao`）只按这份账去看，不扫全站
    tracked = ident or fields.get("resto") or fields.get("fid")
    try:
        from rin.xdao import threads as tracked_threads
        tracked_threads.remember(config, tracked, fields.get("fid") or "",
                                 fields.get("title") or "")
        _subscribe(tracked, config)
    except Exception:
        pass                      # 记账失败不影响"已经发出去了"这个事实
    if ident:
        said = "发出去了：No.%s · %s/t/%s" % (ident, base_of(config), ident)
    elif verdict["state"] == "ok":
        said = "发出去了（没回号），去看一眼：%s/f/" % base_of(config)
    else:
        said = "不确定发没发出去：岛的回话里既没有成功也没有报错。去看一眼：%s/Forum" % (
            base_of(config))
    return ("%s｜%s" % (carried, said)) if carried else said


def _subscribe(tid, config=None) -> bool:
    """把这条串订阅上（站上的"订阅"列表里有它，网页端也看得到）。

    订阅 ID 是客户端自己起的一串，存在设置里，可跨客户端搬（`xdcmd` 的 README 也这么说）。
    失败就当没订上——轮询那条路不依赖它。
    """
    cookie = cookie_of(config)
    if not cookie or not str(tid or "").strip():
        return False
    try:
        from rin.xdao import threads as tracked_threads
        uuid = tracked_threads.feed_uuid(config)
    except Exception:
        return False
    if not uuid:
        return False
    body = urllib.parse.urlencode({"uuid": uuid, "tid": str(tid)}).encode("utf-8")
    request = urllib.request.Request(base_of(config) + "/Api/addFeed", data=body,
                                     method="POST")
    request.add_header("Content-Type", "application/x-www-form-urlencoded")
    request.add_header("User-Agent", UA)
    request.add_header("Cookie", "userhash=" + cookie)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read(2000)
        return True
    except Exception:
        return False


def _page(params: dict) -> int:
    try:
        page = int(params.get("page") or 1)
    except (TypeError, ValueError):
        return 1
    return max(1, min(50, page))
