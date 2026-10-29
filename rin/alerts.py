"""往网页顶上挂一条提示。

写的是 `<数据>/alerts.json`，网页读到就挂在顶栏，点掉就把文件删了。走文件而不是库，
理由和 NapCat 守护那边一样：写它的可能不止一个进程（守护是 root、适配器和循环各是
各的），而记录库是单写者。一条一次性的运维提示不值得挤进那条路。

同一种事半小时内只挂一次：被牵着刷屏时，别把顶栏也刷成一样的一行。
"""

from __future__ import annotations

import io
import json
import os
import time

DEDUPE = 1800.0


def path_of(config) -> str:
    return os.path.join(str(getattr(config, "data_dir", ".") or "."), "alerts.json")


def _read(path: str) -> dict:
    try:
        with io.open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def raise_alert(config, kind: str, text: str, dedupe: float = DEDUPE) -> bool:
    """挂一条提示。同一种事在 `dedupe` 秒内已经挂过就跳过，返回有没有写。"""
    body = str(text or "").strip()
    if not body:
        return False
    path = path_of(config)
    stamp = time.time()
    old = _read(path)
    if (str(old.get("kind") or "") == str(kind or "")
            and stamp - float(old.get("at") or 0) < dedupe):
        return False
    payload = {"kind": str(kind or ""), "text": body,
               "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "at": stamp}
    temp = path + ".tmp"
    try:
        folder = os.path.dirname(path)
        if folder:
            os.makedirs(folder, exist_ok=True)
        with io.open(temp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        os.chmod(temp, 0o644)              # 网页那个进程（另一个用户也行）要读得到
        os.replace(temp, path)
    except OSError:
        return False
    return True


def clear(config) -> None:
    try:
        os.remove(path_of(config))
    except OSError:
        pass
