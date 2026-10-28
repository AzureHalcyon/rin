# 部署与回滚

这个仓是「落地」那一半。核心在 `palimpsest`，两个仓**并排铺开、不装进站点包**：
systemd 用 `PYTHONPATH` 把两个目录都指上，改代码只要重传再重启。

## 目录布局

```
/opt/palimpsest            核心仓（loop + memory_v2）+ .venv
/opt/rin                   这个仓 + data/（她的运行数据）
/etc/palimpsest/credentials.yaml     凭证：模型与嵌入的 key / url
/etc/palimpsest/loop.env             LOOP_NAME / LOOP_HTTP / LOOP_DAILY_CAP / LOOP_DAILY_WARN
/etc/systemd/system/palimpsest.service
```

数据目录（`--data`）跟着这个仓走：`/opt/rin/data`。凭证仍在 `/etc`，两边共用。
`/opt/palimpsest` 底下还有三样**不要**被同步冲掉：`.venv`、`data`（迁移前的旧数据，留作回滚）、
`app`（跟这两个仓无关的遗留目录）；`/opt/rin/data` 同样不能被同步冲掉——所以两边的
`rsync --delete` 都要带 `--exclude data`。

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
   状态「叫过了，还没取」），她的记录里出现一行「还有 1 条没看的消息…」，
   调 `messages` 能看到 `外部 · 私聊「…」 某人：…`；她回话会发回那个会话。
   回发是照记录上的 `meta.via` 定向的：网页里跟她聊的话不会被复述到 QQ 上。

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

她那边不用重启：进件表是同一个，扫码上线之后到的消息照常叫醒她。
她在掉线期间"说"过的话（`meta.via` 指向 QQ）不会补发——发不出去就是发不出去，
重连之后不会把旧话翻出来重发（对齐游标时一条历史都不发）。

## 从单仓切过来（第一次）

前提：旧版本（单仓、`-m loop`）在跑，`/opt/palimpsest/data` 是她现在的全部运行数据。

1. **备份**（三个都要）：

       D=$(date +%Y%m%d-%H%M%S)
       sudo tar -czf /root/palimpsest-data-$D.tar.gz -C /opt/palimpsest data
       sudo tar -czf /root/palimpsest-code-$D.tar.gz -C /opt/palimpsest --exclude=data --exclude=.venv --exclude=app .
       sudo cp -a /etc/systemd/system/palimpsest.service /etc/palimpsest/palimpsest.service.bak

2. **传两个包**（在开发机上，两个仓各打一个）：

       git -C palimpsest archive --format=tar.gz -o deploy-palimpsest.tar.gz HEAD
       git -C rin        archive --format=tar.gz -o deploy-rin.tar.gz HEAD
       scp deploy-palimpsest.tar.gz deploy-rin.tar.gz <user>@<host>:/tmp/

3. **铺代码**（服务器上）。核心用 `rsync --delete`，但要排除那三样：

       mkdir -p /tmp/newp /tmp/newr
       tar -xzf /tmp/deploy-palimpsest.tar.gz -C /tmp/newp
       tar -xzf /tmp/deploy-rin.tar.gz -C /tmp/newr
       sudo rsync -a --delete --exclude data --exclude .venv --exclude app /tmp/newp/ /opt/palimpsest/
       sudo mkdir -p /opt/rin
       sudo rsync -a --delete --exclude data /tmp/newr/ /opt/rin/
       sudo chown -R palimpsest:palimpsest /opt/rin

4. **迁数据：复制，不移动。** 先停服务再复制，旧目录原样留着，回滚要用：

       sudo systemctl stop palimpsest
       sudo cp -a /opt/palimpsest/data /opt/rin/data
       sudo chown -R palimpsest:palimpsest /opt/rin/data

5. **换单元、起服务**：

       sudo install -m 644 /opt/rin/deploy/palimpsest.service /etc/systemd/system/palimpsest.service
       sudo systemctl daemon-reload
       sudo systemctl start palimpsest

6. **核对**（四条都要看）：

       systemctl status palimpsest --no-pager
       curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:8080/
       curl -s -H 'Authorization: Bearer <token>' http://127.0.0.1:8080/state | head -c 400
       sudo journalctl -u palimpsest -n 30 --no-pager

   日志开头那行应当显示 `库 /opt/rin/data/loop.sqlite3 · 记忆 /opt/rin/data/memory`；
   显示成 `/opt/palimpsest/data` 就说明 `WorkingDirectory` 没生效。

## 日常更新

两个仓都是「铺目录 + 重启」，不用安装、不用 pip：

    # 开发机：打包
    git -C palimpsest archive --format=tar.gz -o p.tar.gz HEAD
    git -C rin        archive --format=tar.gz -o r.tar.gz HEAD
    scp p.tar.gz r.tar.gz <user>@<host>:/tmp/

    # 服务器：解开、同步、重启（两个仓要一起更新：rins 依赖核心的新接口）
    rm -rf /tmp/newp /tmp/newr && mkdir -p /tmp/newp /tmp/newr
    tar -xzf /tmp/p.tar.gz -C /tmp/newp && tar -xzf /tmp/r.tar.gz -C /tmp/newr
    sudo rsync -a --delete --exclude data --exclude .venv --exclude app /tmp/newp/ /opt/palimpsest/
    sudo rsync -a --delete --exclude data /tmp/newr/ /opt/rin/
    sudo systemctl restart palimpsest

## 回滚

    # 1. 单元换回去
    sudo cp /etc/palimpsest/palimpsest.service.bak /etc/systemd/system/palimpsest.service
    sudo systemctl daemon-reload
    # 2. 代码退回去（用备份里的 code 包，删掉 data 与 .venv 之外的东西）
    sudo rm -rf /tmp/oldp && mkdir -p /tmp/oldp
    sudo tar -xzf /root/palimpsest-code-<时间>.tar.gz -C /tmp/oldp
    sudo rsync -a --delete --exclude data --exclude .venv /tmp/oldp/ /opt/palimpsest/
    # 3. 起服务
    sudo systemctl start palimpsest

`/opt/palimpsest/data` 一直没动过，所以旧版本起来就是原来的状态。切换之后新写的数据
留在 `/opt/rin/data`，要接回去得手工合并——**切换后先跑一段时间再清旧目录**。

## 常见故障

| 现象 | 原因 | 处理 |
|---|---|---|
| `ModuleNotFoundError: loop` / `rin` | `PYTHONPATH` 没指全，或目录没铺到 | 单元里应有 `PYTHONPATH=/opt/palimpsest:/opt/rin` |
| 日志里的库还是 `/opt/palimpsest/data` | `WorkingDirectory` 还是旧的 | 换成 `/opt/rin`，`daemon-reload` 后重启 |
| `没有登记的提示词` / 工具描述读不了 | `rin/prompts/tools/*.txt` 缺文件 | 重新同步这个仓 |
| 起来就退出 | 数据目录权限（必须 `palimpsest` 可写）、凭证路径 | `journalctl -u palimpsest -n 80 --no-pager` |
| 页面能开但没有思考流 | 网关端口、令牌 | 先用 `/state` 验令牌，再看页面 |
