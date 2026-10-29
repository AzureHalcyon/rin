"""她自己的串：谁回过、回到哪一条了。

岛没有"推送"，只能自己去看。这份账记两件事：

1. 她发过、回过的串（`post` / `reply` 成功之后记一笔）；
2. 每个串已经**送进她耳朵**的最大回复号。

轮询（`rin.xdao.feed`）只按这份账去问，不扫全站——一个串一条请求，通常个位数。
存在 `<数据>/xdao-threads.json`，跟着数据目录走，不进仓库。
"""

from __future__ import annotations

import io
import json
import os
import secrets

NAME = "xdao-threads.json"
UUID_KEY = "xdao_feed_uuid"      # 订阅 ID：站上"订阅"按这串记，客户端自己起、可跨客户端搬


def path_of(config) -> str:
    return os.path.join(str(getattr(config, "data_dir", ".") or "."), NAME)


def load(config) -> dict:
    path = path_of(config)
    try:
        with io.open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"threads": {}}
    if not isinstance(data, dict) or not isinstance(data.get("threads"), dict):
        return {"threads": {}}
    return data


def save(config, data: dict) -> None:
    path = path_of(config)
    temp = path + ".tmp"
    try:
        with io.open(temp, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(temp, path)
    except OSError:
        pass          # 记不下也不该让发帖失败


def remember(config, tid, fid="", title="") -> None:
    """她发/回过这个串：记下来，轮询才会去看它。"""
    key = str(tid or "").strip()
    if not key:
        return
    data = load(config)
    entry = data["threads"].setdefault(key, {"seen": 0})
    if fid:
        entry["fid"] = str(fid)
    if title:
        entry["title"] = str(title)[:60]
    data["threads"][key] = entry
    save(config, data)


def known(config) -> dict:
    return load(config).get("threads") or {}


def seen(config, tid) -> int:
    entry = known(config).get(str(tid)) or {}
    try:
        return int(entry.get("seen") or 0)
    except (TypeError, ValueError):
        return 0


def note_seen(config, tid, reply_id) -> None:
    key = str(tid or "").strip()
    try:
        value = int(reply_id or 0)
    except (TypeError, ValueError):
        return
    data = load(config)
    entry = data["threads"].setdefault(key, {"seen": 0})
    if value > int(entry.get("seen") or 0):
        entry["seen"] = value
        data["threads"][key] = entry
        save(config, data)


def forget(config, tid) -> None:
    data = load(config)
    if str(tid) in data["threads"]:
        data["threads"].pop(str(tid), None)
        save(config, data)


def feed_uuid(config) -> str:
    """订阅 ID：第一次用到时生成一串，跟这份账放在一起（不进仓库、不打印）。

    放这儿而不是 settings.json：`settings.load` 只认登记过的键，放那儿每次读都会被
    过滤掉，于是每轮都生成新的——订阅 ID 一变，站上的订阅就等于全丢了。
    """
    data = load(config)
    value = str(data.get(UUID_KEY) or "").strip()
    if value:
        return value
    value = secrets.token_urlsafe(16)
    data[UUID_KEY] = value
    save(config, data)
    return value
