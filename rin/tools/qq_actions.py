"""NapCat（OneBot 11）动作清单：她从这份表里查，也能直接调（`qq` 工具）。

动作名是从**这台机器上部署的 NapCat 4.18.41** 里抓出来的，不是抄文档——所以
列在这里的都是真存在的。参数按 OneBot 11 的惯例传；拿不准就先调一次 `get_*`
看返回长什么样，再决定怎么下手。

三列：动作名、一句话、要不要当心。

`danger=True` 的意思是：改的是别人/群里的东西，或者发出去就收不回来。工具不会拦
（她要在场就该能动手），但结果里会明说这一步动了什么，记录里也留得住。

`BLOCKED` 里那几条**不给**：直接发原始报文、重启、以及能拿到账号凭据的。
"""

from __future__ import annotations

from typing import Dict, List, Tuple

# 不给的动作：要么能拿到账号，要么能把整个通道弄down，要么是内部私有
BLOCKED: Dict[str, str] = {
    "send_packet": "直接发原始报文，绕开所有封装",
    "set_restart": "会把她自己的连接断掉，重启归运维",
    "get_cookies": "能拿到登录 cookie，等于拿到账号",
    "get_csrf_token": "账号凭据",
    "get_credentials": "账号凭据",
    "set_13": "内部私有动作，不是给人用的",
}

# 分组动作表：(分组, [(动作, 一句话, 当心)])
CATALOG: List[Tuple[str, List[Tuple[str, str, bool]]]] = [
    ("消息", [
        ("send_private_msg", "私聊发一条：user_id + message", False),
        ("send_group_msg", "群里发一条：group_id + message", False),
        ("send_msg", "按 message_type/参数自动选私聊还是群", False),
        ("delete_msg", "撤回一条：message_id（两分钟内）", True),
        ("get_msg", "按 message_id 把那条取回来（含发送者）", False),
        ("get_forward_msg", "取合并转发里的内容", False),
        ("send_forward_msg", "发合并转发（消息节点数组）", False),
        ("send_group_forward_msg", "往群里发合并转发", False),
        ("send_private_forward_msg", "往私聊发合并转发", False),
        ("mark_msg_as_read", "把一条标成已读", False),
        ("mark_private_msg_as_read", "私聊标已读", False),
        ("mark_group_msg_as_read", "群标已读", False),
        ("set_msg_emoji_like", "给一条消息加表情回应", False),
        ("fetch_emoji_like", "拉一条消息的表情回应", False),
        ("get_emoji_likes", "同上（列表）", False),
        ("get_friend_msg_history", "某个好友的最近消息（对账就靠它）", False),
        ("get_group_msg_history", "某个群的最近消息", False),
        ("send_like", "给好友点赞：user_id + times", False),
        ("send_poke", "戳一戳（通用）", False),
        ("friend_poke", "戳好友", False),
        ("group_poke", "戳群友", False),
        ("get_recent_contact", "最近联系过的人", False),
        ("get_ai_characters", "群 AI 语音的角色列表", False),
        ("get_ai_record", "取一段群 AI 语音", False),
        ("send_group_ai_record", "在群里发 AI 语音", False),
        ("send_ark_share", "发卡片（ark）", False),
        ("send_group_ark_share", "群里发卡片", False),
        ("get_mini_app_ark", "小程序卡片", False),
        ("send_flash_msg", "发闪照", True),
        ("send_online_file", "发在线文件", False),
        ("send_online_folder", "发在线文件夹", False),
        ("cancel_online_file", "取消在线文件", False),
        ("get_online_file_msg", "查在线文件消息", False),
    ]),
    ("自己的资料", [
        ("set_qq_profile", "改昵称 / 签名 / 个人说明", True),
        ("set_qq_avatar", "换头像（file 传本地或 URL）", True),
        ("set_self_longnick", "自定义长昵称", True),
        ("set_online_status", "在线状态（在线/离开/隐身/忙碌…）", True),
        ("set_diy_online_status", "自定义状态文案", True),
        ("set_input_status", "让对方看见「正在输入」", False),
    ]),
    ("好友", [
        ("get_friend_list", "好友列表（含 remark 备注、分组）", False),
        ("get_friends_with_category", "按分组列好友", False),
        ("get_stranger_info", "陌生人或好友的详细资料（昵称/备注/生日/家乡）", False),
        ("get_unidirectional_friend_list", "单向好友（我加了他、他没加我）", False),
        ("set_friend_remark", "改好友备注：user_id + remark", True),
        ("delete_friend", "删好友", True),
        ("set_friend_add_request", "处理别人加你的请求（同意/拒绝）", True),
        ("set_doubt_friends_add_request", "处理「可能是认识的人」", True),
        ("get_doubt_friends_add_request", "看有哪些「可能是认识的人」", False),
    ]),
    ("群", [
        ("get_group_list", "我加过的群", False),
        ("get_group_info", "群资料（名字、人数、群主）", False),
        ("get_group_info_e", "群资料（扩展版）", False),
        ("get_group_detail_info", "群资料（详细）", False),
        ("get_group_member_info", "某个群成员的信息", False),
        ("get_group_member_list", "群成员列表", False),
        ("set_group_card", "改群名片（自己或别人）", True),
        ("set_group_name", "改群名", True),
        ("set_group_portrait", "改群头像", True),
        ("set_group_remark", "改群在我这边的备注", False),
        ("set_group_admin", "设/撤管理员", True),
        ("set_group_ban", "禁言某个人（可定时长）", True),
        ("set_group_whole_ban", "全员禁言 / 解开", True),
        ("set_group_kick", "踢一个人", True),
        ("set_group_kick_members", "踢一批", True),
        ("set_group_leave", "退群", True),
        ("set_group_special_title", "设专属头衔", True),
        ("set_group_sign", "群打卡", False),
        ("get_group_signed_list", "群打卡名单", False),
        ("get_group_honor_info", "群荣誉（龙王、群聊之火…）", False),
        ("get_group_shut_list", "群禁言名单", False),
        ("get_group_system_msg", "群系统消息（入群邀请、申请）", False),
        ("get_group_ignored_notifies", "被我忽略的群通知", False),
        ("get_group_ignore_add_request", "忽略的加群请求", False),
        ("get_group_at_all_remain", "还能 @ 全体几次", False),
        ("set_group_add_request", "处理加群邀请/申请（同意/拒绝）", True),
        ("set_group_add_option", "改群的加群设置", True),
        ("set_group_search", "改群是否可被搜索", True),
        ("set_group_member_invite_policy", "改群邀请策略", True),
        ("set_group_new_member_history_visibility", "新人能看多少历史", True),
        ("set_group_member_permissions", "改成员权限", True),
        ("set_group_robot_add_option", "机器人入群设置", True),
        ("set_group_todo", "建一个群待办", False),
        ("cancel_group_todo", "取消群待办", False),
    ]),
    ("群精华", [
        ("get_essence_msg_list", "群精华列表", False),
        ("set_essence_msg", "把一条设成群精华", True),
        ("delete_essence_msg", "撤掉群精华", True),
    ]),
    ("文件与图片", [
        ("upload_group_file", "往群里传文件", True),
        ("upload_private_file", "往私聊传文件", True),
        ("upload_file_stream", "流式上传", True),
        ("upload_forward_msg", "上传合并转发", False),
        ("get_group_root_files", "群文件根目录", False),
        ("get_group_files_by_folder", "群文件子目录", False),
        ("get_group_file_url", "取群文件下载地址", False),
        ("get_group_file_system_info", "群文件系统信息", False),
        ("get_fileset_id", "文件集 id", False),
        ("get_fileset_info", "文件集信息", False),
        ("delete_group_file", "删群文件", True),
        ("delete_group_folder", "删群文件夹", True),
        ("get_private_file_url", "私聊文件地址", False),
        ("get_file", "取一个文件", False),
        ("get_flash_file_list", "闪传文件列表", False),
        ("get_flash_file_url", "闪传文件地址", False),
        ("get_image", "取图片文件", False),
        ("get_record", "取语音文件", False),
        ("fetch_custom_face", "拉收藏的表情", False),
        ("fetch_custom_face_detail", "表情详情", False),
        ("delete_custom_face", "删收藏的表情", True),
        ("set_custom_face_desc", "给表情写备注", False),
        ("fetch_ptt_tet", "语音转文字（QQ 自带）", False),
    ]),
    ("群相册", [
        ("get_group_album_media_list", "群相册里的照片", False),
        ("get_qun_album_list", "群相册列表", False),
        ("upload_image_to_qun_album", "传一张到群相册", True),
        ("upload_images_to_qun_album", "传多张", True),
        ("upload_video_to_qun_album", "传视频到群相册", True),
        ("set_group_album_media_like", "给群相册里的内容点赞", True),
        ("cancel_group_album_media_like", "取消点赞", True),
        ("get_group_share_link", "群分享链接", False),
        ("get_share_link", "分享链接", False),
    ]),
    ("QQ 空间", [
        ("qzone", "空间接口总入口（参数按需要试）", False),
        ("send_qzone_msg", "发一条说说（发出去就公开可见）", True),
        ("delete_qzone_msg", "删自己的说说", True),
        ("get_profile_like", "名片被赞的记录", False),
    ]),
    ("系统", [
        ("get_login_info", "当前登录的是谁", False),
        ("get_status", "在线状态", False),
        ("get_version_info", "NapCat 版本", False),
        ("get_robot_uin_range", "机器人号段", False),
        ("get_online_clients", "在线的设备", False),
        ("get_clientkey", "客户端 key", False),
        ("get_rkey", "rkey（取图用）", False),
        ("get_rkey_server", "rkey 服务", False),
        ("get_collection_list", "收藏列表", False),
        ("get_guild_list", "频道列表", False),
        ("get_guild_service_profile", "频道资料", False),
    ]),
]

# 动作名 → (分组, 一句话, 当心)
ACTIONS: Dict[str, Tuple[str, str, bool]] = {
    name: (group, note, danger)
    for group, items in CATALOG for name, note, danger in items
}


def known(action: str) -> bool:
    return str(action or "").strip() in ACTIONS


def describe(action: str) -> Tuple[str, str, bool]:
    return ACTIONS.get(str(action or "").strip(), ("", "", False))


def render_markdown(host: str = "127.0.0.1:3000") -> str:
    """生成给她看的那份清单（部署时写进工作区）。"""
    lines = [
        "# QQ 能做什么（NapCat 4.18.41 / OneBot 11）",
        "",
        "这份清单是从这台机器上的 NapCat 里抓出来的，列着的都在。",
        "",
        "**怎么用**：`qq` 工具，两个参数——`action` 是下面的动作名，`params` 是参数对象。",
        "",
        "```",
        'qq(action="get_group_list", params={})',
        'qq(action="send_private_msg", params={"user_id": 2189761707, "message": "在"})',
        'qq(action="set_friend_remark", params={"user_id": 2189761707, "remark": "Ranchel"})',
        "```",
        "",
        "拿不准参数就先调一次 `get_*` 看返回；返回是 NapCat 的原样 JSON（`status` / `retcode` / `data`）。",
        "",
        "带 **⚠** 的动作会改别人/群里的东西，或者发出去就收不回来——做之前想清楚。",
        "",
    ]
    for group, items in CATALOG:
        lines.append("## %s" % group)
        lines.append("")
        for name, note, danger in items:
            lines.append("- `%s` %s %s" % (name, "⚠" if danger else "", note))
        lines.append("")
    lines.append("## 不给的动作")
    lines.append("")
    for name, why in BLOCKED.items():
        lines.append("- `%s`：%s" % (name, why))
    lines.append("")
    lines.append("## 做不到的三件事")
    lines.append("")
    lines.append("- **主动加好友**：只有 `set_friend_add_request`（同意别人加你），没有按号发起申请。")
    lines.append("- **主动进群**：只有 `set_group_add_request`（处理别人邀请），没有搜索并加入。")
    lines.append("- **发群公告**：这个构建里没有 `send_group_notice`。")
    lines.append("")
    lines.append("端点：`http://%s`（只在本机回环上）。" % host)
    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":          # python -m rin.tools.qq_actions > 文档
    print(render_markdown())
