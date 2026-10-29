# 部署与回滚

这个仓是「落地」那一半。核心在 `palimpsest`，两个仓**并排铺开、不装进站点包**：
systemd 用 `PYTHONPATH` 把两个目录都指上，改代码只要重传再重启。

## 目录布局

```
/opt/rin                   一处全包：rin/ + loop/ + memory_v2/ + .venv/ + data/
/etc/palimpsest/credentials.yaml     凭证：模型与嵌入的 key / url
/etc/palimpsest/loop.env             LOOP_NAME / LOOP_HTTP / LOOP_DAILY_CAP / LOOP_DAILY_WARN
/etc/systemd/system/palimpsest.service
/etc/systemd/system/qq.service
```

数据目录（`--data`）跟着这个仓走：`/opt/rin/data`。凭证仍在 `/etc`，两边共用。
`/opt/rin` 底下还有三样**不要**被同步冲掉：`.venv`、`data`（迁移前的旧数据，留作回滚）、
`app`（跟这两个仓无关的遗留目录）；`/opt/rin/data` 同样不能被同步冲掉——所以两边的
`rsync --delete` 都要带 `--exclude data`。

⚠️ `/opt/rin/data` 是单仓时代的旧数据目录，服务现在不用它（`WorkingDirectory=/opt/rin`）。
它对服务用户**可读、可 `cd`、不可写**——模型进去一次就会撞一次 `Permission denied`，
而报错长得跟文件权限一样。有三处「删不掉 / 移不动」都踩在这里。
**已归档：`/opt/archive/palimpsest-data-20261029`**（15 MB，原样保留，不在任何 rsync 的路径里）；
`/opt/rin/data` 已经不存在了，`cd` 过去会直接报"没有这个目录"。

## 模型在这台机器上的边界

`run` 跑在宿主上，身份是服务用户 `palimpsest`，外面套一层 Landlock：

| 项 | 值 |
|---|---|
| 能写的地方 | 只有 `<data>/workspace`（建/删/改名/截断都行）；别处能读、能执行、能 `cd` |
| 别处写会怎样 | `EACCES`（`Permission denied`）——是沙箱范围，不是文件权限，`chmod` 改不动 |
| 空设备 | `/dev/null`、`/dev/zero`、`/dev/full` 可写（早期版本写不进去） |
| 凭证 | `/etc/palimpsest`、`credentials.yaml`、`*.pem`、`.ssh` 在工具层就拒；认 glob、软链接、去掉引号的说法，碰上会在网页顶上留一条 |
| 子进程环境 | 变量名里带 key / token / secret / password 的一概不传（`env` 念不出通道令牌） |
| 出站 | QQ 发消息一分钟 20 条、一天 400 条（`rin/limit.py`）：超了这条不发，并留一条提示 |
| 单条命令 | 20 秒超时（连进程组一起杀）、输出留头尾共 4000 字 |
| 单文件 | 64 MB（`RLIMIT_FSIZE`） |
| 临时目录 | `TMPDIR` 指到工作区的 `.tmp` |

启动时会在工作区写一份《这台机器.md》，把这些和「怎么装库」写在一起；`run` 的说明里
指向那份文件。**改了上面任何一项，记得同步 `rin/tools/shell.py` 的 `doc_text()`。**

装库：`python3 -m pip install --target .libs 名字 -i https://mirrors.cloud.tencent.com/pypi/simple`，
用的时候 `PYTHONPATH=.libs python3 …`。默认 PyPI 从这台机器拉包要几分钟，腾讯云镜像几秒。
服务自己的 venv 是 root 的，`sudo` 才装得进去。

机器上只有 `/opt/rin` 一处：核心仓的 `loop/`、`memory_v2/` 在部署时复制进来，
不再有 `/opt/palimpsest`（旧目录已归档到 `/opt/archive/palimpsest-repo-20261029`）。

## 机器上要装的东西

| 东西 | 干什么 | 怎么装 |
|---|---|---|
| `ffmpeg` | 视频抽音轨（`bilibili_transcript`） | `sudo apt-get install -y ffmpeg` |
| `BILI_SESSDATA` | B 站登录 cookie，字幕接口要它 | 写进 `/etc/palimpsest/loop.env`，值是裸 SESSDATA |
| 百炼的键 | 语音转写（`qwen3-asr-flash`） | 已在 `credentials.yaml` 的 `ALIYUN_MAAS_API_KEY`；入口会自动填进 `ASR_KEY` |

`loop.env` 末尾**要有换行**再追加新键，否则会和上一行拼成一行（`LOOP_HTTP=...BILI_SESSDATA=...`），
服务起来时 `--http` 拿到一个坏地址。

## 接 QQ（NapCat + 本仓的适配器）

三样东西：NapCat（协议端，Docker）、本仓的 `rin.qq`（适配器）、网关的 `/inbox`（已有）。

1. **起 NapCat**（host 网络，端口在 WebUI 里只绑 127.0.0.1，别改）：

       sudo mkdir -p /opt/napcat && cd /opt/napcat
       sudo cp /opt/rin/deploy/napcat.compose.yaml ./compose.yaml
       printf 'WEBUI_TOKEN=自己定一串\nACCOUNT=机器人QQ号\n' | sudo tee .env >/dev/null
       sudo docker compose up -d
       sudo docker compose logs --tail 20      # 看到登录二维码/提示就成了

   `ACCOUNT` 填上之后，重启容器走**快速登录**，不用每次扫码。
   用 `network_mode: host` 是因为容器里的 127.0.0.1 得是这台机器的 127.0.0.1——
   它要往适配器推事件，适配器也要连它的 HTTP 服务端；容器网络下两边都连不通。

2. **扫码登录**：SSH 隧道把 6099 转到本机，打开
   `http://127.0.0.1:6099/webui?token=<WEBUI_TOKEN>`，用**小号**扫。登完它会要求你改一次密码。

3. **配两条通道**（WebUI → 网络配置 → 新建）：

   | 方向 | 地址 | 用途 |
   |---|---|---|
   | HTTP 客户端（`httpClients`） | `http://127.0.0.1:8098/onebot` | 事件推给适配器 |
   | HTTP 服务端（`httpServers`） | 监听 `127.0.0.1:3000` | 适配器用它发消息 |

   两边都要填 **token**，值就是下面 `qq.env` 里的 `NAPCAT_TOKEN`。
   适配器对**进来的**上报宽松一档：它只监听 `127.0.0.1`，所以 NapCat 没带令牌也放行
   （各版本带法不同，`NapCat` 有些版本干脆不带）；带了但不对仍然 401，日志里会写清。
   出去的调用（发消息、拉群名）一定带令牌，值不对 NapCat 直接拒。

4. **起适配器**：

       sudo cp /opt/rin/deploy/qq.env.example /etc/palimpsest/qq.env
       sudo chmod 600 /etc/palimpsest/qq.env      # 填 RIN_TOKEN / NAPCAT_TOKEN / QQ_USERS
       sudo cp /opt/rin/deploy/qq.service /etc/systemd/system/qq.service
       sudo systemctl daemon-reload && sudo systemctl enable --now qq
       sudo journalctl -u qq -n 20 --no-pager

5. **核对**：用白名单里的号给小号发一句；网页「消息」页上应出现这条（来源 QQ、
   状态「叫过了，还没取」），记录里会出现一行「还有 1 条没看的消息…」，
   调 `messages` 能看到 `外部 · 私聊「…」 某人：…`；回话会发回那个会话。
   回发是照记录上的 `meta.via` 定向的：网页里的对话不会被复述到 QQ 上。

`RIN_TOKEN` 就是 `/opt/rin/data/web-token` 的内容（网关的 Bearer 令牌）。

### 掉线了怎么办

QQ 账号会被腾讯踢下线（日志里是 `[KickedOffLine] 你的账号当前登录已失效`）。
**掉线时 NapCat 不报错，只是再也不会有事件进来**——所以适配器每分钟问一次
`get_status`，不在线就明说一行：

```
[qq] 账号不在线：消息进不来。docker restart napcat 会重新快速登录，
     它要是还要二维码，就得扫码了
```

处理顺序：

    sudo docker restart napcat          # 会话还有效时，-q $ACCOUNT 直接快速登录
    sudo docker logs napcat --tail 20   # 还要二维码的话，日志里会给二维码路径

要扫码时把图取出来（容器里的 `/app/napcat/cache/qrcode.png` 每次刷新都会重写）：

    sudo docker cp napcat:/app/napcat/cache/qrcode.png /tmp/qr.png
    # 再从本机 scp /tmp/qr.png 出来，用手Q扫
    curl -s -X POST -H "Authorization: Bearer $NAPCAT_TOKEN" -H 'Content-Type: application/json' \
         -d '{}' http://127.0.0.1:3000/get_login_info     # 扫完核对

适配器不用重启：进件表是同一个，扫码上线之后到的消息照常叫醒模型。
掉线期间模型「说」过的话（`meta.via` 指向 QQ）不会补发——发不出去就是发不出去，
重连之后不会把旧话翻出来重发（对齐游标时一条历史都不发）。

#### 被冻结 / 风控（"每次扫码都说新设备"）

腾讯对"挂在云服务器上的个人号"本来就不友好。账号被冻结解冻之后，
NapCat 每两分钟自动重试一次登录，连试 35 分钟：

```
10-29 13:46:10 [error] [Core] [Login] Login Error,ErrType: 1 ErrCode: 3
...
10-29 14:21:31 [error] [Core] [Login] Login Error,ErrType: 1 ErrCode: 1
```

这种自动重试在风控眼里就是"反复敲门"，所以有个守护（`napcat-guard.service`）：

- 判据：OneBot 的 `127.0.0.1:3000` 在听 = 登录好了；否则数 `docker logs` 里
  **最后一次登录成功之后**的 `Login Error` 行数。
- 连着 10 次（约 20 分钟）就 `docker stop napcat`，并往 `<data>/alerts.json` 写一条；
  网页顶栏会挂出来，点「知道了」就删掉。
- 你 `docker start napcat` 扫码上线后，守护看到端口在听就把那条提示删掉。

装它：

    sudo install -m 755 deploy/napcat-guard.py /opt/napcat/guard.py
    sudo install -m 644 deploy/napcat-guard.service /etc/systemd/system/
    sudo systemctl daemon-reload && sudo systemctl enable --now napcat-guard

设备指纹那条：镜像**钉死 digest**，不要 `compose pull`。换镜像 = 换 QQ 客户端版本
（连镜像里的 `/etc/machine-id` 一起换），腾讯那边就认成一台新设备，扫码要多一次
"新设备"授权。宿主机名、`/app/.config/QQ` 都已经是固定的，不用动。

社区里同类问题的结论（都指向"云 + 新设备 + 异地"这个组合）：

- NapCat 维护者（[issue #859](https://github.com/NapNeko/NapCatQQ/issues/859)）：
  「换 IP 换账号，或者保持扫码设备与 Docker 接入互联网 IP 一致」；
  [issue #931](https://github.com/NapNeko/NapCatQQ/issues/931) 里补一句
  「napcat 无法控制与 device 相关变量以载入 session」——所以"搬 `.config/QQ` 免扫码"
  不一定成。
- 有人（issue #931 评论）换掉公网 IP 之后就登进去了：同一个 IP 被风控过。
- 更早的 go-cqhttp 时代（[老唐笔记](https://oldtang.com/11071.html)）：本地登录成功后
  把 `device` / `session.token` 搬到服务器；或者关掉账号安全里的"登录保护 /
  登录安全检查"（粗暴，慎用）。

## 日常更新

两个仓都是「铺目录 + 重启」，不用安装、不用 pip：

    # 开发机：打包
    git -C palimpsest archive --format=tar.gz -o p.tar.gz HEAD
    git -C rin        archive --format=tar.gz -o r.tar.gz HEAD
    scp p.tar.gz r.tar.gz <user>@<host>:/tmp/

    # 服务器：解开、同步、重启（两个仓要一起更新：rins 依赖核心的新接口）
    rm -rf /tmp/newp /tmp/newr && mkdir -p /tmp/newp /tmp/newr
    tar -xzf /tmp/p.tar.gz -C /tmp/newp && tar -xzf /tmp/r.tar.gz -C /tmp/newr
    # rin 侧：不能删核心代码与解释器，它们和 rin 住在同一个目录里
    rsync -a --delete --exclude data --exclude .venv --exclude loop --exclude memory_v2 \
          --exclude testsupport.py --exclude check_all.py /tmp/newr/ /opt/rin/
    # 核心侧：只覆盖代码，不带 --delete（否则会把 rin/ checks/ deploy/ 删掉）
    rsync -a /tmp/newp/loop /tmp/newp/memory_v2 /tmp/newp/testsupport.py \
          /tmp/newp/check_all.py /opt/rin/
    sudo systemctl restart palimpsest

## 回滚

    # 1. 单元换回去
    sudo cp /etc/palimpsest/palimpsest.service.bak /etc/systemd/system/palimpsest.service
/etc/systemd/system/qq.service
    sudo systemctl daemon-reload
    # 2. 代码退回去（用备份里的 code 包，删掉 data 与 .venv 之外的东西）
    sudo rm -rf /tmp/oldp && mkdir -p /tmp/oldp
    sudo tar -xzf /root/palimpsest-code-<时间>.tar.gz -C /tmp/oldp
    sudo rsync -a --delete --exclude data --exclude .venv /tmp/oldp/ /opt/rin/
    # 3. 起服务
    sudo systemctl start palimpsest

`/opt/rin/data` 一直没动过，所以旧版本起来就是原来的状态。切换之后新写的数据
留在 `/opt/rin/data`，要接回去得手工合并——**切换后先跑一段时间再清旧目录**。

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: loop` / `rin` | `PYTHONPATH` 没指全，或目录没铺到 | 单元里应有 `PYTHONPATH=/opt/rin:/opt/rin` |
| 日志里的库还是 `/opt/rin/data` | `WorkingDirectory` 还是旧的 | 换成 `/opt/rin`，`daemon-reload` 后重启 |
| `没有登记的提示词` / 工具描述读不了 | `rin/prompts/tools/*.txt` 缺文件 | 重新同步这个仓 |
| 起来就退出 | 数据目录权限（必须 `palimpsest` 可写）、凭证路径 | `journalctl -u palimpsest -n 80 --no-pager` |
| 页面能开但没有思考流 | 网关端口、令牌 | 先用 `/state` 验令牌，再看页面 |
