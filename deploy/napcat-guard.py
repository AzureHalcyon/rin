#!/usr/bin/env python3
"""NapCat 登不上就别再敲门：连续失败到 10 次就停下容器，并在网页顶上挂一条。

为什么要它：账号被风控冻结时，QQ 客户端会自己每两分钟重试一次登录，试三十多分钟；
这本身在风控眼里就是"反复敲门"。停下只是等人扫码，继续试的代价是下一次冻得更久。

判据都是数出来的，不猜：
- OneBot 的端口在听（`127.0.0.1:3000`）= 登录好了，计数清零、提示清掉；
- 否则数 `docker logs` 里**最后一次登录成功之后**的 `Login Error` 行数，到上限就
  `docker stop`，往 `<data>/alerts.json` 写一条给网页看的提示。

用法（systemd 跑，见 `napcat-guard.service`）：root，因为要调 docker。
    ALERT_FILE=/opt/rin/data/alerts.json python3 /opt/napcat/guard.py
"""

from __future__ import annotations

import json
import os
import socket
import subprocess
import time

CONTAINER = os.environ.get("NAPCAT_CONTAINER", "napcat")
ALERT_FILE = os.environ.get("ALERT_FILE", "/opt/rin/data/alerts.json")
LIMIT = int(os.environ.get("NAPCAT_FAIL_LIMIT", "10") or 10)
INTERVAL = float(os.environ.get("NAPCAT_GUARD_INTERVAL", "60") or 60)
LOOKBACK = int(os.environ.get("NAPCAT_LOOKBACK_MIN", "60") or 60)
PORT = int(os.environ.get("NAPCAT_ONEBOT_PORT", "3000") or 3000)

# 登录成功的信号：OneBot 的 HTTP 服务起了；老版本日志里是「登录成功」。
SUCCESS_MARKERS = ("[HTTP Server Adapter] Start On", "登录成功")
FAIL_MARKER = "Login Error"


def port_open(host: str = "127.0.0.1", port: int = PORT, timeout: float = 2.0) -> bool:
    """OneBot 的端口在听 = 它登录好了（HTTP 服务要等登录之后才起）。"""
    with socket.socket() as sock:
        sock.settimeout(timeout)
        return sock.connect_ex((host, int(port))) == 0


def login_errors_since_success(text: str) -> int:
    """最后一次登录成功之后有几条 Login Error：成功之前的是上一轮的账，不算。"""
    lines = str(text or "").splitlines()
    last_ok = -1
    for index, line in enumerate(lines):
        if any(marker in line for marker in SUCCESS_MARKERS):
            last_ok = index
    return sum(1 for line in lines[last_ok + 1:] if FAIL_MARKER in line)


def read_logs() -> str:
    try:
        done = subprocess.run(["docker", "logs", "--since", "%dm" % LOOKBACK, CONTAINER],
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=60, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", "replace")


def alert_file() -> str:
    return ALERT_FILE


def write_alert(text: str, kind: str = "qq_login_failed") -> None:
    path = alert_file()
    folder = os.path.dirname(path)
    if folder:
        os.makedirs(folder, exist_ok=True)
    payload = {"kind": kind, "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "text": text}
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.chmod(temp, 0o644)               # 服务用户要读得到
    os.replace(temp, path)


def clear_alert() -> None:
    try:
        os.remove(alert_file())
    except FileNotFoundError:
        pass


def container_running() -> bool:
    try:
        done = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", CONTAINER],
                              stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                              timeout=20, check=False)
    except (OSError, subprocess.SubprocessError):
        return False
    return done.stdout.decode("utf-8", "replace").strip() == "true"


def stop_container() -> None:
    subprocess.run(["docker", "stop", CONTAINER], stdout=subprocess.DEVNULL,
                   stderr=subprocess.DEVNULL, timeout=180, check=False)


def say(text: str) -> None:
    print(time.strftime("%Y-%m-%d %H:%M:%S"), text, flush=True)


def main() -> int:
    say("守护起来了：连着 %d 次登录失败就停下 %s" % (LIMIT, CONTAINER))
    while True:
        try:
            if port_open():
                if os.path.exists(alert_file()):
                    clear_alert()
                    say("通道回来了，提示清掉")
            elif container_running():
                count = login_errors_since_success(read_logs())
                if count >= LIMIT:
                    stop_container()
                    write_alert("QQ 登不上：NapCat 连着 %d 次登录失败，我把它停了。"
                                "多半是被冻结或风控——解锁后 docker start napcat 再扫码。"
                                % count)
                    say("连着 %d 次失败，已停下 %s" % (count, CONTAINER))
        except Exception as exc:        # 守护自己不该把这条路带塌
            say("这一轮出错（继续）：%r" % (exc,))
        time.sleep(INTERVAL)


if __name__ == "__main__":
    raise SystemExit(main())
