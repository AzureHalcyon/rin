"""Landlock 包装器：把一条命令关进"只能写工作区"的笼子里再执行。

用法（`run` 工具就是这么调它的）：

    python landlock.py --workspace <dir> -- sh -c "命令"

它只做一件事：**把"写"这类访问交给 Landlock 管，然后只给工作区开权限**。
读和执行不接管——整台机器照常读、照常执行；写的动作（建/删/改/截断/改名/建符号链接）
在工作区之外一律 EACCES。网络不属于 Landlock 的文件词表，不受影响。

★ 适用范围：Linux 上以当前用户身份跑的子进程。容器/microVM/远端执行器不在此列。
★ fail-closed：内核不支持 Landlock 就退出并说明，绝不"偷偷不加限制地跑"。

内核接口（landlock(7)）三个 syscall：`landlock_create_ruleset`（x86_64 号 444）、
`landlock_add_rule`（445）、`landlock_restrict_self`（446）。用 ctypes 直接调，
不依赖任何 Python 包，也不需要 root——只要有 `no_new_privs`。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sys

try:                      # Windows 上没有 resource：那个上限只在 Linux 上生效
    import resource
except ImportError:       # pragma: no cover - 只为跨平台自检
    resource = None

# ── 内核常量（landlock(7) / uapi/linux/landlock.h） ─────────────────────────
SYS_CREATE_RULESET = 444
SYS_ADD_RULE = 445
SYS_RESTRICT_SELF = 446
RULE_PATH_BENEATH = 1
PR_SET_NO_NEW_PRIVS = 38

EXECUTE = 1 << 0
WRITE_FILE = 1 << 1
READ_FILE = 1 << 2
READ_DIR = 1 << 3
REMOVE_DIR = 1 << 4
REMOVE_FILE = 1 << 5
MAKE_CHAR = 1 << 6
MAKE_DIR = 1 << 7
MAKE_REG = 1 << 8
MAKE_SOCK = 1 << 9
MAKE_FIFO = 1 << 10
MAKE_BLOCK = 1 << 11
MAKE_SYM = 1 << 12
REFER = 1 << 13        # ABI 2
TRUNCATE = 1 << 14     # ABI 3

# 只接管"写"这一类：读与执行不受限，所以在任何地方都能看源码、跑程序。
WRITE_RIGHTS = (WRITE_FILE | REMOVE_DIR | REMOVE_FILE | MAKE_CHAR | MAKE_DIR
                | MAKE_REG | MAKE_SOCK | MAKE_FIFO | MAKE_BLOCK | MAKE_SYM
                | REFER | TRUNCATE)


class LandlockError(RuntimeError):
    """内核不支持，或者规则没装上。"""


class _RulesetAttr(ctypes.Structure):
    _fields_ = [("handled_access_fs", ctypes.c_uint64)]


class _PathBeneathAttr(ctypes.Structure):
    _fields_ = [("allowed_access", ctypes.c_uint64),
                ("parent_fd", ctypes.c_int32),
                ("_pad", ctypes.c_int32)]


def _libc():
    return ctypes.CDLL(None, use_errno=True)


def abi_version() -> int:
    """内核支持的 Landlock ABI 版本；不支持就是 0。"""
    libc = _libc()
    try:
        value = libc.syscall(SYS_CREATE_RULESET, None, ctypes.c_size_t(0),
                             ctypes.c_uint32(1))       # 1 = VERSION
    except AttributeError:
        return 0
    return int(value) if int(value) > 0 else 0


def confine(workspace: str, rights: int = WRITE_RIGHTS) -> None:
    """给当前进程装上 Landlock 规则：只允许在 workspace 下做 rights 那些事。"""
    version = abi_version()
    if version < 1:
        raise LandlockError("这个内核没有 Landlock（abi=0）")
    # 老 ABI 没有 REFER / TRUNCATE：把它们从要管的位里去掉，别让 add_rule 报 EINVAL
    if version < 2:
        rights &= ~REFER
    if version < 3:
        rights &= ~TRUNCATE

    libc = _libc()
    attr = _RulesetAttr(handled_access_fs=rights)
    ruleset = libc.syscall(SYS_CREATE_RULESET, ctypes.byref(attr),
                           ctypes.c_size_t(ctypes.sizeof(attr)),
                           ctypes.c_uint32(0))
    if ruleset < 0:
        raise LandlockError("landlock_create_ruleset 失败：%s"
                            % os.strerror(ctypes.get_errno()))
    try:
        folder = os.path.abspath(workspace)
        os.makedirs(folder, exist_ok=True)
        fd = os.open(folder, os.O_PATH | os.O_CLOEXEC)
        try:
            path = _PathBeneathAttr(allowed_access=rights, parent_fd=fd)
            if libc.syscall(SYS_ADD_RULE, ctypes.c_int(ruleset),
                            ctypes.c_int(RULE_PATH_BENEATH),
                            ctypes.byref(path), ctypes.c_uint32(0)) < 0:
                raise LandlockError("landlock_add_rule 失败：%s"
                                    % os.strerror(ctypes.get_errno()))
        finally:
            os.close(fd)
        if libc.prctl(PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
            raise LandlockError("prctl(NO_NEW_PRIVS) 失败")
        if libc.syscall(SYS_RESTRICT_SELF, ctypes.c_int(ruleset),
                        ctypes.c_uint32(0)) < 0:
            raise LandlockError("landlock_restrict_self 失败：%s"
                                % os.strerror(ctypes.get_errno()))
    finally:
        os.close(ruleset)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="把一条命令关进只能写工作区的笼子里")
    parser.add_argument("--workspace", required=True, help="唯一可写的地方")
    parser.add_argument("--file-mb", type=int, default=64,
                        help="单个文件最大多少 MB（RLIMIT_FSIZE），0=不限")
    parser.add_argument("command", nargs=argparse.REMAINDER,
                        help="`--` 之后的命令")
    args = parser.parse_args(argv)
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        print("landlock: 没有要给执行的命令", file=sys.stderr)
        return 2

    workspace = os.path.abspath(args.workspace)
    try:
        confine(workspace)
    except LandlockError as exc:
        print("landlock: %s" % exc, file=sys.stderr)
        return 125                        # fail-closed：没有沙箱就不跑
    if args.file_mb and args.file_mb > 0 and resource is not None:
        limit = int(args.file_mb) * 1024 * 1024
        try:
            resource.setrlimit(resource.RLIMIT_FSIZE, (limit, limit))
        except (ValueError, OSError):
            pass
    os.environ["TMPDIR"] = os.path.join(workspace, ".tmp")
    try:
        os.makedirs(os.environ["TMPDIR"], exist_ok=True)
    except OSError:
        pass
    try:
        os.execvp(command[0], command)
    except OSError as exc:
        print("landlock: 起不来：%s" % exc, file=sys.stderr)
        return 127


if __name__ == "__main__":
    sys.exit(main())
