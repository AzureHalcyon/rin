"""可以在线改的旋钮。

这些参数原来是命令行给的，改一次要登服务器改 systemd 单元。放进
`<data>/settings.json` 之后，网页（手机也行）就能改，改完点重启。

★ 提示词不走这里：它们在 `<data>/prompts.json`，`prompt_files` 每次调用都重读，
  改完立刻生效，不用重启。
★ 这里只存**改动过的**键；没存的就是代码里的默认值，升级时默认值变了它也跟得上。
★ 每次改动往 `<data>/settings.log` 里记一行（谁、什么时候、从什么改成什么），
  界面上的「改动记录」就是读它。
"""

from __future__ import annotations

import io
import json
import os
import time

# 组名按这个顺序显示；账目的分组借用 App 那边的说法。
GROUPS = ("模型", "接口", "上下文", "记忆", "命令", "成本", "节奏",
          "B 站", "QQ", "提示词", "改动记录")

# 网页表单就按这张表画。加一个旋钮 = 在这里加一行 + `LoopConfig` 里加字段。
# 空着是有意义的（空 = 跟主模型走、空 = 用凭证文件）就写 `"optional": True`：
# 页面是**整张表一起存**的，漏一个就会让每次保存都 400（`persona_model` 踩过）。
FIELDS = (
    {"key": "name", "group": "模型", "label": "名字", "type": "text",
     "help": "sink 第一句里的名字；改完重启生效"},
    {"key": "user_name", "group": "模型", "label": "对方的名字", "type": "text",
     "help": "压缩文本里用「名字：」代替「对方：」，sink 第一句也用它"},
    {"key": "model", "group": "模型", "label": "主模型", "type": "text",
     "picker": "chat", "help": "说话用的模型"},
    {"key": "summary_model", "group": "模型", "label": "摘要模型", "type": "text",
     "picker": "summary", "help": "压旧块用的模型，可以比主模型便宜"},
    {"key": "persona_model", "group": "模型", "label": "整理用的模型", "type": "text",
     "optional": True,
     "help": "写自我描述那次整理用哪个模型；空 = 跟主模型走（填便宜的就省主模型额度）"},
    {"key": "summary_provider", "group": "模型", "label": "摘要走哪家", "type": "select",
     "options": ["auto", "deepseek", "bailian"],
     "help": "auto：模型名带 qwen 走百炼，其余走主模型端点"},
    {"key": "reasoning_effort", "group": "模型", "label": "思考强度", "type": "select",
     "options": ["", "low", "high", "max"],
     "help": "空 = 端点默认；官方 V4.1 支持 low / high / max，越低想得越少越快"},
    {"key": "stream", "group": "模型", "label": "流式输出", "type": "bool",
     "help": "打开后边生成边推给网页（能看见她在打字）；网关不兼容时关掉"},
    {"key": "temperature", "group": "模型", "label": "温度", "type": "float",
     "min": 0, "max": 2, "help": "越高越飘"},
    {"key": "max_tokens", "group": "模型", "label": "单次输出上限", "type": "int",
     "min": 128, "max": 32000, "help": "一步最多说多少 token"},

    {"key": "chat_url", "group": "接口", "label": "对话地址", "type": "text",
     "optional": True, "help": "完整 URL（含 /v1）；留空 = 用凭证文件里的"},
    {"key": "chat_key", "group": "接口", "label": "对话密钥", "type": "text",
     "secret": True, "help": "留空 = 不改（只显示是否已设置）"},
    {"key": "embed_url", "group": "接口", "label": "嵌入地址", "type": "text",
     "optional": True, "help": "记忆取向量用的地址（含 /v1）；留空 = 用凭证文件里的"},
    {"key": "embed_key", "group": "接口", "label": "嵌入密钥", "type": "text",
     "secret": True, "help": "留空 = 不改"},

    {"key": "block_tokens", "group": "上下文", "label": "块大小", "type": "int",
     "min": 1000, "max": 400000,
     "help": "当前块攒到这么多 token 就升成上一块；块小压得勤、旧事忘得快"},
    {"key": "context_budget", "group": "上下文", "label": "上下文预算", "type": "int",
     "min": 8000, "max": 1000000,
     "help": "一次请求最多多少 token；超了先丢最旧的段"},
    {"key": "band_budget", "group": "上下文", "label": "旧段预算", "type": "int",
     "min": 2000, "max": 200000, "help": "四段摘要合计的上限"},
    {"key": "band_chars", "group": "上下文", "label": "旧段长度", "type": "ints",
     "count": [1, 4], "min": 200, "max": 40000,
     "help": "旧摘要那一层的长度上限：只启用一档就给一个数（比如 9600）；"
             "给四个数就是原来那套多档，越靠后越接近"},
    {"key": "band_decay", "group": "上下文", "label": "逐档收缩比", "type": "float",
     "min": 0.3, "max": 1.0, "help": "往老一格至少缩到这个比例"},
    {"key": "tool_fold_keep", "group": "上下文", "label": "工具结果保留条数",
     "type": "int", "min": 0, "max": 20,
     "help": "每块留最近几条超长工具结果原文，更早的折成一行；0=不折"},

    {"key": "recent_steps", "group": "记忆", "label": "最近几步不入召回", "type": "int",
     "min": 0, "max": 1000, "help": "0=关；开着可以让刚发生的事只出现在原文里"},
    {"key": "lexical", "group": "记忆", "label": "词面检索", "type": "bool",
     "help": "打开后 recall 除了嵌入还会按字面匹配"},
    {"key": "embed_model", "group": "记忆", "label": "嵌入模型", "type": "text",
     "picker": "embed", "help": "recall / remember 取向量用的模型；换模型同名要连维度一起换"},
    {"key": "embed_dim", "group": "记忆", "label": "嵌入维度", "type": "int",
     "min": 256, "max": 4096,
     "help": "要和模型匹配（qwen3.7-text-embedding 是 1024）；改了旧的向量缓存会作废重取"},

    {"key": "run_enabled", "group": "命令", "label": "命令工具", "type": "bool",
     "help": "打开后模型能跑白名单里的命令（宿主上，工作目录 <数据>/workspace）；默认关"},
    {"key": "run_sandbox", "group": "命令", "label": "关进沙箱", "type": "bool",
     "help": "用 Landlock 把它关在「只有工作区能写」里；关掉等于把整台机器交给它，默认开"},
    {"key": "run_file_mb", "group": "命令", "label": "单文件上限（MB）", "type": "int",
     "min": 1, "max": 2048, "help": "工作区里一个文件最大多少 MB，防止把盘写满"},
    {"key": "run_timeout", "group": "命令", "label": "命令超时（秒）", "type": "float",
     "min": 1, "max": 300, "help": "超时就杀了，连它自己起的进程一起"},
    {"key": "run_output_chars", "group": "命令", "label": "命令输出上限", "type": "int",
     "min": 200, "max": 20000, "help": "超了留头 60% 和尾 40%，中间标出省了多少字"},
    {"key": "write_enabled", "group": "命令", "label": "写入工具", "type": "bool",
     "help": "打开后模型能往工作区（<数据>/workspace）写文件；绝对路径和 .. 一律拒"},
    {"key": "write_max_chars", "group": "命令", "label": "写入上限（字）", "type": "int",
     "min": 1000, "max": 1000000, "help": "单次最多写多少字，超了让模型分几次写"},

    {"key": "daily_token_cap", "group": "成本", "label": "每天 token 上限",
     "type": "int", "min": 0, "max": 100000000,
     "help": "到了就等到第二天零点，期间消息照收；0=不限"},
    {"key": "daily_token_warn", "group": "成本", "label": "每天提醒线",
     "type": "int", "min": 0, "max": 100000000, "help": "过了只在输出里提一句；0=不提"},

    {"key": "persona_enabled", "group": "节奏", "label": "自我描述", "type": "bool",
     "help": "让她自己写一段自我描述，常驻在 system 里；平时不提，隔一段时间自动重写一次"},
    {"key": "persona_hours", "group": "节奏", "label": "自我描述重写间隔（小时）", "type": "float",
     "min": 0, "max": 720, "help": "隔多久重写一次；0 = 不自动重写。改一次会作废一次前缀缓存"},
    {"key": "persona_min_chars", "group": "节奏", "label": "重写所需新材料（字）", "type": "int",
     "min": 0, "max": 200000,
     "help": "上次改写之后至少要攒这么多字才重写；几句话不够，要一段像样的新经历（0 = 到点就写）"},
    {"key": "persona_feed_chars", "group": "节奏", "label": "整理时喂多少字", "type": "int",
     "min": 0, "max": 400000,
     "help": "一次整理最多喂多少字（旧段摘要 + 上次改写之后的新记录）；0 = 全部喂"},
    {"key": "persona_with_bands", "group": "节奏", "label": "整理时带上旧段摘要", "type": "bool",
     "help": "旧段摘要一起喂，长程连续性更好；关掉就只看上次改写之后的原文"},
    {"key": "persona_chars", "group": "节奏", "label": "自我描述上限（字）", "type": "int",
     "min": 200, "max": 8000, "help": "这段描述最多多少字，超了截断"},
    {"key": "clock_refresh", "group": "节奏", "label": "墙钟刷新（秒）", "type": "int",
     "min": 0, "max": 3600,
     "help": "有人说话、刚醒来一定给；其余步至少隔这么久才刷新，中间那些步只写「接着上面」；0=每步都给"},
    {"key": "watchdog_idle", "group": "节奏", "label": "心跳步数", "type": "int",
     "min": 0, "max": 100,
     "help": "连续几步没有外界动静就问一句「还在做事吗」；0=不问"},
    {"key": "retry_delay", "group": "节奏", "label": "重试起点（秒）", "type": "float",
     "min": 1, "max": 600, "help": "模型调不通时先等这么久，之后翻倍"},

    {"key": "bili_sessdata", "group": "B 站", "label": "登录 cookie（SESSDATA）",
     "type": "text", "secret": True,
     "help": "登录后 cookie 里 SESSDATA 那段（裸值）；有它才拿得到多数字幕轨"},
    {"key": "bili_ffmpeg", "group": "B 站", "label": "ffmpeg 路径", "type": "text",
     "optional": True, "help": "转写时抽音轨用；空 = 用 PATH 里的 ffmpeg"},
    {"key": "bili_asr_model", "group": "B 站", "label": "转写模型", "type": "text",
     "optional": True, "help": "没字幕时用哪个模型转；空 = qwen-audio-3.1-asr-flash"},

    {"key": "qq_base", "group": "QQ", "label": "NapCat 地址", "type": "text",
     "optional": True, "help": "qq 工具调 OneBot 的地址；空 = http://127.0.0.1:3000"},
    {"key": "qq_token", "group": "QQ", "label": "NapCat 令牌", "type": "text",
     "secret": True, "help": "留空 = 不改；空着就用环境里的 NAPCAT_TOKEN"},
)

_KEYS = [field["key"] for field in FIELDS]
_BY_KEY = {field["key"]: field for field in FIELDS}
_SECRET_KEYS = tuple(field["key"] for field in FIELDS if field.get("secret"))


class SettingsError(ValueError):
    """值不合法。"""


def path_of(data_dir: str) -> str:
    return os.path.join(str(data_dir or "."), "settings.json")


def log_path(data_dir: str) -> str:
    return os.path.join(str(data_dir or "."), "settings.log")


def defaults() -> dict:
    """代码里的默认值：界面上要标「改过没有」，也要能一键恢复。"""
    from loop.config import LoopConfig
    fresh = LoopConfig()
    out = {}
    for field in FIELDS:
        value = getattr(fresh, field["key"], None)
        out[field["key"]] = list(value or []) if field["type"] == "ints" else value
    return out


def load(data_dir: str) -> dict:
    """读改动过的键；文件没有、坏了都当「全默认」。"""
    try:
        with io.open(path_of(data_dir), encoding="utf-8") as handle:
            data = json.load(handle) or {}
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {key: value for key, value in data.items() if key in _KEYS}


def _count_range(field: dict) -> tuple:
    """`count` 允许两种写法：一个数（必须正好这么多），或一对数（下限–上限）。"""
    count = field.get("count", 1)
    if isinstance(count, (list, tuple)) and len(count) == 2:
        return max(1, int(count[0])), max(1, int(count[1]))
    exact = max(1, int(count or 1))
    return exact, exact


def _count_words(low: int, high: int) -> str:
    return "%d" % low if low == high else "%d–%d" % (low, high)


def _coerce(field: dict, value):
    kind = field["type"]
    if kind == "bool":
        return bool(value)
    if kind == "select":
        value = str(value or "").strip()
        options = list(field.get("options") or ())
        if value not in options:
            raise SettingsError("%s 只能是 %s" % (field["label"], " / ".join(options)))
        return value
    if kind == "ints":
        # count 可以是一个数（必须正好这么多），也可以是一对数（1–4 这种范围）：
        # 旧段那层现在默认只启用一档，写死"必须 4 个"会把整页保存拒掉。
        low, high = _count_range(field)
        if not isinstance(value, (list, tuple)):
            raise SettingsError("%s 要给 %s 个数" % (field["label"], _count_words(low, high)))
        numbers = []
        for item in list(value)[:high]:
            try:
                number = int(item)
            except (TypeError, ValueError):
                raise SettingsError("%s 只能是整数" % field["label"]) from None
            numbers.append(number)
        if len(numbers) < low:
            raise SettingsError("%s 要给 %s 个数" % (field["label"], _count_words(low, high)))
        value = numbers
    elif kind == "int":
        try:
            value = int(value)
        except (TypeError, ValueError):
            raise SettingsError("%s 只能是整数" % field["label"]) from None
    elif kind == "float":
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise SettingsError("%s 只能是数字" % field["label"]) from None
    else:
        value = str(value or "").strip()
        if not value and not field.get("optional"):
            raise SettingsError("%s 不能空着" % field["label"])
    low = field.get("min")
    high = field.get("max")
    for number in (value if isinstance(value, list) else [value]):
        if isinstance(number, bool) or not isinstance(number, (int, float)):
            continue
        if low is not None and number < low:
            raise SettingsError("%s 不能小于 %s" % (field["label"], low))
        if high is not None and number > high:
            raise SettingsError("%s 不能大于 %s" % (field["label"], high))
    return value


def save(data_dir: str, values: dict, who: str = "") -> dict:
    """合并进现有文件并落盘；改动记进日志。返回最终存下的那份。"""
    current = load(data_dir)
    base = defaults()
    merged = dict(current)
    changes = []
    for key, value in dict(values or {}).items():
        if key not in _BY_KEY:
            continue
        if _BY_KEY[key].get("secret") and not str(value or "").strip():
            continue                    # 密钥留空就是"不改"，别把已存的清掉
        new = _coerce(_BY_KEY[key], value)
        old = current.get(key, base.get(key))
        if new != old:
            changes.append({"key": key, "from": old, "to": new})
        merged[key] = new
    target = path_of(data_dir)
    os.makedirs(os.path.dirname(os.path.abspath(target)), exist_ok=True)
    with io.open(target, "w", encoding="utf-8") as handle:
        json.dump(merged, handle, ensure_ascii=False, indent=2)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    if changes:
        _append_log(data_dir, changes, who)
    return merged


def _append_log(data_dir: str, changes: list, who: str) -> None:
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with io.open(log_path(data_dir), "a", encoding="utf-8") as handle:
            for item in changes:
                handle.write(json.dumps(
                    {"ts": stamp, "who": who or "网页",
                     "key": item["key"],
                     "label": _BY_KEY[item["key"]]["label"],
                     "from": item["from"], "to": item["to"]},
                    ensure_ascii=False) + "\n")
        os.chmod(log_path(data_dir), 0o600)
    except OSError:
        pass


def history(data_dir: str, limit: int = 40) -> list:
    """最近的改动，新的在前。"""
    try:
        with io.open(log_path(data_dir), encoding="utf-8") as handle:
            lines = handle.readlines()
    except OSError:
        return []
    out = []
    for line in lines[-max(1, int(limit)):]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    out.reverse()
    return out


# 存在 settings.json、不进 LoopConfig 的键：接口凭据 + 两个工具的凭据。
CHANNEL_KEYS = ("chat_url", "chat_key", "embed_url", "embed_key",
                "bili_sessdata", "bili_ffmpeg", "bili_asr_model",
                "qq_base", "qq_token")


def embed_base_of(value) -> str:
    """嵌入地址归一：`…/v1/chat/completions`、`…/v1/embeddings` 都收成 `…/v1`。"""
    url = str(value or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/embeddings"):
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


def hint_of(value: str) -> str:
    """密钥只露出"已设置"和尾巴几位。"""
    text = str(value or "")
    if not text:
        return ""
    return "已设置（%s，%d 位）" % (text[-4:] if len(text) > 8 else "**", len(text))


def channel(values: dict) -> dict:
    """四个渠道键的最终值：设置里写了就用，没写就是空（由凭证文件兜底）。"""
    values = dict(values or {})
    out = {}
    for key in CHANNEL_KEYS:
        out[key] = str(values.get(key) or "").strip()
    return out


def mask(values: dict) -> dict:
    """给网页看的：密钥换成空串（前端显示 hint）。"""
    out = dict(values or {})
    for key in CHANNEL_KEYS:
        if key.endswith("_key") and out.get(key):
            out[key] = ""
    return out


def hints(data_dir: str) -> dict:
    """密钥字段的提示：只说明"已设置"和末尾几位。"""
    stored = load(data_dir)
    out = {}
    for key in CHANNEL_KEYS:
        if key.endswith("_key"):
            hint = hint_of(str(stored.get(key) or ""))
            if hint:
                out[key] = hint
    return out


def apply_credentials(credentials, values: dict) -> None:
    """把设置里的渠道值盖到凭证对象上；没写的保持凭证文件里的原值。"""
    if credentials is None:
        return
    from loop.config import chat_url_of

    current = channel(values)
    if current["chat_url"]:
        credentials.chat_url = chat_url_of(current["chat_url"])
    if current["chat_key"]:
        credentials.chat_key = current["chat_key"]
    # 摘要与嵌入共用一百炼端点：设置里写的就是它的地址/密钥
    if current["embed_url"]:
        credentials.summary_url = embed_base_of(current["embed_url"])
    if current["embed_key"]:
        credentials.summary_key = current["embed_key"]


def apply(config, values: dict) -> None:
    """把存下的值盖到运行时配置上；没存的键保持代码默认。"""
    for key, value in dict(values or {}).items():
        if key in _KEYS and hasattr(config, key):
            setattr(config, key, value)


def effective(config) -> dict:
    """界面上要显示的当前值；密钥只给掩码，不给明文。"""
    stored = load(getattr(config, "data_dir", "") or "")
    out = {}
    for field in FIELDS:
        key = field["key"]
        if key in CHANNEL_KEYS:
            out[key] = "" if field.get("secret") else str(stored.get(key) or "")
            continue
        value = getattr(config, key, None)
        out[key] = list(value or []) if field["type"] == "ints" else value
    return out
