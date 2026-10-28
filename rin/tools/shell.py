"""在宿主机上跑一条命令：关进 Landlock 的"只能写工作区"里，不维护程序白名单。

说明文字在 `prompts/tools/run.txt` 里，这里只管闸门和执行。

★ 边界是**内核**，不是程序名单：整台机器都能读、能执行，只有 `<data>/workspace`
  能写（建/删/改/截断/改名）。`loop/landlock.py` 装规则，装不上就 fail-closed——
  绝不"没沙箱也照跑"。
★ 命令走 `sh -c`（管道、重定向都能用），因为有沙箱兜底了。
★ 唯一剩下的一条规则：参数里点到凭证/密钥就不跑（读出来会进记录）。它不是沙箱，
  挡的是"别把密钥念进对话"。
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import List

from loop import prompt_files

NAMES = ("run",)

# 敏感文件：命令照跑，但命令里点到它就不跑。它不是沙箱——源码、数据、日志随便读，
# 只有凭证/密钥这一类不许进记录（读出来会变成"她说过密钥"，还可能被写成笔记）。
SENSITIVE_DIRS = ("/etc/palimpsest", "/etc/shadow", "/etc/sudoers", "/root/")
SENSITIVE_NAMES = ("credentials.yaml", "credentials.yml", "web-token",
                   "id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys")
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")


def _sensitive(token: str) -> str:
    """这个参数像不像敏感文件；像就返回它，不像返回空。"""
    text = str(token or "").strip().strip("\"'")
    if not text:
        return ""
    lowered = text.lower()
    for folder in SENSITIVE_DIRS:
        if folder in lowered:
            return text
    name = os.path.basename(lowered)
    if name in SENSITIVE_NAMES:
        return text
    if lowered.endswith(SENSITIVE_SUFFIXES):
        return text
    if "/.ssh/" in lowered or lowered.startswith("~/.ssh"):
        return text
    return ""


def workspace_of(config) -> str:
    """命令在哪儿跑：数据目录下的 workspace，不是仓库也不是家目录。"""
    return os.path.join(str(getattr(config, "data_dir", ".") or "."), "workspace")


def sandbox_ready() -> bool:
    """这台机器能不能装 Landlock（不能就别跑，fail-closed）。"""
    from rin.tools import landlock

    try:
        return landlock.abi_version() >= 1
    except Exception:
        return False


def tools(config=None) -> List[dict]:
    """没开就给空表：工具面里连 `run` 都不出现。"""
    if not bool(getattr(config, "run_enabled", False)):
        return []
    text = prompt_files.tool_texts()
    return [
        {"type": "function", "function": {
            "name": "run",
            "description": text["run"],
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string",
                                "description": text["run.command"]},
                },
                "required": ["command"],
            },
        }},
    ]


def call(name: str, arguments: str, config) -> str:
    if name not in NAMES:
        return "没有这个工具：%s" % name
    if not bool(getattr(config, "run_enabled", False)):
        return "命令没开：这是宿主上的能力，默认关；要开在设置里打开「命令」。"
    try:
        payload = json.loads(arguments or "{}")
    except ValueError:
        return "参数不是 JSON。"
    if not isinstance(payload, dict):
        return "参数不是对象。"
    command = str(payload.get("command") or "").strip()
    if not command:
        return "要给一条命令。"
    return execute(command, config)


def _shell_argv(command: str) -> List[str]:
    """命令怎么交给系统：POSIX 上 `sh -c`（管道、重定向都能用），Windows 上 `cmd /c`。"""
    if os.name == "nt":
        return ["cmd", "/c", command]
    return ["sh", "-c", command]


def execute(command: str, config) -> str:
    hit = _sensitive(command)
    if hit:
        return ("不给读：命令里点到了 %s 这类凭证或密钥，那类东西不进记录。"
                "源码、数据、日志随便看。" % hit)

    workdir = os.path.abspath(workspace_of(config))
    try:
        os.makedirs(workdir, exist_ok=True)
    except OSError as exc:
        return "工作目录建不出来：%s" % exc
    try:
        timeout = float(getattr(config, "run_timeout", 20.0) or 20.0)
    except (TypeError, ValueError):
        timeout = 20.0
    try:
        limit = int(getattr(config, "run_output_chars", 4000) or 4000)
    except (TypeError, ValueError):
        limit = 4000

    argv = _shell_argv(command)
    if bool(getattr(config, "run_sandbox", True)):
        if not sandbox_ready():
            return ("跑不了：这台机器装不上 Landlock 的「只能写工作区」规则，"
                    "而命令默认必须关在沙箱里。要在没沙箱的情况下跑，"
                    "得在设置里明确关掉「关进沙箱」，那是另一回事。")
        from rin.tools import landlock

        wrapper = os.path.join(os.path.dirname(os.path.abspath(landlock.__file__)),
                               "landlock.py")
        try:
            file_mb = int(getattr(config, "run_file_mb", 64) or 64)
        except (TypeError, ValueError):
            file_mb = 64
        argv = [sys.executable, wrapper, "--workspace", workdir,
                "--file-mb", str(file_mb), "--"] + argv

    started = time.time()
    try:
        proc = subprocess.Popen(  # noqa: S603 - 边界在内核（Landlock），不在这里
            argv, cwd=workdir, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8", errors="replace", start_new_session=True)
    except OSError as exc:
        return "起不来：%s" % exc

    timed_out = False
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill(proc)
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            out = ""
    cost = time.time() - started
    body = _clip(str(out or ""), limit)
    code = proc.returncode
    if code == 125 and "landlock:" in body:
        return "沙箱装不上，这条没跑：%s" % body.replace("landlock:", "").strip()
    head = ("超时 %g 秒，已经杀掉" % timeout if timed_out else "退出码 %s" % code)
    return "（%s，用时 %.1f 秒）%s%s" % (head, cost, "\n" if body else "",
                                        body or "没有输出")


def _kill(proc) -> None:
    """连它自己起的东西一起杀：超时之后不能留孤儿进程。"""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (AttributeError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


def _clip(text: str, limit: int) -> str:
    """超长输出留头又留尾：头是它说了什么，尾是最后那句结果。"""
    if limit <= 0 or len(text) <= limit:
        return text
    head = int(limit * 0.6)
    tail = max(0, limit - head)
    return "%s\n…（省略 %d 字）…\n%s" % (text[:head], len(text) - limit,
                                        text[-tail:] if tail else "")
