"""出站闸门：她一口气能往外发多少条。

为什么要有：`qq` 有九十来个动作，发消息那几个能直接发给任意好友和群；岛上还有
**公开发帖**——发出去收不回来，还可能踩站规。要是哪次被网上的一段话牵着走，最脏的
结果不是性格跑偏，是刷屏：账号被风控、群被踢、饼干被没收，都收不回来。

所以按**通道分桶**：每个桶自己有「一分钟几条、一天几条」。微信…咳，QQ 那边给得宽
（正常说话碰不到），岛那边给得紧——公开说话本来就该更省，站上每个板块还另有一道
冷却（15–120 秒），我们不该比它更急。

计数落在 `<数据>/outbound.json`：重启不清零，适配器和循环看的是同一份。
老文件顶层就是 `{day, count, recent}`（只有一个桶那会儿），读到这种东西按 `qq` 那一桶认。
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
DEFAULT = "qq"

# 桶 → (一分钟, 一天)。加一个平台就在这里加一行。
BUDGETS = {
    "qq": (PER_MINUTE, PER_DAY),
    "xdao": (2, 20),
}


def path_of(config) -> str:
    return os.path.join(str(getattr(config, "data_dir", ".") or "."), NAME)


def _load(path: str) -> dict:
    try:
        with io.open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    if "day" in data:                     # 老格式：顶层就是 qq 那一桶
        return {DEFAULT: data}
    return data


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


def budget_of(bucket: str) -> tuple:
    return BUDGETS.get(str(bucket or DEFAULT), BUDGETS[DEFAULT])


def allow(config, moment: float = None, bucket: str = DEFAULT) -> tuple:
    """放行一条？返回 (放行, 不放行时的那句话)。放行时顺手记一笔。"""
    stamp = time.time() if moment is None else float(moment)
    path = path_of(config)
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    minute_cap, day_cap = budget_of(bucket)
    data = _load(path)
    slot = data.get(bucket)
    if not isinstance(slot, dict) or str(slot.get("day") or "") != day:
        slot = {"day": day, "count": 0, "recent": []}
    recent = []
    for value in slot.get("recent") or []:
        try:
            when = float(value)
        except (TypeError, ValueError):
            continue
        if stamp - when < WINDOW:
            recent.append(when)
    count = int(slot.get("count") or 0)
    if len(recent) >= minute_cap:
        return False, ("一分钟里已经发了 %d 条，这一条先不发。" % len(recent))
    if count >= day_cap:
        return False, ("今天已经发了 %d 条，先不发了。" % count)
    recent.append(stamp)
    data[bucket] = {"day": day, "count": count + 1, "recent": recent}
    _save(path, data)
    return True, ""


def used(config, moment: float = None, bucket: str = DEFAULT) -> dict:
    """现在这份计数长什么样（网页和自检看它）。"""
    stamp = time.time() if moment is None else float(moment)
    day = time.strftime("%Y-%m-%d", time.localtime(stamp))
    slot = _load(path_of(config)).get(bucket)
    if not isinstance(slot, dict) or str(slot.get("day") or "") != day:
        return {"day": day, "count": 0, "recent": 0}
    recent = 0
    for value in slot.get("recent") or []:
        try:
            if stamp - float(value) < WINDOW:
                recent += 1
        except (TypeError, ValueError):
            continue
    return {"day": day, "count": int(slot.get("count") or 0), "recent": recent}
