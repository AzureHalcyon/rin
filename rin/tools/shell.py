"""在宿主机上跑一条命令：关进 Landlock 的"只能写工作区"里，不维护程序白名单。

说明文字在 `prompts/tools/run.txt` 里，这里只管闸门和执行。

★ 边界是**内核**，不是程序名单：整台机器都能读、能执行，只有 `<data>/workspace`
  能写（建/删/改/截断/改名）。`loop/landlock.py` 装规则，装不上就 fail-closed——
  绝不"没沙箱也照跑"。
★ 命令走 `sh -c`（管道、重定向都能用），因为有沙箱兜底了。
★ 唯一剩下的一条规则：参数里点到凭证/密钥就不跑（读出来会进记录）。它不是沙箱，
  挡的是"别把密钥念进对话"；所以它认 glob、软链接和去掉引号后的样子，而不是只认
  字面。子进程的环境变量里也去掉 key / token / secret 这类名字（`env` 一把就能念出来）。
"""

from __future__ import annotations

import glob
import json
import os
import re
import signal
import subprocess
import sys
import time
from typing import List

from loop import prompt_files
from rin import alerts

NAMES = ("run",)

# 敏感文件：命令照跑，但命令里点到它就不跑。它不是沙箱——源码、数据、日志随便读，
# 只有凭证/密钥这一类不许进记录（读出来会变成"她说过密钥"，还可能被写成笔记）。
# 既然它不是沙箱，就别只认字面：`/etc/palimp*/cred*`、`$(echo /etc/palimpsest)`、
# 软链接过去这些都要算进来（见 `_sensitive`），碰上了留一条提示给人看。
SENSITIVE_DIRS = ("/etc/palimpsest", "/etc/shadow", "/etc/sudoers", "/root/")
SENSITIVE_NAMES = ("credentials.yaml", "credentials.yml", "web-token",
                   "id_rsa", "id_ed25519", "id_ecdsa", "authorized_keys")
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
# 子进程的环境里不带这一类名字的变量：`env` 一把就能把令牌念出来。
_SECRET_ENV = re.compile(r"(?i)(key|token|secret|passw?d|credential)")
_SHELL_NOISE = re.compile(r"[()$`{}\[\];|&<>\"']")


def _is_secret(path: str) -> bool:
    """这个路径是不是凭证/密钥那类。"""
    lowered = str(path or "").strip().lower()
    if not lowered:
        return False
    for folder in SENSITIVE_DIRS:
        if lowered.startswith(folder):
            return True
    name = os.path.basename(lowered)
    if name in SENSITIVE_NAMES:
        return True
    if lowered.endswith(SENSITIVE_SUFFIXES):
        return True
    if "/.ssh/" in lowered or lowered.startswith("~/.ssh"):
        return True
    if "/proc/" in lowered and lowered.rstrip("/").endswith("/environ"):
        return True
    return False


def _shapes(text: str) -> List[str]:
    """一个词可能指向哪些真实路径：原样、去掉 shell 装饰的、glob 展开的每一个。"""
    plain = _SHELL_NOISE.sub("", str(text or "")).strip()
    shapes = [str(text or "").strip(), plain]
    try:
        shapes.extend(glob.glob(os.path.expanduser(plain))[:50])
    except (OSError, re.error):
        pass
    try:
        shapes.append(os.path.realpath(os.path.expanduser(plain)))
    except OSError:
        pass
    return [item for item in shapes if item]


def _word_hits(word: str) -> str:
    for shape in _shapes(word):
        if _is_secret(shape):
            return word.strip().strip("\"'")
    return ""


def _sensitive(token: str) -> str:
    """命令里有没有点到敏感文件；有就返回那个词，没有返回空。

    先整条扫一遍字面（`cat /etc/palimpsest/credentials.yaml` 这类最直白的），
    再把每个词按 glob / 软链接 / 去掉引号后的样子展开一遍。
    """
    body = str(token or "")
    lowered = body.lower()
    for folder in SENSITIVE_DIRS:
        if folder in lowered:
            return folder
    for name in SENSITIVE_NAMES:
        if name in lowered:
            return name
    for suffix in SENSITIVE_SUFFIXES:
        if suffix in lowered:
            return suffix
    if "/.ssh/" in lowered:
        return "/.ssh/"
    for word in body.replace("\n", " ").split():
        hit = _word_hits(word)
        if hit:
            return hit
    return ""


def _child_env() -> dict:
    """给子进程的环境：把带 key / token / secret / password 的字眼去掉。

    她不缺这几个变量；而 `env` 或 `/proc/self/environ` 一把就能把通道令牌念进记录，
    再顺手发出去。凭证文件本身有权限挡着，环境变量没有。
    """
    env = {}
    for name, value in os.environ.items():
        if _SECRET_ENV.search(str(name)):
            continue
        env[name] = value
    return env


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


# ── 这台机器的说明：现写一份进工作区，她自己查 ─────────────────────────────
DOC_NAME = "这台机器.md"
MIRROR = "https://mirrors.cloud.tencent.com/pypi/simple"


def doc_path(config) -> str:
    return os.path.join(workspace_of(config), DOC_NAME)


def _version_of(module: str) -> str:
    """装没装、什么版本——问的就是服务这个解释器，不是猜。"""
    try:
        imported = __import__(module)
    except Exception:
        return ""
    return str(getattr(imported, "__version__", "") or "有")


def doc_text(config) -> str:
    workspace = os.path.abspath(workspace_of(config))
    timeout = float(getattr(config, "run_timeout", 20.0) or 20.0)
    file_mb = int(getattr(config, "run_file_mb", 64) or 64)
    limit = int(getattr(config, "run_output_chars", 4000) or 4000)
    pillow = _version_of("PIL")
    pillow_line = ("- 已经装好的库：Pillow %s，装在 **%s** 里——系统那个 `python3` 没有它；"
                   "要用就先换成这条路径的解释器。" % (pillow, sys.executable) if pillow
                   else "- 这个解释器里还没有 Pillow。")
    try:
        existing = sorted(name for name in os.listdir(workspace)
                          if not name.startswith(".") and name != DOC_NAME)
    except OSError:
        existing = []
    lines = [
        "# 这台机器",
        "",
        "每次启动现写一遍，别往这里写东西。",
        "",
        "## 能写的地方",
        "",
        "- 工作区：`%s`。它就是 `run` 的当前目录，也是 `write` 的根。" % workspace,
        "- 别处：能读、能执行、能 `cd`；写、删、改名一律 `Permission denied`。",
        "  那是内核沙箱（Landlock）划的范围，不是文件权限，`chmod` 也改不动。",
        "- `/dev/null`、`/dev/zero`、`/dev/full` 可以写：`命令 > /dev/null 2>&1` 照常用。",
        "- 临时目录指在工作区的 `.tmp`；单个文件上限 %d MB。" % file_mb,
        "",
        "## 跑命令",
        "",
        "- `run` 走 `sh -c`：一次最多 %g 秒（超了连子进程一起杀），输出只留头尾共 %d 字。"
        % (timeout, limit),
        "- `python3` 是系统那个（%s）；服务自己用的是 `%s`。"
        % (_version_of_python(), sys.executable),
        "- 装库装到工作区：`python3 -m pip install --target .libs 名字 -i %s`，" % MIRROR,
        "  用的时候 `PYTHONPATH=.libs python3 …`。默认 PyPI 从这台机器拉包要几分钟，",
        "  腾讯云镜像几秒；也别用 `--user`，系统会以 PEP 668 拒绝。",
        pillow_line,
        "",
        "## 机器与网络",
        "",
        "- Ubuntu 24.04 的云服务器，网络通，`curl` / `git` / `pip` 都在。",
        "- 不给读的：`/etc/palimpsest`、`credentials.yaml`、`*.pem`、`.ssh` 这类；",
        "  命令看到的环境里也没有令牌那类变量（`env` 里不会出现 `NAPCAT_TOKEN`）。",
        "- 这台机器上还在跑的：`palimpsest`（你自己）、`qq`（NapCat 适配器）、",
        "  `napcat` 容器（QQ 协议端，端口 3000）。",
        "",
        "## 工作区里现成的东西",
        "",
    ]
    lines.append("、".join(existing) if existing else "（空）")
    lines.append("")
    return "\n".join(lines)


def _version_of_python() -> str:
    import platform

    return platform.python_version()


def write_doc(config) -> str:
    """把说明写到工作区；写不进去就抛 OSError，让入口报一句。"""
    path = doc_path(config)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(doc_text(config))
    return path


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
        alerts.raise_alert(config, "credential_probe",
                           "`run` 里点到了 %s 这类凭证，已经拦下。" % hit)
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
            text=True, encoding="utf-8", errors="replace", start_new_session=True,
            env=_child_env())
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
