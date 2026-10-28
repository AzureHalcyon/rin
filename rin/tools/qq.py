"""QQ 那边的一双手：按动作名调 NapCat。

为什么不是一个动作一个工具：NapCat 有九十来个动作，铺成工具面会把她的上下文
撑满、把选择变成负担。这里只给一个入口，配一份清单——清单在
`<数据>/workspace/QQ-动作清单.md`（启动时按 `rin.tools.qq_actions` 现写），
她要做什么就去查，查完照动作名调。最常用的三个直接写在工具说明里。

端点与令牌从环境来：`NAPCAT_BASE`（默认 `http://127.0.0.1:3000`）、
`NAPCAT_TOKEN`。和适配器读的是同一份 `/etc/palimpsest/qq.env`。
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from typing import List

from loop import prompt_files
from rin.tools import qq_actions, workspace

NAMES = ("qq",)

DOC_NAME = "QQ-动作清单.md"
_TIMEOUT = 20
_KEEP = 1500          # 返回太长就截断：够她判断，别把上下文塞满


def doc_path(config=None) -> str:
    """清单放在她工作区里：`run`、`write` 都摸得到，她自己也能改。"""
    if config is None:
        return DOC_NAME
    return os.path.join(workspace.root_of(config), DOC_NAME)


def write_doc(config) -> str:
    """把清单写进工作区（内容没变就不动它）。返回路径。"""
    path = doc_path(config)
    try:
        body = qq_actions.render_markdown()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if os.path.isfile(path):
            with open(path, encoding="utf-8") as handle:
                if handle.read() == body:
                    return path
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(body)
    except OSError:
        pass          # 写不进去也不该影响她起来
    return path


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



def _base() -> str:
    base = str(os.environ.get("NAPCAT_BASE") or "").strip()
    if not base:
        base = "http://127.0.0.1:3000"
    return base if "://" in base else "http://" + base


def _post(action: str, params: dict) -> dict:
    body = json.dumps(params, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(_base().rstrip("/") + "/" + action,
                                     data=body, method="POST")
    request.add_header("Content-Type", "application/json")
    token = str(os.environ.get("NAPCAT_TOKEN") or "").strip()
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urllib.request.urlopen(request, timeout=_TIMEOUT) as response:
        raw = response.read() or b"{}"
    return json.loads(raw.decode("utf-8", "replace") or "{}")


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
        return "要给一个 action（清单在 %s）。" % doc_path(config)
    if action in qq_actions.BLOCKED:
        return "这个动作不给：%s（%s）。要做让本人来。" % (
            action, qq_actions.BLOCKED[action])
    if not qq_actions.known(action):
        return ("清单里没有这个动作：%s。能做什么、参数怎么给写在 %s；"
                "真需要新的，让本人加进清单。" % (action, doc_path(config)))
    params = payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return "params 要是一个对象。"
    try:
        reply = _post(action, params)
    except urllib.error.HTTPError as exc:
        return "NapCat 拒了（HTTP %d）：%s" % (exc.code, _brief(exc))
    except (urllib.error.URLError, OSError) as exc:
        return "连不上 NapCat（%s）：通道那边可能掉线了。" % exc
    except ValueError as exc:
        return "NapCat 回的不是 JSON：%s" % exc

    group, note, danger = qq_actions.describe(action)
    text = json.dumps(reply, ensure_ascii=False)
    if len(text) > _KEEP:
        text = text[:_KEEP] + "…（截断）"
    head = "%s（%s）" % (action, note or group or "动作")
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
