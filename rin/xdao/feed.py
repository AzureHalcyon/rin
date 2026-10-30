"""轮询：她自己那几个串，有没有人回。

一次只问她自己的串（`xdao-threads.json` 里那几个，通常个位数）：拿每串**最后一页**的回复，
比"已经送过的最大号"新的、而且**不是她自己**发的，就推给网关的收件箱。

★ 一律 `policy="batch"`——攒着，**不叫醒她**。岛是公开版块，别人回一句不该打断她手上的事；
   她想看的时候用 `messages`，网页上也能看见这些进件。
★ 送不进去就不推进"已送过的号"：下一轮还从这儿来，别把话吞了。
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request

from rin import settings
from rin.tools import xdao
from rin.xdao import threads

PER_PAGE = 19           # 一个串一页 19 条回复（含主串那一页的算法见 WinX 文档）
KEEP = 800              # 一条回复最多进记录这么多字
SEND_RETRIES = 3


def _pages(reply_count: int) -> int:
    try:
        count = int(reply_count or 0)
    except (TypeError, ValueError):
        count = 0
    return max(1, (count + PER_PAGE - 1) // PER_PAGE)


def our_name(config) -> str:
    """她在那儿的落款（饼干显示名）。拿它挡掉"自己回自己"。"""
    data_dir = str(getattr(config, "data_dir", "") or "")
    try:
        return str(settings.load(data_dir).get("xdao_name") or "").strip()
    except Exception:
        return ""


def collect(config, log=lambda text: None) -> list:
    """(进件列表, {串号: 已送到哪一条})——只读，不动账。"""
    ours = our_name(config)
    items = []
    marks = {}
    for tid, entry in sorted(threads.known(config).items()):
        title = str(entry.get("title") or "")
        seen = threads.seen(config, tid)
        try:
            info = xdao.api("/Api/thread?id=%s&page=1" % tid, config)
            if isinstance(info, dict):
                pages = _pages(info.get("ReplyCount"))
                if pages > 1:                 # 新回复在最后一页
                    info = xdao.api("/Api/thread?id=%s&page=%d" % (tid, pages),
                                    config)
        except xdao.XdaoError as exc:
            log("[岛] 看 No.%s 失败：%s" % (tid, exc))
            continue
        if not isinstance(info, dict):
            continue
        replies = [item for item in (info.get("Replies") or [])
                   if isinstance(item, dict)]
        newest = seen
        for reply in sorted(replies, key=lambda item: int(item.get("id") or 0)):
            try:
                ident = int(reply.get("id") or 0)
            except (TypeError, ValueError):
                continue
            newest = max(newest, ident)
            if ident <= seen:
                continue
            who = str(reply.get("user_hash") or "").strip() or "无名氏"
            if ours and who == ours:
                continue                  # 自己说的，不用再告诉她一遍
            body = " ".join(xdao.plain(reply.get("content")).split())
            picture = _fetch_image(reply, config)
            if not body and not picture:
                continue                  # 空回复（删掉了、只有表情）：跳过
            item = {
            }
            item.update({
                "source": "岛",
                "kind": "user",
                "chat_type": "thread",
                "chat_id": "xdao:%s" % tid,
                "chat_name": title or ("No.%s" % tid),
                "sender_id": who,
                "sender_name": who,
                "text": body[:KEEP],
                "policy": "batch",        # 攒着，永远不叫她起来
                "ext_id": "r%s" % ident,
                "meta": {"thread": tid, "reply": ident,
                         "fid": entry.get("fid") or "",
                         **({"images": [picture]} if picture else {})},
            })
            items.append(item)
        marks[tid] = newest
    return items, marks


def _fetch_image(reply: dict, config) -> dict:
    """回复里挂了图片就把它落地，回来 `{"path", "w", "h"}`；没有就空字典。

    落进**图片库**（`<数据>/图片/<年-月>/`，和 QQ、网页来的图片一个地方）——那儿在
    工作区外面，她的 `run` / `write` 动不了；记录里只存路径，核心拼请求时再读出来、
    缩一遍。
    """
    url = xdao.image_of(reply)
    if not url:
        return {}
    try:
        request = urllib.request.Request(
            url, headers={"User-Agent": xdao.UA})
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read(8 * 1024 * 1024)
    except Exception:
        return {}
    if not raw:
        return {}
    ext = str(reply.get("ext") or ".jpg").strip() or ".jpg"
    if not ext.startswith("."):
        ext = "." + ext
    month = time.strftime("%Y-%m")
    folder = os.path.join(str(getattr(config, "data_dir", ".") or "."), "图片", month)
    name = "%s-%s-岛%s%s" % (time.strftime("%Y%m%d"), time.strftime("%H%M%S"),
                             reply.get("id") or "x", ext)
    try:
        os.makedirs(folder, exist_ok=True)
        with open(os.path.join(folder, name), "wb") as handle:
            handle.write(raw)
    except OSError:
        return {}
    width = height = 0
    try:
        import io as _io
        from PIL import Image
        with Image.open(_io.BytesIO(raw)) as image:
            width, height = int(image.size[0]), int(image.size[1])
    except Exception:
        pass
    rel = "图片/%s/%s" % (month, name)
    return {"path": rel, "w": width, "h": height}


def deliver(gateway: str, token: str, item: dict, timeout: int = 15) -> tuple:
    """推一条进件；回来 `(推成了没有, 网关说重了吗)`。"""
    payload = json.dumps(item, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(gateway.rstrip("/") + "/inbox", data=payload,
                                     method="POST")
    request.add_header("Content-Type", "application/json")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            reply = json.loads(response.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError, http.client.HTTPException):
        return False, False
    return True, bool((reply or {}).get("duplicate"))


def poll_once(config, gateway: str, token: str, log=print) -> int:
    """跑一轮。回来"新推了几条"。"""
    items, marks = collect(config, log)
    if not items:
        for tid, newest in marks.items():
            threads.note_seen(config, tid, newest)
        return 0
    added = 0
    ok_all = True
    for item in items:
        ok, duplicate = deliver(gateway, token, item)
        if not ok:
            ok_all = False
            continue
        added += 0 if duplicate else 1
        if not duplicate:
            log("[岛] 收到 No.%s 的回复（%s）：%s" % (
                item["meta"]["reply"], item["sender_name"], item["text"][:40]))
    if ok_all:
        for tid, newest in marks.items():
            threads.note_seen(config, tid, newest)
    else:
        log("[岛] 有一批没推进去，下一轮还从这儿来")
    return added


def serve(config, gateway: str, token: str, interval: float = 300.0,
          log=print) -> None:
    """一直轮。间隔在设置页里也能改（`xdao_feed_interval`）。"""
    log("[岛] 开始看她的串：每 %d 秒一轮，只攒着、不叫她" % int(interval))
    while True:
        try:
            poll_once(config, gateway, token, log)
        except Exception as exc:            # 一轮炸了不该让整个通道停
            log("[岛] 这一轮出错（%s）：%s" % (type(exc).__name__, exc))
        time.sleep(max(30.0, float(interval)))


def interval_of(config, fallback: float = 300.0) -> float:
    data_dir = str(getattr(config, "data_dir", "") or "")
    try:
        value = settings.load(data_dir).get("xdao_feed_interval")
        return max(30.0, float(value)) if value else fallback
    except Exception:
        return fallback
