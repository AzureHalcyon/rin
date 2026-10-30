"""把一张图片变成"能发出去的那一坨字节"。

往外发图片要过两条完全不同的路：QQ 那边 NapCat 收 `base64://`，岛那边是一个
multipart 里的 `image` 文件部分。两边在出发前要做的是同一件事——在图库或工作区里
找到文件、确认它真是张图片、太大了先缩——所以这段放在这里，`qq` 和 `xdao` 都用它。

路径规矩和核心的 `look` 一模一样：认 `图片/…`（图片库）和 `<数据>/workspace` 里面的，
跑出去就拒。图片太大时缩到长边 1024——不是省流量，是岛上和 QQ 都可能不收几 MB 的
附件，而且这一步不改变她"发的是哪张图片"。jpg / png / gif / webp 都认；webp 顺手
转成 jpg（两家客户端对 webp 的支持参差）。
"""

from __future__ import annotations

import io
import os

MAX_READ = 12 * 1024 * 1024      # 读之前的上限：再大就别读了
MAX_SEND = 3 * 1024 * 1024       # 处理完还这么大就别发了
MAX_EDGE = 1024                  # 长边缩到这个尺寸
KEEP_RAW = 1500 * 1024           # 本来就在这个尺寸内、又不算大，就原样发
QUALITY = 85
IMAGE_DIR = "图片"               # 图片库：和工作区平级；老记录还指着 `workspace/收件`

# 文件头 → (mime, 扩展名)。和核心 `look` 认的那几种保持一致。
_MAGIC = ((b"\xff\xd8\xff", "image/jpeg", ".jpg"),
          (b"\x89PNG\r\n\x1a\n", "image/png", ".png"),
          (b"GIF8", "image/gif", ".gif"),
          (b"RIFF", "image/webp", ".webp"))


class MediaError(Exception):
    """发不出去的原因，一句话，直接给她看。"""


def locate(config, raw: str):
    """把 `图片/2026-10/x.jpg` / `收件/2026-10-30/x.jpg` 落到盘上。

    回来 `(绝对路径, 相对数据目录)`。只认两处：**图片库**（`<数据>/图片`，收进来的
    图片都在那儿，她的 `run` / `write` 碰不到）和**工作区**（她自己画的、写进来的）。
    """
    data_dir = os.path.abspath(str(getattr(config, "data_dir", "") or "."))
    text = str(raw or "").strip()
    if not text:
        raise MediaError("要给一张图的路径（图库里 `图片/…`，或者工作区里的相对路径）。")
    target = ""
    for head in (IMAGE_DIR, "workspace"):
        if not text.startswith(head + "/"):
            continue
        root = os.path.join(data_dir, head)
        candidate = os.path.abspath(os.path.join(root, text[len(head) + 1:]))
        if not (candidate == root or candidate.startswith(root + os.sep)):
            raise MediaError("只发得出图库和工作区里的图：%s 跑到外面去了。" % text)
        target = candidate
        break
    if not target:                    # 没有前缀：按工作区算（老用法）
        root = os.path.join(data_dir, "workspace")
        target = os.path.abspath(os.path.join(root, text))
        if not (target == root or target.startswith(root + os.sep)):
            raise MediaError("只发得出图库和工作区里的图：%s 跑到外面去了。" % text)
    if not os.path.isfile(target):
        raise MediaError("没有这个文件：%s" % text)
    shown = os.path.relpath(target, data_dir).replace(os.sep, "/")
    return target, shown


def sniff(head: bytes):
    """认得出来的话回来 `(mime, 扩展名)`。"""
    for magic, mime, ext in _MAGIC:
        if head.startswith(magic):
            return mime, ext
    return None


def _flatten(image):
    """透明底压白：JPEG 不带 alpha，不铺白的话透明处会变黑。"""
    try:
        from PIL import Image
    except Exception:                 # 没有 PIL 就不动，后面按原样发
        return image
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        base = Image.new("RGB", image.size, (255, 255, 255))
        rgba = image.convert("RGBA")
        base.paste(rgba, mask=rgba.split()[-1])
        return base
    return image.convert("RGB")


def prepare(config, raw: str, max_edge: int = MAX_EDGE) -> dict:
    """读一张图片，回来能直接塞进请求里的东西。

    `{"path", "name", "mime", "data", "w", "h", "shrunk"}`；`path` 是相对数据目录的
    那种写法（和记录、`look` 一致），`name` 是给 multipart 用的文件名。
    """
    target, shown = locate(config, raw)
    try:
        size = os.path.getsize(target)
    except OSError as exc:
        raise MediaError("读不了：%s" % exc)
    if size > MAX_READ:
        raise MediaError("这张太大了（%.1f MB）：先缩一下再发。" % (size / 1048576.0))
    try:
        with open(target, "rb") as handle:
            data = handle.read()
    except OSError as exc:
        raise MediaError("读不了：%s" % exc)
    if not data:
        raise MediaError("这个文件是空的：%s" % shown)
    kind = sniff(data[:12])
    if kind is None:
        raise MediaError("这不像图（文件头 %r）：发不出去。" % data[:6])
    mime, ext = kind
    stem = os.path.splitext(os.path.basename(target))[0]

    width = height = 0
    shrunk = False
    try:
        from PIL import Image
        with Image.open(io.BytesIO(data)) as image:
            width, height = int(image.size[0]), int(image.size[1])
            # webp 转掉：QQ 和岛都更认 jpg，而 webp 各家客户端支持参差
            if mime == "image/webp" or max(width, height) > max_edge or len(data) > KEEP_RAW:
                scale = float(max_edge) / float(max(width, height))
                small = _flatten(image)
                if scale < 1.0:
                    small = small.resize((max(1, int(width * scale)),
                                          max(1, int(height * scale))), Image.LANCZOS)
                buffer = io.BytesIO()
                small.save(buffer, format="JPEG", quality=QUALITY, optimize=True)
                data = buffer.getvalue()
                mime, ext = "image/jpeg", ".jpg"
                width, height = int(small.size[0]), int(small.size[1])
                shrunk = True
    except MediaError:
        raise
    except Exception:
        pass                          # 缩不动就原样发：能发出去比好看重要

    if len(data) > MAX_SEND:
        raise MediaError("缩完还有 %.1f MB，太大发不出去：换一张小点的。" % (len(data) / 1048576.0))
    return {"path": shown, "name": stem + ext, "mime": mime, "data": data,
            "w": width, "h": height, "shrunk": shrunk}


def note_of(picture: dict) -> str:
    """一行交代：发了哪张、多大、缩没缩。"""
    parts = [picture["path"]]
    if picture["w"] and picture["h"]:
        parts.append("%d×%d" % (picture["w"], picture["h"]))
    parts.append("%.0f KB" % (len(picture["data"]) / 1024.0))
    if picture["shrunk"]:
        parts.append("缩过")
    return "，".join(parts)
