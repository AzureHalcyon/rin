# rin

把「持续在场」的引擎落到一台真机器上：工具、通道、网页、设置与部署。

算法与循环设计在另一个仓 [`palimpsest`](https://github.com/AzureHalcyon/palimpsest)
（`loop` 循环引擎 + `memory_v2` 记忆介质）。依赖只有一个方向：

    rin  ——→ palimpsest
    palimpsest 里没有 import rin

## 分工

| 在这一侧 | 在 palimpsest |
|---|---|
| 工具实现：`run` / `fetch` / `search` / `write` / `bilibili` / `qq` | 循环引擎：`driver` / `context` / `compress` / `store` |
| 工具描述文本（`prompts/tools/*.txt`） | 提示词的登记、渲染与校验 |
| 记忆工具面：参数 → 动词 → 一句话 | 记忆算法与六个动词（`memory_v2.api`） |
| 网关、本机入口、设置表 | 引擎自己的工具：`wait` |
| 人格、提示词覆盖、部署、运维 | —— |

判据：**算法归 palimpsest，「这个实例怎么跑起来」归这里。**

## 目录

```
rin/
  __main__.py        入口：python -m rin
  app.py             装配线：工具表、提示词登记
  cli.py             命令行（--stdin / --http / --say）
  gateway.py         网页网关（/feed /state /inbox /settings /archive）
  web/index.html     单页界面
  settings.py        设置表
  memory_bridge.py   记忆工具面
  tool_texts.py      工具描述名单
  tools/             shell / webtools / workspace / web_access / landlock / bilibili / qq
  prompts/tools/     各工具的描述（`wait` 的描述在核心）
  qq/                QQ 通道适配器（收事件进收件箱、按 via 定向回发）
checks/              自检
deploy/              systemd 单元、部署与回滚
data/                运行数据（不进版本控制）
```

## 跑起来

开发时两仓并排，用 `PYTHONPATH` 指回核心：

    set PYTHONPATH=D:\path\to\palimpsest
    python -m rin --name 名字 --data data --memory-data data/memory --http 127.0.0.1:8080

`python -m rin --help` 是全部参数；凭证与数据目录都能用参数改。
循环只由工具调用推进，停是模型自己的动作（`wait`）——`--max-steps` 默认 0，不设上限。

## 工具

### 上网

| 工具 | 做什么 |
|---|---|
| `fetch(url, offset)` | 读一个公开网页的正文，默认 3000 字，`offset` 续读 |
| `search(query)` | 搜索，返回标题、链接、摘要 |

抓取分两级：先自己抓（免费，带 SSRF 闸门与正文抽取）；抽不到 300 字或连不上，再走
DeepSeek 的 `/v1/tools/fetch`。搜索分三级：DeepSeek `/v1/tools/search`（带重排分，
低分丢弃）→ Tavily（配了键才走）→ Bing RSS（免费兜底）。

闸门在 `web_access.py`：只许 http/https、只走 80/443、域名必须解析到公网地址、
每一跳重定向重新校验，拒 `localhost` / `.local` / `.internal` / 云元数据。
同一个链接五分钟内不重复抓。

### 工作区

`<data>/workspace` 是唯一可写的地方，也是 `run` 的当前目录。

| 工具 | 做什么 |
|---|---|
| `run(command)` | 在宿主上跑一条命令（`sh -c`），默认关 |
| `write(path, content)` | 往工作区写文件，只接受相对路径 |

`run` 的边界交给内核：Landlock 只放工作区可写，别处能读能执行；单条命令默认 20 秒、
单文件 64 MB、输出留头尾共 4000 字；装不上 Landlock 就拒跑（fail-closed）。
命令里点到凭证（`/etc/palimpsest`、`credentials.yaml`、`*.pem`、`.ssh`）直接不跑。

启动时会在工作区写一份《这台机器.md》，说明能写哪儿、怎么装库、限额多少。

### B 站

一个入口、五个动作（`bilibili(action="help")` 看全部）：
`search` / `video` / `subtitles` / `playurl` / `transcript`。

- 字幕要登录 cookie：`BILI_SESSDATA`（裸 SESSDATA 值），没有就只回「账号未登录」；
- `transcript` 没字幕时走 下载 → ffmpeg 抽音轨 → 百炼 `qwen3-asr-flash` 转写，
  一次最多 30 分钟；
- 设置页的「B 站」组可以覆盖这三样（cookie / ffmpeg 路径 / 转写模型）。

基于 [`dsh-plugin-bilibili`](https://github.com/AzureHalcyon/dsh-bilibili) fork 并修改：
搜索、元数据、字幕、直链四步的接口与风控降级沿用上游，`rin/tools/bilibili.py` 重写到
Python 这一侧，并加了 `transcript`（没字幕时抽音轨转写）。上游本身是 DeepSeek
Harness 插件，本仓不依赖它运行。

### QQ

通道在 `rin.qq`：把 OneBot 事件收进收件箱，按记录上的 `meta.via` 定向回发。
模型这一侧只有一个入口 `qq(action, params)`：

- 动作清单写在 `<数据>/workspace/QQ-动作清单.md`（每次启动现写），
  用 `qq(action="index")` 看组、`qq(action="help", params={"group": …})` 看组内动作；
- 最常用的三个直接写在工具说明里；
- 会弄坏连接或等于交出账号的动作（`send_packet`、`set_restart`、`get_cookies` …）直接拒；
- 端点与令牌：`NAPCAT_BASE`、`NAPCAT_TOKEN`。

用到的外部东西：

| 项目 | 用途 | 说明 |
|---|---|---|
| [NapCat](https://github.com/NapNeko/NapCatQQ)（镜像 `mlikiowa/napcat-docker`） | QQ 协议端：登录、收消息、执行动作 | 单独跑在 Docker 里，本仓只连它的 HTTP 端点；掉线与登录见 [`deploy/README.md`](deploy/README.md) |
| [OneBot 11](https://github.com/botuniverse/onebot-11) | 事件与动作的协议 | 收事件用 HTTP 上报（`POST /feed`），发消息走动作接口 |

接 NapCat、掉线处置见 [`deploy/README.md`](deploy/README.md)。

## 网页

```powershell
python -m rin --name 名字 --http 127.0.0.1:8080
```

单文件 `web/index.html`，不起前端工程。令牌在 `<data>/web-token`（600），也可 `--token`。
**默认只绑 127.0.0.1**。

| 接口 | 作用 |
|---|---|
| `POST /say {"text": "…"}` | 写进 `inbox` |
| `GET /feed?since=<block>:<seq>&step=<n>` | 增量原文 + 增量用量，断线按游标补齐 |
| `GET /context` | sink、旧段摘要、上一块、当前块 |
| `GET /state` | 状态、块与上下文占用、今日用量、三处模型名、落地侧提示 |
| `GET /inbox` | 最近进件与每个会话的策略 |
| `GET /models?kind=chat\|summary\|embed` | 向端点要模型清单（缓存两分钟） |
| `GET/PUT /settings` | 旋钮与改动记录；PUT 保存即生效 |
| `PUT /prompts` | 改一份提示词（空 = 恢复默认） |
| `POST /archive` | 提前清：把当前这块封口（压缩 → 补录 → 新开一块） |
| `POST /alerts/clear` | 点掉顶栏那条落地侧提示 |
| `DELETE /entries?block=&seq=` | 删一条记录（成对删工具结果） |
| `POST /restart` | 请求循环走到步与步之间退出，交给进程管理器拉起 |

网页只读库、只往 `inbox` 写；写库的只有循环那个进程。每条连接各开一个 `LoopStore`，
库开 WAL，读不挡写。

## 设置与接口

- 参数存 `<data>/settings.json`，只记改过的键；保存即生效（都是每步现读的）。
- 提示词存 `<data>/prompts.json`，每步现读；「恢复默认」就是删掉那条覆盖。
- 渠道（对话 / 嵌入的地址与密钥）三层，从下往上盖：代码默认 → 凭证文件
  `/etc/palimpsest/credentials.yaml` 的 `refs` → 网页设置里的「接口」组。
  对话与嵌入分开填（常常不是同一家）。改完当场重建客户端，不用重启。
- 换摘要模型会重建摘要客户端；换嵌入模型要连维度一起对，否则向量缓存认不出来。

只有改代码才需要重启进程（`POST /restart`），循环会走到步与步之间自己退出。

## 自检

    python checks/run.py

不联网、不用凭证：假模型、假嵌入、临时目录。

## 部署

见 [`deploy/README.md`](deploy/README.md)：机器布局、装包、接 QQ、掉线处置、更新与回滚。

## 协议

| 部分 | 协议 |
|---|---|
| 代码 | Apache-2.0，见 [`LICENSE`](LICENSE) |
| 设计 / 文档 | CC BY-SA 4.0，见 [`LICENSE-DOCS`](LICENSE-DOCS) |

Copyright © 2026 AzureHalcyon
