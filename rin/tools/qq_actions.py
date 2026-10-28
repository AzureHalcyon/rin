"""NapCat（OneBot 11）动作清单：她从这份表里查，也能直接调（`qq` 工具）。

动作名与参数都来自**这台机器上部署的 NapCat 4.18.41**（由 `build-qq-params.py`
从它的 bundle 里读出来，落在 `qq_params.json`），不是手写猜的：`personal_note`
到底是"个性签名"还是"个人说明"这种问题，以 schema 为准。

分组按「做的是同一件事」划：收发、互动、群信息、群管理……不按对象划——同一个群
既有只读查询也有踢人，混在一组里既不好找也不安全。

`danger=True`：改的是别人/群里的东西，或者发出去收不回来。工具不拦（她要在场就该
能动手），但结果里会明说这一步动了什么。

`BLOCKED` 里那几条**不给**：直接发原始报文、重启、能拿到账号凭据的。
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Tuple

_HERE = os.path.dirname(os.path.abspath(__file__))
_PARAMS_PATH = os.path.join(_HERE, "qq_params.json")

try:
    with open(_PARAMS_PATH, encoding="utf-8") as _handle:
        PARAMS: Dict[str, dict] = json.load(_handle)
except (OSError, ValueError):        # 没生成过就退化：只给动作名，不给参数
    PARAMS = {}

# 不给的动作：要么能拿到账号，要么能把整个通道弄down，要么是内部私有
BLOCKED: Dict[str, str] = {
    "send_packet": "直接发原始报文，绕开所有封装",
    "set_restart": "会把她自己的连接断掉，重启归运维",
    "get_cookies": "能拿到登录 cookie，等于拿到账号",
    "get_csrf_token": "账号凭据",
    "get_credentials": "账号凭据",
    "set_13": "内部私有动作，不是给人用的",
}

# 分组动作表：(动作, 当心)。一句话说明优先用 NapCat 自己的 summary。
# 分组名 → [(动作, danger)]
CATALOG: List[Tuple[str, List[Tuple[str, bool]]]] = [
    ("收发消息", [
        ("send_private_msg", False),
        ("send_group_msg", False),
        ("send_msg", False),
        ("delete_msg", True),
        ("get_msg", False),
        ("get_forward_msg", False),
        ("send_forward_msg", False),
        ("send_group_forward_msg", False),
        ("send_private_forward_msg", False),
        ("forward_friend_single_msg", False),
        ("forward_group_single_msg", False),
        ("mark_msg_as_read", False),
        ("mark_private_msg_as_read", False),
        ("mark_group_msg_as_read", False),
        ("get_friend_msg_history", False),
        ("get_group_msg_history", False),
        ("get_share_link", False),
        ("get_group_share_link", False),
    ]),
    ("消息互动", [
        ("set_msg_emoji_like", False),
        ("fetch_emoji_like", False),
        ("get_emoji_likes", False),
        ("send_poke", False),
        ("friend_poke", False),
        ("group_poke", False),
        ("send_like", False),
        ("get_profile_like", False),
        ("click_inline_keyboard_button", False),
    ]),
    ("卡片、语音与闪照", [
        ("send_ark_share", False),
        ("send_group_ark_share", False),
        ("get_mini_app_ark", False),
        ("send_flash_msg", True),
        ("create_flash_task", True),
        ("get_ai_characters", False),
        ("get_ai_record", False),
        ("send_group_ai_record", False),
        ("fetch_ptt_text", False),
    ]),
    ("自己的资料", [
        ("get_login_info", False),
        ("set_qq_profile", True),
        ("set_qq_avatar", True),
        ("set_self_longnick", True),
        ("set_online_status", True),
        ("set_diy_online_status", True),
        ("set_input_status", False),
    ]),
    ("好友", [
        ("get_friend_list", False),
        ("get_friends_with_category", False),
        ("get_stranger_info", False),
        ("get_unidirectional_friend_list", False),
        ("get_recent_contact", False),
        ("set_friend_remark", True),
        ("delete_friend", True),
        ("set_friend_add_request", True),
        ("get_doubt_friends_add_request", False),
        ("set_doubt_friends_add_request", True),
    ]),
    ("群信息与成员（只读）", [
        ("get_group_list", False),
        ("get_group_info", False),
        ("get_group_info_ex", False),
        ("get_group_detail_info", False),
        ("get_group_member_info", False),
        ("get_group_member_list", False),
        ("get_group_at_all_remain", False),
        ("get_group_system_msg", False),
        ("get_group_ignored_notifies", False),
        ("get_group_ignore_add_request", False),
        ("_get_group_notice", False),
        ("_get_model_show", False),
        ("nc_get_user_status", False),
    ]),
    ("群设置与成员处置", [
        ("set_group_card", True),
        ("set_group_name", True),
        ("set_group_portrait", True),
        ("set_group_remark", False),
        ("set_group_admin", True),
        ("set_group_ban", True),
        ("set_group_whole_ban", True),
        ("set_group_kick", True),
        ("set_group_kick_members", True),
        ("set_group_leave", True),
        ("set_group_special_title", True),
        ("set_group_member_permissions", True),
        ("set_group_member_invite_policy", True),
        ("set_group_new_member_history_visibility", True),
        ("set_group_add_option", True),
        ("set_group_search", True),
        ("set_group_robot_add_option", True),
        ("set_group_add_request", True),
    ]),
    ("群活动与精华", [
        ("set_group_sign", False),
        ("get_group_signed_list", False),
        ("get_group_honor_info", False),
        ("get_group_shut_list", False),
        ("set_group_todo", False),
        ("cancel_group_todo", False),
        ("complete_group_todo", False),
        ("_send_group_notice", True),
        ("_del_group_notice", True),
        ("get_essence_msg_list", False),
        ("set_essence_msg", True),
        ("delete_essence_msg", True),
    ]),
    ("文件与图片", [
        ("upload_group_file", True),
        ("upload_private_file", True),
        ("upload_file_stream", True),
        ("download_file", False),
        ("download_file_stream", False),
        ("download_file_image_stream", False),
        ("download_file_record_stream", False),
        ("download_fileset", False),
        ("test_download_stream", False),
        ("get_group_root_files", False),
        ("get_group_files_by_folder", False),
        ("get_group_file_url", False),
        ("get_group_file_system_info", False),
        ("create_group_file_folder", True),
        ("move_group_file", True),
        ("rename_group_file", True),
        ("trans_group_file", True),
        ("get_fileset_id", False),
        ("get_fileset_info", False),
        ("delete_group_file", True),
        ("delete_group_folder", True),
        ("get_private_file_url", False),
        ("get_file", False),
        ("get_flash_file_list", False),
        ("get_flash_file_url", False),
        ("get_image", False),
        ("get_record", False),
        ("send_online_file", False),
        ("send_online_folder", False),
        ("receive_online_file", False),
        ("refuse_online_file", False),
        ("cancel_online_file", False),
        ("get_online_file_msg", False),
    ]),
    ("群相册", [
        ("get_group_album_media_list", False),
        ("get_qun_album_list", False),
        ("upload_image_to_qun_album", True),
        ("upload_images_to_qun_album", True),
        ("upload_video_to_qun_album", True),
        ("set_group_album_media_like", True),
        ("cancel_group_album_media_like", True),
        ("del_group_album_media", True),
        ("do_group_album_comment", False),
    ]),
    ("表情包", [
        ("fetch_custom_face", False),
        ("fetch_custom_face_detail", False),
        ("add_custom_face", False),
        ("delete_custom_face", True),
        ("set_custom_face_desc", False),
    ]),
    ("识别与翻译", [
        ("ocr_image", False),
        ("translate_en2zh", False),
        ("check_url_safely", False),
    ]),
    ("QQ 空间", [
        ("send_qzone_msg", True),
        ("delete_qzone_msg", True),
    ]),
    ("账号与连接", [
        ("get_status", False),
        ("get_version_info", False),
        ("get_robot_uin_range", False),
        ("get_online_clients", False),
        ("get_clientkey", False),
        ("get_rkey", False),
        ("get_rkey_server", False),
        ("can_send_image", False),
        ("can_send_record", False),
    ]),
    ("频道与收藏", [
        ("get_guild_list", False),
        ("get_guild_service_profile", False),
        ("get_collection_list", False),
        ("create_collection", False),
    ]),
]

# 动作名 → (分组, 当心)
WHERE: Dict[str, Tuple[str, bool]] = {
    name: (group, danger) for group, items in CATALOG for name, danger in items
}
ACTIONS: Dict[str, Tuple[str, bool]] = dict(WHERE)


def known(action: str) -> bool:
    return str(action or "").strip() in ACTIONS


def group_of(action: str) -> str:
    return ACTIONS.get(str(action or "").strip(), ("", False))[0]


def danger_of(action: str) -> bool:
    return ACTIONS.get(str(action or "").strip(), ("", False))[1]


def summary_of(action: str) -> str:
    return str((PARAMS.get(str(action or "").strip()) or {}).get("summary") or "").strip()


def params_of(action: str) -> List[dict]:
    return list((PARAMS.get(str(action or "").strip()) or {}).get("params") or [])


def example_of(action: str) -> str:
    return str((PARAMS.get(str(action or "").strip()) or {}).get("example") or "").strip()


def groups() -> List[str]:
    return [group for group, _ in CATALOG]


def _param_line(action: str) -> str:
    """一行参数：`user_id（对方 QQ 号，必填） personal_note（个性签名）`。"""
    parts = []
    for param in params_of(action):
        mark = "" if param.get("optional") else "，必填"
        desc = str(param.get("desc") or "").strip()
        parts.append("%s（%s%s）" % (param["name"], desc or "无说明", mark))
    if parts:
        return "；".join(parts)
    example = example_of(action)
    if example:
        return " ".join(example.split())
    return ""


def describe_line(action: str) -> str:
    """一行：动作名（⚠）+ 一句话 + 参数。"""
    head = str(action) + (" ⚠" if danger_of(action) else "")
    note = summary_of(action) or "（NapCat 没给说明）"
    params = _param_line(action)
    return "- %s：%s%s" % (head, note, ("　参数：" + params) if params else "")


def index_text() -> str:
    """第一层：**只有哪些组**，以及怎么打开其中一组。"""
    lines = ["QQ 动作清单（%d 组，共 %d 个）：" % (len(CATALOG), len(ACTIONS))]
    for group, items in CATALOG:
        lines.append("- %s（%d 条）" % (group, len(items)))
    lines.append("")
    lines.append("要看某一组：qq(action=\"help\", params={\"group\": \"组名\"})；"
                 "也可以直接读工作区里的 QQ-动作/<组名>.md。")
    lines.append("带 ⚠ 的会改别人/群里的东西，或者发出去收不回来。")
    return "\n".join(lines)


def group_text(group: str) -> str:
    """第二层：某一组的动作表（带真实参数）。"""
    for name, items in CATALOG:
        if name != group:
            continue
        lines = ["%s（%d 条）：" % (name, len(items)), ""]
        for action, _danger in items:
            lines.append(describe_line(action))
        return "\n".join(lines)
    return "没有这一组：%s。现有：%s" % (group or "?", "、".join(groups()))


def render_index() -> str:
    """落到 `<数据>/workspace/QQ-动作清单.md` 的那一页。"""
    lines = [
        "# QQ 动作（NapCat 4.18.41）",
        "",
        "`qq(action=\"index\")` 看组；`qq(action=\"help\", params={\"group\": \"组名\"})` "
        "看一组；`qq(action=\"<动作名>\", params={...})` 干活。",
        "",
        "```",
        'qq(action="get_friend_list", params={})',
        'qq(action="get_group_list", params={})',
        'qq(action="send_private_msg", params={"user_id": 10000, "message": "在"})',
        "```",
        "",
        "## 组",
        "",
    ]
    for group, items in CATALOG:
        lines.append("- **%s**（%d 条）→ `QQ-动作/%s.md`" % (group, len(items), group))
    lines += ["", "## 不能调的动作", ""]
    for name, why in BLOCKED.items():
        lines.append("- `%s`：%s" % (name, why))
    lines += [
        "",
        "## 注意",
        "",
        "- **资料卡上的「个人说明」改不了**：`set_qq_profile` 只收 nickname / "
        "personal_note（个性签名）/ sex。",
        "- **主动加好友、主动进群**不行：只有同意别人发来的请求（`set_friend_add_request`、"
        "`set_group_add_request`）。",
        "",
    ]
    return "\n".join(lines)


def render_group(group: str) -> str:
    """落到 `QQ-动作/<组>.md` 的那一页（带真实参数）。"""
    for name, items in CATALOG:
        if name != group:
            continue
        lines = ["# QQ 动作 · %s" % name, "",
                 "用法：`qq(action=\"动作名\", params={...})`。", ""]
        for action, danger in items:
            lines.append(describe_line(action))
            lines.append("")
        lines += ["带 ⚠ 的动作会改别人/群里的东西，或者发出去收不回来。", ""]
        return "\n".join(lines)
    return "没有这一组：%s" % group


def render_all() -> List[Tuple[str, str]]:
    """(相对路径, 内容)：索引 + 每一组一页。"""
    out = [("QQ-动作清单.md", render_index())]
    for group, _ in CATALOG:
        out.append(("QQ-动作/%s.md" % group, render_group(group)))
    return out
