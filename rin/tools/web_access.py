"""上网：抓一个公开网页，或者搜一下。

从 `palimpsest-duet/duet/web_access.py` 搬过来，去掉两样东西：
JS 渲染（要 Playwright + Chromium，这台 4G 的机器不装）和百度备用搜索
（实测这台腾讯云机器上只拿到 1548 字节的验证页，没有结果）。留下的是：
SSRF 防护、安全重定向、trafilatura 优先 / 标准库兜底、五分钟缓存。

★ 三条底线：
  · 只允许 http/https，只走 80/443，域名必须解析到公网地址——避免模型被网页
    里的一句话牵着去读本机接口、内网服务、云元数据；
  · 每一跳重定向都重新校验；
  · 网页里的字是**数据**，不是指令。工具说明和 sink 里都写了这一句。
"""

from __future__ import annotations

import functools
import html
import ipaddress
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Dict, List, Optional, Tuple

try:                                    # 装了更好，没装也能跑
    import trafilatura
except Exception:  # pragma: no cover
    trafilatura = None

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36 Edg/124.0.0.0"
)
# 实测这台腾讯云机器：www 入口给的是真结果，cn 入口返回一堆无关内容
# （问「deepseek v4」回法语论坛）。换机器要重测这一条。
SEARCH_URL = "https://www.bing.com/search"
# 有 key 就走 Tavily（准、带正文片段）；没有就退回 Bing 的 RSS，质量差一截。
TAVILY_URL = "https://api.tavily.com/search"
_TAVILY_KEY = ""
# 我们抓不到正文时的兜底：DeepSeek 的抓取接口（POST /v1/tools/fetch），
# 能把 JS 页面也渲染成 Markdown。按成功请求计费，所以只在必要时用。
DEEPSEEK_FETCH_URL = "https://api.deepseek.com/v1/tools/fetch"
DEEPSEEK_SEARCH_URL = "https://api.deepseek.com/v1/tools/search"
_DEEPSEEK_KEY = ""
# 官方说 rerank_score ≥1.5 算优质；低于这个数的直接丢，免得把跑题的结果喂给模型。
MIN_SCORE = 1.0
# 抽出来少于这么多字，就当没抽到，值得花一次抓取接口。
MIN_TEXT = 300

MAX_BYTES = 2_000_000
MAX_RESULTS = 6
DEFAULT_CHARS = 3000
MAX_CHARS = 8000
TIMEOUT = 20.0
CACHE_TTL = 300.0
ALLOWED_PORTS = {80, 443}
ALLOWED_CONTENT = {
    "text/html", "application/xhtml+xml", "text/plain", "text/markdown",
    "text/xml", "application/xml", "application/rss+xml",
    "application/atom+xml", "application/json",
}
BLOCKED_HOSTS = {"localhost", "localhost.localdomain", "metadata",
                 "metadata.google.internal"}
BLOCKED_SUFFIXES = (".local", ".internal", ".localhost", ".home", ".lan")


class WebAccessError(ValueError):
    """抓不到、不许抓、或者抓回来的东西不能用。"""


def _is_public_ip(value: str) -> bool:
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        return False
    if address.version == 6 and address.ipv4_mapped:
        address = address.ipv4_mapped
    return bool(address.is_global)


def validate_url(url: str) -> str:
    """规范化并校验；不合格就抛 WebAccessError。这是 SSRF 的闸门。"""
    raw = str(url or "").strip()
    if not raw:
        raise WebAccessError("url 是空的")
    if len(raw) > 4096:
        raise WebAccessError("url 太长")
    parsed = urllib.parse.urlsplit(raw)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise WebAccessError("只允许 http/https，不接受 %s" % (parsed.scheme or "空"))
    if parsed.username or parsed.password:
        raise WebAccessError("url 里不要带用户名或密码")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise WebAccessError("url 没有主机名")
    if host in BLOCKED_HOSTS or any(host.endswith(s) for s in BLOCKED_SUFFIXES):
        raise WebAccessError("不能访问本地或内部主机：%s" % host)
    try:
        port = parsed.port
    except ValueError as exc:
        raise WebAccessError("端口不合法") from exc
    if port is None:
        port = 443 if scheme == "https" else 80
    if port not in ALLOWED_PORTS:
        raise WebAccessError("只允许 80/443 端口，不接受 %s" % port)
    try:
        addresses = {item[4][0] for item in
                     socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)}
    except OSError as exc:
        raise WebAccessError("DNS 解析失败：%s" % host) from exc
    if not addresses:
        raise WebAccessError("DNS 没有返回地址：%s" % host)
    for address in addresses:
        if not _is_public_ip(address):
            raise WebAccessError("主机解析到非公网地址，拒绝：%s" % address)
    return urllib.parse.urlunsplit(
        (scheme, parsed.netloc, parsed.path or "/", parsed.query, ""))


class _SafeRedirect(urllib.request.HTTPRedirectHandler):
    """每一跳都先过一遍 validate_url，别让重定向把闸门绕过去。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        validate_url(target)
        return super().redirect_request(req, fp, code, msg, headers, target)


# build_opener 会带上默认的 ProxyHandler：环境里配了 HTTP(S)_PROXY 就走代理，
# 没配就直连。这台机器在国内，要读墙外的站就靠这个。
_OPENER = urllib.request.build_opener(_SafeRedirect())


def _fetch(url: str) -> Tuple[str, str, str, bytes, bool]:
    """GET 一个公开地址，返回 (最终地址, content-type, charset, body, 是否截断)。"""
    target = validate_url(url)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        "Accept-Encoding": "identity",
        "Cache-Control": "no-cache",
    }
    request = urllib.request.Request(target, headers=headers, method="GET")
    try:
        with _OPENER.open(request, timeout=TIMEOUT) as response:
            content_type = (response.headers.get_content_type() or "").lower()
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read(MAX_BYTES + 1)
            truncated = len(body) > MAX_BYTES
            return response.geturl(), content_type, charset, body[:MAX_BYTES], truncated
    except urllib.error.HTTPError as exc:
        raise WebAccessError("网页返回 HTTP %s" % exc.code) from exc
    except urllib.error.URLError as exc:
        raise WebAccessError("取不到网页：%s" % exc.reason) from exc
    except (TimeoutError, socket.timeout) as exc:
        raise WebAccessError("网页超时") from exc


def _strip_tags(value: str) -> str:
    text = re.sub(r"<script[\s\S]*?</script>", " ", value or "", flags=re.I)
    text = re.sub(r"<style[\s\S]*?</style>", " ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _decode(body: bytes, charset: str) -> str:
    try:
        return body.decode(charset or "utf-8", errors="replace")
    except LookupError:
        return body.decode("utf-8", errors="replace")


class _TextExtractor(HTMLParser):
    """没有 trafilatura 时的兜底：扔脚本样式，按块级标签断行。"""

    SKIP = ("script", "style", "noscript")
    BREAK = ("p", "div", "br", "li", "h1", "h2", "h3", "h4", "tr")

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: List[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        merged = "".join(self.parts)
        merged = re.sub(r"[ \t\r\f\v]+", " ", merged)
        return re.sub(r"\n\s*\n+", "\n\n", merged).strip()


def extract_page(raw: str) -> Dict[str, str]:
    """正文：trafilatura 优先，不行就退回标准库那把。"""
    title = ""
    text = None
    if trafilatura is not None:
        try:
            metadata = trafilatura.extract_metadata(raw)
            title = str(getattr(metadata, "title", "") or "")
            text = trafilatura.extract(raw, include_comments=False,
                                       include_tables=True, favor_recall=True)
        except Exception:
            text = None
    if not text:
        parser = _TextExtractor()
        try:
            parser.feed(raw)
            text = parser.text()
        except Exception:
            text = ""
    if not title:
        match = re.search(r"<title[^>]*>(.*?)</title>", raw, flags=re.I | re.S)
        if match:
            title = _strip_tags(match.group(1))
    return {"title": title.strip(), "text": (text or "").strip()}


def set_tavily_key(key: str) -> None:
    """启动时喂一次；空串就是没有，退回免费源。"""
    global _TAVILY_KEY
    _TAVILY_KEY = str(key or "").strip()


def set_deepseek_key(key: str) -> None:
    """启动时喂一次；空串就是没有，抽不到就抽不到，不花钱。"""
    global _DEEPSEEK_KEY
    _DEEPSEEK_KEY = str(key or "").strip()


def needs_upgrade(text: str) -> bool:
    """自己抽出来的正文够不够用：不够就值得走一次抓取接口。"""
    return len(str(text or "").strip()) < MIN_TEXT


def parse_deepseek(payload: dict, limit: int = MAX_RESULTS,
                   min_score: float = MIN_SCORE) -> List[Dict[str, str]]:
    """DeepSeek 的搜索结果：按 rerank_score 排序，低分的丢掉。

    如果全都被丢光，就退回全部——宁可给几条也可能不对的，也不要假装没搜到。
    """
    items = []
    for raw in payload.get("results") or []:
        url = str(raw.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        try:
            score = float(raw.get("rerank_score") or 0.0)
        except (TypeError, ValueError):
            score = 0.0
        body = str(raw.get("summary") or raw.get("snippet") or "")
        items.append({"title": str(raw.get("title") or "") or url,
                      "url": url, "snippet": _strip_tags(body)[:400],
                      "score": score})
    good = [item for item in items if item["score"] >= min_score]
    chosen = good or items
    chosen.sort(key=lambda item: item["score"], reverse=True)
    return [{"title": item["title"], "url": item["url"],
             "snippet": item["snippet"]}
            for item in chosen[:max(1, min(limit, 10))]]


def parse_tavily(payload: dict, limit: int = MAX_RESULTS) -> List[Dict[str, str]]:
    results: List[Dict[str, str]] = []
    for item in (payload.get("results") or [])[:max(1, min(limit, 10))]:
        url = str(item.get("url") or "").strip()
        if not url.startswith(("http://", "https://")):
            continue
        results.append({"title": str(item.get("title") or "") or url,
                        "url": url,
                        "snippet": _strip_tags(str(item.get("content") or ""))[:400]})
    return results


def parse_bing_rss(raw: bytes, limit: int = MAX_RESULTS) -> List[Dict[str, str]]:
    try:
        root = ET.fromstring(raw)
    except ET.ParseError as exc:
        raise WebAccessError("搜索结果不是有效 XML") from exc
    results: List[Dict[str, str]] = []
    for item in root.findall("./channel/item")[:max(1, min(limit, 10))]:
        link = (item.findtext("link") or "").strip()
        if not link:
            continue
        results.append({
            "title": _strip_tags(item.findtext("title") or "") or link,
            "url": link,
            "snippet": _strip_tags(item.findtext("description") or "")[:400],
        })
    return results


def _slice(text: str, offset: int, limit: int) -> Tuple[str, int, bool]:
    """取第 offset 到 offset+limit 字；返回 (这段, 总字数, 后面还有没有)。"""
    total = len(text)
    start = max(0, int(offset or 0))
    end = start + max(200, min(int(limit or DEFAULT_CHARS), MAX_CHARS))
    return text[start:end], total, end < total


class WebAccess:
    """带五分钟缓存的取网页入口。同一个链接连着抓两次不该打两次网络。"""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._cache: Dict[tuple, Tuple[float, object]] = {}

    def _cached(self, key: tuple):
        with self._lock:
            item = self._cache.get(key)
            if not item:
                return None
            stamp, payload = item
            if time.monotonic() - stamp > CACHE_TTL:
                self._cache.pop(key, None)
                return None
            return payload

    def _store(self, key: tuple, payload) -> None:
        with self._lock:
            self._cache[key] = (time.monotonic(), payload)

    # ── 抓一页 ────────────────────────────────────────────────────────────
    def _ds_read(self, url: str) -> Dict[str, object]:
        """让 DeepSeek 替我们抓：返回标题和 Markdown 正文。"""
        body = json.dumps({"url": url}).encode("utf-8")
        request = urllib.request.Request(
            DEEPSEEK_FETCH_URL, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + _DEEPSEEK_KEY,
                     "User-Agent": USER_AGENT})
        try:
            with _OPENER.open(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read(MAX_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise WebAccessError("抓取接口返回 HTTP %s" % exc.code) from exc
        except urllib.error.URLError as exc:
            raise WebAccessError("抓取接口连不上：%s" % exc.reason) from exc
        except (ValueError, TimeoutError, socket.timeout) as exc:
            raise WebAccessError("抓取接口返回的东西看不懂：%s" % exc) from exc
        text = str(payload.get("content") or "").strip()
        return {"url": url, "title": str(payload.get("title") or ""),
                "text": text, "chars": len(text), "truncated": False,
                "via": "deepseek"}

    def read(self, url: str) -> Dict[str, object]:
        """先自己抓（免费）；抽不到正文，或者根本连不上，再走抓取接口。"""
        target = validate_url(url)
        key = ("read", target)
        hit = self._cached(key)
        if hit is not None:
            return hit

        try:
            final, content_type, charset, body, truncated = _fetch(target)
        except WebAccessError as first:
            if not _DEEPSEEK_KEY:
                raise
            # 我们连不上（超时、被墙、反爬）——让它替我们抓一次。
            try:
                payload = self._ds_read(target)
            except WebAccessError as second:
                # 两路都没成：自己抓为什么失败也得说出来，别只留下兜底那一句 401
                raise WebAccessError(
                    "这一页没抓到：自己抓取失败（%s），兜底抓取接口也不行（%s）。"
                    "换个链接，或者过一会儿再试。" % (first, second)) from second
            self._store(key, payload)
            return payload

        if ALLOWED_CONTENT and content_type and content_type not in ALLOWED_CONTENT:
            if not content_type.startswith("text/"):
                raise WebAccessError("这不是网页（%s）" % content_type)
        page = extract_page(_decode(body, charset))
        payload = {"url": final, "title": page["title"], "text": page["text"],
                   "chars": len(page["text"]), "truncated": truncated}

        if _DEEPSEEK_KEY and needs_upgrade(str(payload["text"])):
            try:
                better = self._ds_read(final)
            except WebAccessError:
                better = None
            if better is not None and len(better["text"]) > len(payload["text"]):
                payload = better

        self._store(key, payload)
        return payload

    def fetch(self, url: str, offset: int = 0, limit: int = DEFAULT_CHARS) -> str:
        page = self.read(url)
        text = str(page["text"] or "")
        if not text:
            return "（这个页面没有抽到正文：%s）" % page["url"]
        part, total, more = _slice(text, offset, limit)
        head = "标题：%s\n链接：%s" % (page["title"] or "（无标题）", page["url"])
        if offset or more:
            head += "\n（全文 %d 字，这里是第 %d–%d 字%s）" % (
                total, int(offset) + 1, int(offset) + len(part),
                "，要看后面就再调一次、offset 用 %d" % (int(offset) + len(part))
                if more else "")
        return "%s\n\n%s" % (head, part)

    # ── 搜一下 ────────────────────────────────────────────────────────────
    def _tavily(self, text: str, count: int) -> List[Dict[str, str]]:
        body = json.dumps({"query": text,
                           "max_results": max(1, min(count, 10)),
                           "search_depth": "basic",
                           "include_answer": False}).encode("utf-8")
        request = urllib.request.Request(
            TAVILY_URL, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + _TAVILY_KEY,
                     "User-Agent": USER_AGENT})
        try:
            with _OPENER.open(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read(MAX_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise WebAccessError("搜索接口返回 HTTP %s（key 可能过期或额度用完）"
                                 % exc.code) from exc
        except urllib.error.URLError as exc:
            raise WebAccessError("搜索接口连不上：%s" % exc.reason) from exc
        except (ValueError, TimeoutError, socket.timeout) as exc:
            raise WebAccessError("搜索接口返回的东西看不懂：%s" % exc) from exc
        return parse_tavily(payload, count)

    def _deepseek(self, text: str, count: int) -> List[Dict[str, str]]:
        body = json.dumps({"query": text,
                           "count": max(1, min(count, 10)),
                           "need_content": False}).encode("utf-8")
        request = urllib.request.Request(
            DEEPSEEK_SEARCH_URL, data=body, method="POST",
            headers={"Content-Type": "application/json",
                     "Authorization": "Bearer " + _DEEPSEEK_KEY,
                     "User-Agent": USER_AGENT})
        try:
            with _OPENER.open(request, timeout=TIMEOUT) as response:
                payload = json.loads(response.read(MAX_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise WebAccessError("搜索接口返回 HTTP %s" % exc.code) from exc
        except urllib.error.URLError as exc:
            raise WebAccessError("搜索接口连不上：%s" % exc.reason) from exc
        except (ValueError, TimeoutError, socket.timeout) as exc:
            raise WebAccessError("搜索接口返回的东西看不懂：%s" % exc) from exc
        return parse_deepseek(payload, count)

    def _bing(self, text: str, count: int) -> List[Dict[str, str]]:
        target = SEARCH_URL + "?" + urllib.parse.urlencode(
            {"q": text, "format": "rss"})
        _, _, _, body, _ = _fetch(target)
        return parse_bing_rss(body, count)

    def search(self, query: str, count: int = MAX_RESULTS) -> str:
        text = str(query or "").strip()
        if not text:
            raise WebAccessError("搜什么？")
        key = ("search", text, int(count))
        hit = self._cached(key)
        if hit is None:
            # 有 key 先走 Tavily；不通、或者没给 key，就退回 Bing 的 RSS。
            # 免费源实测：百度跳验证码、搜狗拦 IP、360 的链接会话绑定打不开，
            # 而 Bing 偶尔整段跑题——所以免费那条只当兜底。
            hit = []
            if _DEEPSEEK_KEY:
                try:
                    hit = self._deepseek(text, count)
                except WebAccessError:
                    hit = []
            if not hit and _TAVILY_KEY:
                try:
                    hit = self._tavily(text, count)
                except WebAccessError:
                    hit = []
            if not hit:
                try:
                    hit = self._bing(text, count)
                except WebAccessError:
                    hit = []
            self._store(key, hit)
        if not hit:
            return "没有搜到结果。"
        lines = []
        for index, item in enumerate(hit, 1):
            lines.append("%d. %s\n   %s" % (index, item["title"], item["url"]))
            if item["snippet"]:
                lines.append("   %s" % item["snippet"])
        return "\n".join(lines)


_SHARED: Optional[WebAccess] = None
_SHARED_LOCK = threading.Lock()


def shared() -> WebAccess:
    """一个进程一份缓存就够了。"""
    global _SHARED
    with _SHARED_LOCK:
        if _SHARED is None:
            _SHARED = WebAccess()
        return _SHARED
