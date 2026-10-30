#!/usr/bin/env python3
"""NapCat 登不上就别再敲门：连续失败到 2 次就停下容器，并在网页顶上挂一条。

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
LIMIT = int(os.environ.get("NAPCAT_FAIL_LIMIT", "2") or 2)
INTERVAL = float(os.environ.get("NAPCAT_GUARD_INTERVAL", "600") or 600)
# 容器在跑、端口没开（正在登录/正在失败）时改看这么勤：读日志是本机的事，
# 不敲门；这么看才能真的「两次就停」，而不是等 NapCat 自己试够五次。
BUSY_INTERVAL = float(os.environ.get("NAPCAT_GUARD_BUSY_INTERVAL", "30") or 30)
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


def container_started_at() -> str:
    """容器这一次是什么时候起来的（RFC3339）；拿不到就空串。"""
    try:
        done = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.StartedAt}}", CONTAINER],
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, timeout=30, check=False)
    except (OSError, subprocess.SubprocessError):
        return ""
    return done.stdout.decode("utf-8", "replace").strip()


def read_logs() -> str:
    """看这一次启动之后的日志。

    容器停了再起，上一轮的错误不该算进这一轮：不然「重起 NapCat」刚起来就被
    判两次失败，十秒内又被停下（2026-10-30 15:13 撞见过，二维码还没生出来）。
    """
    since = container_started_at() or ("%dm" % LOOKBACK)
    try:
        done = subprocess.run(["docker", "logs", "--since", since, CONTAINER],
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
        # 只有「通道好的」时候才歇十分钟；其余情况（正在登录、正在失败、容器停着
        # 等人点「重起」）都看勤一点——读日志、inspect 都是本机的事，不敲门。
        # 以前默认是 600 秒，于是有人手动重起之后，守护要睡够十分钟才发现容器在跑。
        wait = BUSY_INTERVAL
        try:
            if port_open():
                wait = INTERVAL
                if os.path.exists(alert_file()):
                    clear_alert()
                    say("通道回来了，提示清掉")
            elif container_running():
                count = login_errors_since_success(read_logs())
                if count >= LIMIT:
                    stop_container()
                    write_alert("QQ 登不上：NapCat 连着 %d 次登录失败，我把它停了。"
                                "多半是被冻结或风控——网页「设置 → QQ」点「重起 NapCat」"
                                "换一张二维码，扫一下就行。"
                                % count)
                    say("连着 %d 次失败，已停下 %s" % (count, CONTAINER))
        except Exception as exc:        # 守护自己不该把这条路带塌
            say("这一轮出错（继续）：%r" % (exc,))
        time.sleep(wait)


if __name__ == "__main__":
    raise SystemExit(main())
