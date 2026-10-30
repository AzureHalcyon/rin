"""QQ 那边的一双手：按动作名调 NapCat。

NapCat 有九十来个动作。**一个动作一个工具**会把她的工具面撑满、把选择变成负担；
**一份大文档全塞进提示词**更糟——每步都付这份 token。所以分三层：

1. 工具说明里只写最常用的三个调用（十几行）；
2. 其余动作分组成小文件放在 `<数据>/workspace/QQ-动作/`，索引在
   `QQ-动作清单.md`——她自己用 `run` 去读，读到哪组算哪组；
3. 不想开 shell 就直接问：`qq(action="index")` 看分组、`qq(action="help",
   params={"group": "群"})` 看某一组，都是轻的。

端点与令牌从环境来：`NAPCAT_BASE`（默认 `http://127.0.0.1:3000`）、
`NAPCAT_TOKEN`。和适配器读的是同一份 `/etc/palimpsest/qq.env`。
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.request
from typing import List

from loop import prompt_files
from rin import alerts, limit
from rin.tools import media, qq_actions, workspace

NAMES = ("qq",)

DOC_NAME = "QQ-动作清单.md"
_TIMEOUT = 20
_KEEP = 1500          # 返回太长就截断：够她判断，别把上下文塞满

# 这三个的 `message` 可以是段数组，所以能挂图。发文件和相册那些另有自己的入口。
WITH_IMAGE = ("send_private_msg", "send_group_msg", "send_msg")


def doc_path(config=None) -> str:
    """索引页放在她工作区里：`run`、`write` 都摸得到，她自己也能改。"""
    if config is None:
        return DOC_NAME
    return os.path.join(workspace.root_of(config), DOC_NAME)


def write_docs(config) -> List[str]:
    """把索引和每一组的小文件写进工作区（内容没变就不动）。返回写过的路径。"""
    root = workspace.root_of(config) if config is not None else "."
    written: List[str] = []
    for rel, body in qq_actions.render_all():
        path = os.path.join(root, rel)
        try:
            os.makedirs(os.path.dirname(path) or root, exist_ok=True)
            if os.path.isfile(path):
                with open(path, encoding="utf-8") as handle:
                    if handle.read() == body:
                        written.append(path)
                        continue
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(body)
            written.append(path)
        except OSError:
            continue          # 写不进去也不该影响她起来
    return written


def tools(config=None) -> List[dict]:
    text = prompt_files.tool_texts()
    return [
        {"type": "function", "function": {
            "name": "qq",
            "description": text["qq"],
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {"type": "string",
                               "description": text["qq.action"]},
                    "params": {"type": "object",
                               "description": text["qq.params"]},
                },
                "required": ["action"],
            },
        }},
    ]



def _setting(config, key: str, env: str, fallback: str = "") -> str:
    """设置页填了就用它，没填再退回环境变量，最后才是默认。"""
    value = ""
    if config is not None:
        try:
            from rin import settings
            data_dir = str(getattr(config, "data_dir", "") or "")
            if data_dir:
                value = str(settings.load(data_dir).get(key) or "").strip()
        except Exception:          # 读不到设置不该让工具挂掉
            value = ""
    if value:
        return value
    return str(os.environ.get(env) or "").strip() or fallback


def _base(config=None) -> str:
    base = _setting(config, "qq_base", "NAPCAT_BASE", "http://127.0.0.1:3000")
    return base if "://" in base else "http://" + base


def _post(action: str, params: dict, config=None) -> dict:
    body = json.dumps(params, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(_base(config).rstrip("/") + "/" + action,
                                     data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    token = _setting(config, "qq_token", "NAPCAT_TOKEN")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        raw = response.read() or b"{}"
    return json.loads(raw.decode("utf-8", "replace") or "{}")


def _with_images(params: dict, config=None) -> str:
    """把 `params.image` / `params.images` 变成消息里的图，回来一句"带了什么"（没图就空串）。

    NapCat 的 `message` 可以是字符串（整段文字）也可以是段数组；**只有段数组能带图**。
    所以要么不带图（原样发），要么把 `message` 换成 `[文字段, 图段…]`——文字在前、
    图按给的顺序跟在后面，和人在群里"说一句然后甩两张图"是一个样子。
    图用 `base64://`：NapCat 在容器里，看不见 `/opt/rin/data`，路径传不过去；
    缩过之后一张几百 KB，这一段 base64 只活在发给 NapCat 的那个请求里，
    她这边给的一直是**路径**。
    """
    raw = params.pop("images", None)
    if raw is None:
        raw = params.pop("image", None)
    if not raw:
        return ""
    paths = raw if isinstance(raw, list) else [raw]
    parts: List[dict] = []
    message = params.get("message")
    if isinstance(message, str):
        if message.strip():
            parts.append({"type": "text", "data": {"text": message}})
    elif isinstance(message, list):
        parts.extend(message)         # 她要是自己拼好了段，就接着往下加
    notes = []
    for item in paths:
        picture = media.prepare(config, item)
        parts.append({"type": "image", "data": {
            "file": "base64://" + base64.b64encode(picture["data"]).decode("ascii")}})
        notes.append(media.note_of(picture))
    params["message"] = parts
    return "带图 %d 张：%s" % (len(notes), "、".join(notes))


def call(name: str, arguments: str, config=None) -> str:
    if name != "qq":
        return "没有这个工具：%s" % name
    try:
        payload = json.loads(arguments or "{}")
    except ValueError:
        return "参数不是 JSON。"
    if not isinstance(payload, dict):
        return "参数不是对象。"
    action = str(payload.get("action") or "").strip()
    if not action:
        return "要给一个 action：先 qq(action=\"index\") 看有哪些组。"
    params = payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return "params 要是一个对象。"
    # 两层轻查询（渐进披露）：不调 NapCat，也不把整棵目录倒进上下文
    if action == "index":
        return qq_actions.index_text()
    if action == "help":
        return qq_actions.group_text(str(params.get("group") or "").strip())
    if action in qq_actions.BLOCKED:
        return "这个动作不给：%s（%s）。" % (action, qq_actions.BLOCKED[action])
    if not qq_actions.known(action):
        return "清单里没有这个动作：%s。用 index 看组，help 看一组。" % action
    # 图要在过闸之前准备好：找不到文件、不是图，都不该占掉一次发送额度
    carried = ""
    if action in WITH_IMAGE:
        if "message" not in params and "text" in params:
            params["message"] = params.pop("text")     # text 是这边的别名
        try:
            carried = _with_images(params, config)
        except media.MediaError as exc:
            return str(exc)
    # 发消息那几个过一道闸：被牵着刷屏是收不回来的那种坏（见 rin/limit.py）
    if action.startswith("send"):
        allowed, why = limit.allow(config)
        if not allowed:
            alerts.raise_alert(config, "qq_send_blocked",
                               "QQ 发消息被闸住了：%s" % why)
            return "这条没发：%s" % why
    try:
        reply = _post(action, params, config)
    except urllib.error.HTTPError as exc:
        return "NapCat 拒了（HTTP %d）：%s" % (exc.code, _brief(exc))
    except (urllib.error.URLError, OSError) as exc:
        return "连不上 NapCat（%s）：通道那边可能掉线了。" % exc
    except ValueError as exc:
        return "NapCat 回的不是 JSON：%s" % exc

    group = qq_actions.group_of(action)
    danger = qq_actions.danger_of(action)
    note = qq_actions.summary_of(action) or group or "动作"
    text = json.dumps(reply, ensure_ascii=False)
    if len(text) > _KEEP:
        text = text[:_KEEP] + "…（截断）"
    head = "%s（%s）" % (action, note)
    if carried:
        head = "%s｜%s" % (carried, head)
    if str(reply.get("status") or "") == "failed" or int(reply.get("retcode") or 0) != 0:
        return "%s 没做成：%s" % (head, text)
    if danger:
        return "%s 做成了（⚠ 这一步改的是别人/群里的东西，已经记在记录里）：%s" % (
            head, text)
    return "%s：%s" % (head, text)


def _brief(exc) -> str:
    try:
        return exc.read()[:200].decode("utf-8", "replace").replace("\n", " ")
    except Exception:
        return str(exc)
