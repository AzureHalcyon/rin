"""出站闸门：她一口气能发多少条 QQ 消息。

为什么要有：`qq` 有九十来个动作，发消息那几个能直接发给任意好友和群。要是哪次被
网上的一段话牵着走，最脏的结果不是性格跑偏，是刷屏——账号被风控、群里被踢，都收不
回来。所以发消息的动作过一道闸：一分钟 `PER_MINUTE` 条、一天 `PER_DAY` 条，超了
这一条就不发，回一句实话，并在网页顶上挂一条提示。

计数落在 `<数据>/outbound.json`：重启不清零，适配器和循环看的是同一份。阈值给得宽
——正常说话碰不到，只有连着往外倒才会撞上。
"""

from __future__ import annotations

import io
import json
import os
import time

PER_MINUTE = 20
PER_DAY = 400
WINDOW = 60.0
NAME = "outbound.json"


def path_of(config) -> str:
    return os.path.join(str(getattr(config, "data_dir", ".") or "."), NAME)


def _load(path: str) -> dict:
    try:
        with io.open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _save(path: str, data: dict) -> None:
    temp = path + ".tmp"
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with io.open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False)
        os.replace(temp, path)
    except OSError:
        pass


def allow(config, moment: float = None) -> tuple:
    """放行一条？返回 (放行, 不放行时的那句话)。放行时顺手记一笔。"""
    stamp = time.time() if moment is None else float(moment)
    path = path_of(config)
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    data = _load(path)
    if str(data.get("day") or "") != day:
        data = {"day": day, "count": 0, "recent": []}
    recent = []
    for value in data.get("recent") or []:
        try:
            when = float(value)
        except (TypeError, ValueError):
            continue
        if stamp - when < WINDOW:
            recent.append(when)
    count = int(data.get("count") or 0)
    if len(recent) >= PER_MINUTE:
        return False, ("一分钟里已经发了 %d 条，这一条先不发。" % len(recent))
    if count >= PER_DAY:
        return False, ("今天已经发了 %d 条，先不发了。" % count)
    recent.append(stamp)
    _save(path, {"day": day, "count": count + 1, "recent": recent})
    return True, ""


def used(config, moment: float = None) -> dict:
    """现在这份计数长什么样（网页和自检看它）。"""
    stamp = time.time() if moment is None else float(moment)
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    data = _load(path_of(config))
    if str(data.get("day") or "") != day:
        return {"day": day, "count": 0, "recent": 0}
    recent = 0
    for value in data.get("recent") or []:
        try:
            if stamp - float(value) < WINDOW:
                recent += 1
        except (TypeError, ValueError):
            continue
    return {"day": day, "count": int(data.get("count") or 0), "recent": recent}
