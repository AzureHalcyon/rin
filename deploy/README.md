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
`app`（跟这两个仓无关的遗留目录）。

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
       sudo rsync -a --delete /tmp/newr/ /opt/rin/
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

    # 服务器：解开、同步、重启
    rm -rf /tmp/newp && mkdir -p /tmp/newp && tar -xzf /tmp/p.tar.gz -C /tmp/newp
    sudo rsync -a --delete --exclude data --exclude .venv --exclude app /tmp/newp/ /opt/palimpsest/
    sudo rsync -a --delete /tmp/newr/ /opt/rin/
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
