"""bilibili 的几个工具：搜、看元数据、取字幕、拿直链。

走的是 B 站网页版公开接口，和 dsh 那个插件的做法一致（`dsh-bilibili` 的 fork）：

- 搜索 `/x/web-interface/search/type`，被风控（默认综合排序）时降级成 `order=pubdate` 重试
- 元数据 `/x/web-interface/view`
- 直链 `/x/player/playurl`（`fnval=0`，拿单段 mp4）
- 字幕 `/x/player/wbi/v2`，要 WBI 签名（`w_rid`），再取轨道 JSON

三个约定：
- 请求头带浏览器的 UA / Referer / Origin；不带登录 cookie 也能用，但很多字幕轨要登录。
  有 `BILI_SESSDATA` 环境变量（或在设置里填）就带上，能多拿到一部分。
- 字幕 JSON 在 CDN 上，**不带 cookie 去取**——凭证只发给 API 主机。
- 网络错误、被风控、没有字幕轨都翻成一句中文写回记录，不抛栈。
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Dict, List, Optional

from loop import prompt_files

NAMES = ("bilibili_search", "bilibili_video", "bilibili_subtitles",
         "bilibili_playurl", "bilibili_transcript")

BASE = "https://api.bilibili.com"
USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/134.0.0.0 Safari/537.36")
TIMEOUT = 20
MAX_TRANSCRIPT_CHARS = 6000

# 语音转写：百炼 qwen3-asr-flash（HTTP，音频用 base64 data URI，不用上传公网地址）。
# 键与地址走环境变量，入口从凭证里填：ASR_KEY / ASR_URL / ASR_MODEL。
ASR_URL = os.environ.get(
    "ASR_URL",
    "https://dashscope.aliyuncs.com/api/v1/services/aigc/multimodal-generation/generation")
ASR_MODEL = os.environ.get("ASR_MODEL", "qwen-audio-3.1-asr-flash")
ASR_CHUNK_SECONDS = 290          # 单段上限：这一族模型一次最多 300 秒，留 10 秒余量
ASR_MAX_SECONDS = 1800           # 一次最多转 30 分钟，超了截断并说明
FFMPEG = os.environ.get("FFMPEG", "ffmpeg")

# wbi 签名用的固定置换表
_MIXIN_TAB = (
    46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35,
    27, 43, 5, 49, 33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13,
    37, 48, 7, 16, 24, 55, 40, 61, 26, 17, 0, 1, 60, 51, 30, 4,
    22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36, 20, 34, 44, 52,
)

_WBI_CACHE: Dict[str, object] = {"keys": None, "at": 0.0}
_WBI_TTL = 3600.0

# 匿名 cookie：不带 buvid 的请求容易被 WAF 挡（HTTP 412）。首页与 finger/spi 各给一部分。
_COOKIE_CACHE: Dict[str, object] = {"header": "", "at": 0.0}
_COOKIE_TTL = 3600.0


def _anonymous_cookies() -> str:
    header = str(_COOKIE_CACHE["header"] or "")
    if header and time.time() - float(_COOKIE_CACHE["at"]) < _COOKIE_TTL:
        return header
    jar: Dict[str, str] = {}
    try:
        request = urllib.request.Request("https://www.bilibili.com/",
                                         headers={"User-Agent": USER_AGENT})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            for raw in response.headers.get_all("Set-Cookie") or []:
                pair = raw.split(";")[0]
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    jar[key.strip()] = value.strip()
        request = urllib.request.Request(
            BASE + "/x/frontend/finger/spi",
            headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            data = json.loads(response.read()).get("data") or {}
        if data.get("b_3"):
            jar["buvid3"] = str(data["b_3"])
        if data.get("b_4"):
            jar["buvid4"] = str(data["b_4"])
    except (urllib.error.URLError, OSError, ValueError):
        pass
    header = "; ".join("%s=%s" % (key, value) for key, value in jar.items())
    _COOKIE_CACHE.update({"header": header, "at": time.time()})
    return header


def _headers(with_cookie: bool = True) -> Dict[str, str]:
    headers = {
        "User-Agent": USER_AGENT,
        "Referer": "https://www.bilibili.com/",
        "Origin": "https://www.bilibili.com",
        "Accept": "application/json, text/plain, */*",
    }
    if with_cookie:
        # 登录 cookie 和匿名 buvid 要一起带：只给 SESSDATA 时播放器接口容易被 WAF 打回 412。
        parts = []
        sessdata = os.environ.get("BILI_SESSDATA", "").strip()
        if sessdata:
            parts.append("SESSDATA=" + sessdata)
        anonymous = _anonymous_cookies()
        if anonymous:
            parts.append(anonymous)
        if parts:
            headers["Cookie"] = "; ".join(parts)
    return headers


class BiliError(RuntimeError):
    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code


def _get_json(path: str, params: Optional[dict] = None,
              with_cookie: bool = True) -> dict:
    """GET 一个接口；412/429/503 这类风控或限流退避一秒再来一次。"""
    for attempt in range(2):
        try:
            return _get_json_once(path, params, with_cookie)
        except BiliError as exc:
            if exc.code not in ("http-412", "http-429", "http-503") or attempt:
                raise
            time.sleep(1.5)
    raise BiliError("重试也没成功", "http")


def _get_json_once(path: str, params: Optional[dict] = None,
                   with_cookie: bool = True) -> dict:
    url = BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    request = urllib.request.Request(url, headers=_headers(with_cookie))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise BiliError("HTTP %s" % exc.code, "http-%s" % exc.code) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise BiliError("连不上 bilibili：%s" % exc, "network") from exc
    try:
        data = json.loads(body)
    except ValueError as exc:
        raise BiliError("返回的不是 JSON", "bad-json") from exc
    code = data.get("code")
    if code != 0:
        raise BiliError("接口返回 %s：%s" % (code, data.get("message") or ""),
                        "risk" if code == -412 else "code")
    return data


# ── WBI 签名 ────────────────────────────────────────────────────────────────
def _mixin_key(orig: str) -> str:
    if len(orig) < 64:
        raise BiliError("wbi key 太短", "wbi")
    return "".join(orig[index] for index in _MIXIN_TAB)[:32]


def _wbi_keys() -> tuple:
    cached = _WBI_CACHE["keys"]
    if cached and time.time() - float(_WBI_CACHE["at"]) < _WBI_TTL:
        return cached  # type: ignore[return-value]
    data = _get_json("/x/web-interface/nav")
    wbi = ((data.get("data") or {}).get("wbi_img") or {})
    img = str(wbi.get("img_url") or "").rsplit("/", 1)[-1].split(".")[0]
    sub = str(wbi.get("sub_url") or "").rsplit("/", 1)[-1].split(".")[0]
    if not img or not sub:
        raise BiliError("拿不到 wbi key", "wbi")
    pair = (img, sub)
    _WBI_CACHE.update({"keys": pair, "at": time.time()})
    return pair


def _wbi_sign(params: dict) -> dict:
    img, sub = _wbi_keys()
    with_wts = {key: str(value) for key, value in params.items()}
    with_wts["wts"] = str(int(time.time()))
    query = "&".join(
        "%s=%s" % (key, urllib.parse.quote(with_wts[key].translate(
            str.maketrans("", "", "!'()*")), safe=""))
        for key in sorted(with_wts))
    digest = hashlib.md5((query + _mixin_key(img + sub)).encode("utf-8")).hexdigest()
    signed = dict(with_wts)
    signed["w_rid"] = digest
    return signed


# ── 工具 ────────────────────────────────────────────────────────────────────
def _search(text: dict, payload: dict) -> str:
    keyword = str(payload.get("keyword") or "").strip()
    if not keyword:
        return "bilibili_search 需要 keyword。"
    page = max(1, int(payload.get("page") or 1))
    size = min(20, max(1, int(payload.get("page_size") or 10)))
    order = str(payload.get("order") or "").strip()
    params = {"search_type": "video", "keyword": keyword, "page": page,
              "page_size": size}
    if order:
        params["order"] = order
    try:
        data = _get_json("/x/web-interface/search/type", params)
    except BiliError as exc:
        if exc.code != "risk" or order:
            return "搜索没成功：%s" % exc
        try:
            params["order"] = "pubdate"
            data = _get_json("/x/web-interface/search/type", params)
        except BiliError as again:
            return "搜索没成功（降级也失败）：%s" % again
    rows = ((data.get("data") or {}).get("result") or [])
    if not rows:
        return "没搜到「%s」。" % keyword
    lines = ["找到 %d 条（第 %d 页）：" % (len(rows), page)]
    for row in rows:
        title = _clean(str(row.get("title") or ""))
        lines.append("%s %s · %s · %s播放 · %s" % (
            row.get("bvid"), title, row.get("author") or "",
            _num(row.get("play")), _dur(row.get("duration"))))
    lines.append("看一条的详情用 bilibili_video；取字幕用 bilibili_subtitles；"
                 "要直链用 bilibili_playurl。")
    return "\n".join(lines)


def _video(text: dict, payload: dict) -> str:
    bvid = _bvid(payload)
    if not bvid:
        return "bilibili_video 需要 bvid。"
    try:
        data = _get_json("/x/web-interface/view", {"bvid": bvid})
    except BiliError as exc:
        return "取不到 %s：%s" % (bvid, exc)
    item = data.get("data") or {}
    pages = item.get("pages") or []
    lines = [
        "%s %s" % (bvid, item.get("title") or ""),
        "UP：%s · 发布 %s · %s播放 · %s" % (
            (item.get("owner") or {}).get("name") or "",
            time.strftime("%Y-%m-%d", time.localtime(item.get("pubdate") or 0)),
            _num(item.get("stat", {}).get("view")), _dur(item.get("duration"))),
        "简介：%s" % _clip(str(item.get("desc") or "").replace("\n", " "), 300),
    ]
    if len(pages) > 1:
        lines.append("共 %d 分P：%s" % (len(pages), "、".join(
            "%d.%s" % (page.get("page"), _clip(str(page.get("part") or ""), 20))
            for page in pages[:10])))
    lines.append("cid：%s" % item.get("cid"))
    lines.append("链接：https://www.bilibili.com/video/%s" % bvid)
    return "\n".join(lines)


def _subtitles(text: dict, payload: dict) -> str:
    bvid = _bvid(payload)
    if not bvid:
        return "bilibili_subtitles 需要 bvid。"
    want = str(payload.get("language") or "zh-CN").strip()
    try:
        detail = _get_json("/x/web-interface/view", {"bvid": bvid}).get("data") or {}
        cid = detail.get("cid")
        signed = _wbi_sign({"bvid": bvid, "cid": cid})
        player = _get_json("/x/player/wbi/v2", signed)
    except BiliError as exc:
        return "拿不到 %s 的字幕轨：%s" % (bvid, exc)
    tracks = (((player.get("data") or {}).get("subtitle") or {})
              .get("subtitles") or [])
    if not tracks:
        return ("%s 没有可读的字幕轨（很多要登录 cookie）。"
                "要文字内容可以用 bilibili_transcript 走语音转写。" % bvid)
    track = next((row for row in tracks
                  if str(row.get("lan") or "") == want), tracks[0])
    url = str(track.get("subtitle_url") or "")
    if url.startswith("//"):
        url = "https:" + url
    if not url:
        return "这一条的字幕地址是空的。"
    request = urllib.request.Request(url, headers=_headers(with_cookie=False))
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
            body = json.loads(response.read())
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "字幕文件取不回来：%s" % exc
    cues = [str((cue or {}).get("content") or "").strip()
            for cue in (body.get("body") or [])]
    cues = [cue for cue in cues if cue]
    if not cues:
        return "字幕轨是空的。"
    joined = "\n".join(cues)
    clipped = joined[:MAX_TRANSCRIPT_CHARS]
    tail = "" if len(joined) <= MAX_TRANSCRIPT_CHARS else (
        "\n（截断：全文 %d 字，这里给了前 %d 字）" % (len(joined), MAX_TRANSCRIPT_CHARS))
    return "字幕（%s，%d 条）：\n%s%s" % (track.get("lan") or want, len(cues),
                                        clipped, tail)


def _playurl(text: dict, payload: dict) -> str:
    bvid = _bvid(payload)
    if not bvid:
        return "bilibili_playurl 需要 bvid。"
    qn = int(payload.get("qn") or 64)
    try:
        detail = _get_json("/x/web-interface/view", {"bvid": bvid}).get("data") or {}
        data = _get_json("/x/player/playurl", {
            "bvid": bvid, "cid": detail.get("cid"), "qn": qn, "fnval": 0,
            "platform": "html5", "high_quality": 1})
    except BiliError as exc:
        return "拿不到直链：%s" % exc
    durl = ((data.get("data") or {}).get("durl") or [])
    if not durl:
        return "这条没有单段 mp4 直链（可能是 DASH，或者要登录）。"
    first = durl[0]
    return ("%s 的直链（%s，%s）：\n%s" % (
        bvid, _dur(data.get("data", {}).get("timelength")), _size(first.get("size")),
        first.get("url")))


_SCHEMAS = {
    "bilibili_search": {
        "type": "object",
        "properties": {
            "keyword": {"type": "string"},
            "page": {"type": "number"},
            "page_size": {"type": "number"},
            "order": {"type": "string"},
        },
        "required": ["keyword"],
    },
    "bilibili_video": {
        "type": "object",
        "properties": {"bvid": {"type": "string"}},
        "required": ["bvid"],
    },
    "bilibili_subtitles": {
        "type": "object",
        "properties": {
            "bvid": {"type": "string"},
            "language": {"type": "string"},
        },
        "required": ["bvid"],
    },
    "bilibili_playurl": {
        "type": "object",
        "properties": {
            "bvid": {"type": "string"},
            "qn": {"type": "number"},
        },
        "required": ["bvid"],
    },
    "bilibili_transcript": {
        "type": "object",
        "properties": {"bvid": {"type": "string"}},
        "required": ["bvid"],
    },
}


def tools() -> List[dict]:
    text = prompt_files.tool_texts()
    out = []
    for name in NAMES:
        schema = json.loads(json.dumps(_SCHEMAS[name]))
        for param in schema["properties"]:
            label = text.get(name + "." + param, "")
            if label:
                schema["properties"][param]["description"] = label
        out.append({"type": "function",
                    "function": {"name": name,
                                 "description": text[name],
                                 "parameters": schema}})
    return out


def call(name: str, arguments: str) -> str:
    payload = {}
    if str(arguments or "").strip():
        try:
            payload = json.loads(arguments)
        except ValueError:
            return "%s 的参数不是 JSON。" % name
        if not isinstance(payload, dict):
            return "%s 的参数不是对象。" % name
    handler = _CALLS.get(name)
    if handler is None:
        return "没有这个工具：%s" % name
    try:
        return handler({}, payload)
    except BiliError as exc:
        return "bilibili 出错了：%s" % exc


# ── 语音转写 ────────────────────────────────────────────────────────────────
def _transcript(text: dict, payload: dict) -> str:
    """没有字幕轨时的退路：拉低清 mp4 → ffmpeg 抽音轨切段 → 百炼转写。"""
    bvid = _bvid(payload)
    if not bvid:
        return "bilibili_transcript 需要 bvid。"
    key = str(os.environ.get("ASR_KEY") or "").strip()
    if not key:
        return "语音转写没配：环境里要有 ASR_KEY（入口会拿凭证里的键填上）。"
    try:
        detail = _get_json("/x/web-interface/view", {"bvid": bvid}).get("data") or {}
        data = _get_json("/x/player/playurl", {
            "bvid": bvid, "cid": detail.get("cid"), "qn": 16, "fnval": 0,
            "platform": "html5", "high_quality": 1})
    except BiliError as exc:
        return "取视频失败：%s" % exc
    durl = ((data.get("data") or {}).get("durl") or [])
    if not durl:
        return "这条拿不到单段 mp4，转写做不了。"
    seconds = int(detail.get("duration") or 0)
    note = ""
    if seconds > ASR_MAX_SECONDS:
        note = "（原片 %s，只转了前 %s）" % (_dur(seconds), _dur(ASR_MAX_SECONDS))
    work = _workdir()
    try:
        mp4 = os.path.join(work, "video.mp4")
        try:
            _download(str(durl[0].get("url") or ""), mp4)
        except (urllib.error.URLError, OSError) as exc:
            return "下载视频失败：%s" % exc
        chunks = _split_audio(mp4, work, seconds)
        if not chunks:
            return "抽音轨失败：%s 跑不起来，或这条视频没有音轨。" % FFMPEG
        pieces = []
        for path in chunks:
            piece = _asr_file(path, key)
            if piece.startswith("!"):
                return "转写失败：%s" % piece[1:]
            pieces.append(piece)
        joined = "\n".join(piece for piece in pieces if piece).strip()
        if not joined:
            return "转写回来了，但一个字都没有（可能是纯音乐或静音）。"
        clipped = joined[:MAX_TRANSCRIPT_CHARS]
        tail = "" if len(joined) <= MAX_TRANSCRIPT_CHARS else (
            "\n（截断：全文 %d 字，这里给了前 %d 字）" % (len(joined), MAX_TRANSCRIPT_CHARS))
        return "语音转写%s（%d 段）：\n%s%s" % (note, len(chunks), clipped, tail)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _workdir() -> str:
    """找一个**真能写**的临时目录。

    逐个试：`BILI_TMPDIR` → 系统 temp → 进程目录下的 `data/tmp` → 进程目录。
    Windows 沙箱里系统 temp 建得出来却写不进去，所以光看能不能 mkdir 不够，
    要真写一个探针文件。
    """
    candidates = []
    custom = str(os.environ.get("BILI_TMPDIR") or "").strip()
    if custom:
        candidates.append(custom)
    candidates.append(tempfile.gettempdir())
    candidates.append(os.path.join(os.getcwd(), "data", "tmp"))
    candidates.append(os.getcwd())
    for base in candidates:
        try:
            os.makedirs(base, exist_ok=True)
            probe = os.path.join(base, ".bili-probe-" + uuid.uuid4().hex[:6])
            with open(probe, "w", encoding="utf-8") as handle:
                handle.write("x")
            os.remove(probe)
        except OSError:
            continue
        work = os.path.join(base, "bili-audio-" + uuid.uuid4().hex[:8])
        os.makedirs(work, exist_ok=True)
        return work
    raise BiliError("找不到可写的临时目录", "tmp")


def _download(url: str, path: str) -> None:
    request = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT, "Referer": "https://www.bilibili.com/"})
    with urllib.request.urlopen(request, timeout=60) as response, \
            open(path, "wb") as handle:
        while True:
            block = response.read(1 << 20)
            if not block:
                break
            handle.write(block)


def _run(command: List[str], timeout: int = 600) -> int:
    try:
        return subprocess.run(command, stdout=subprocess.DEVNULL,
                              stderr=subprocess.PIPE, timeout=timeout).returncode
    except (OSError, subprocess.SubprocessError):
        return -1


def _split_audio(mp4: str, work: str, seconds: int) -> List[str]:
    """抽成 16k 单声道 mp3，按 10 分钟切段；切不开就整段一个文件。"""
    limit = max(1, min(seconds or ASR_MAX_SECONDS, ASR_MAX_SECONDS))
    pattern = os.path.join(work, "chunk%03d.mp3")
    code = _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", mp4,
                 "-t", str(limit), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
                 "-f", "segment", "-segment_time", str(ASR_CHUNK_SECONDS), pattern])
    if code == 0:
        return sorted(os.path.join(work, name) for name in os.listdir(work)
                      if name.startswith("chunk") and name.endswith(".mp3"))
    single = os.path.join(work, "audio.mp3")
    code = _run([FFMPEG, "-hide_banner", "-loglevel", "error", "-y", "-i", mp4,
                 "-t", str(limit), "-vn", "-ac", "1", "-ar", "16000", "-b:a", "32k",
                 single])
    return [single] if code == 0 and os.path.exists(single) else []


def _asr_file(path: str, key: str) -> str:
    with open(path, "rb") as handle:
        payload = handle.read()
    body = {
        "model": ASR_MODEL,
        "input": {"messages": [{"role": "user", "content": [
            {"audio": "data:audio/mpeg;base64," + base64.b64encode(payload).decode()}]}]},
        # qwen-audio-3.x 要显式给格式，否则 400 UNSUPPORTED_FORMAT: format is empty
        "parameters": {"format": "mp3"},
    }
    request = urllib.request.Request(
        ASR_URL, data=json.dumps(body).encode("utf-8"),
        headers={"Authorization": "Bearer " + key,
                 "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=300) as response:
            raw = json.loads(response.read())
    except urllib.error.HTTPError as exc:
        body = exc.read()[:400].decode("utf-8", "replace")
        if "ASR_RESPONSE_HAVE_NO_WORDS" in body:
            return "!这段没识别出说话（可能只有音乐或静音）"
        if "AUDIO_DURATION_TOO_LONG" in body:
            return "!这段太长，超过了接口单次上限"
        return "!HTTP %s：%s" % (exc.code, body)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        return "!接口出错：%s" % exc
    return _asr_text(raw)


def _asr_text(raw: dict) -> str:
    """两种返回结构都认：新的 `output.output.text`，旧的 `output.choices`。"""
    inner = ((raw.get("output") or {}).get("output")) or {}
    flat = str(inner.get("text") or "").strip()
    if not flat:
        flat = str(((inner.get("sentence") or {}).get("text")) or "").strip()
    if flat:
        return flat
    choices = (((raw.get("output") or {}).get("choices")) or [])
    if not choices:
        message = str((raw.get("output") or {}).get("message") or raw.get("message") or "")
        return "!没有结果：%s" % (message[:160] or "接口没给 choices")
    content = ((choices[0] or {}).get("message") or {}).get("content") or ""
    if isinstance(content, str):
        return content.strip()
    texts = [str((block or {}).get("text") or "") for block in content
             if isinstance(block, dict)]
    return "\n".join(text for text in texts if text).strip()
def _bvid(payload: dict) -> str:
    return str(payload.get("bvid") or "").strip()


def _clean(text: str) -> str:
    out, skipping = [], False
    for char in text:
        if char == "<":
            skipping = True
        elif char == ">":
            skipping = False
        elif not skipping:
            out.append(char)
    return "".join(out).replace("&amp;", "&").strip()


def _clip(text: str, limit: int) -> str:
    body = " ".join(text.split())
    return body[:limit] + ("…" if len(body) > limit else "")


def _num(value) -> str:
    try:
        count = int(value or 0)
    except (TypeError, ValueError):
        return str(value or "")
    if count >= 10000:
        return "%.1f万" % (count / 10000.0)
    return str(count)


def _dur(value) -> str:
    try:
        seconds = int(value or 0)
    except (TypeError, ValueError):
        return str(value or "")
    if seconds >= 1000:          # 播放时长给的是毫秒
        seconds //= 1000
    return "%d:%02d" % (seconds // 60, seconds % 60)


def _size(value) -> str:
    try:
        size = int(value or 0)
    except (TypeError, ValueError):
        return str(value or "")
    if size >= 1024 * 1024:
        return "%.1f MB" % (size / 1024 / 1024.0)
    return "%d KB" % (size // 1024)


# 名字 → 处理函数。放在文件末尾：处理函数都在上面定义完了。
_CALLS = {
    "bilibili_search": _search,
    "bilibili_video": _video,
    "bilibili_subtitles": _subtitles,
    "bilibili_playurl": _playurl,
    "bilibili_transcript": _transcript,
}