# DyVault · 抖音批量下载（Server 版）

按博主主页批量归档抖音作品：**作品 / 博主点赞 / 我的收藏**，同时采集评论与互动数据。
面向 NAS（飞牛 fpk）、服务器与 Docker 部署，带密码保护与多账号轮询。

```
DyVault = Dy（抖音）+ Vault（归档库）
```

## 功能

- **三种来源**：博主作品、博主点赞、我的收藏（点赞 / 收藏需登录态）
- **多账号 Cookie 池**：可存多个账号，抓取时按请求轮询（默认每 25 次请求换一个），单账号失效或触发风控自动切换下一个
- **两种保存位置**：服务器本地磁盘 / 浏览器所在电脑（只抓清单与直链，媒体落到操作者本机）
- **两种目录结构，各自独立路径**：博主作品合集（`video/`、`image/` 分类归档）与单作品目录（`works/日期_ID_描述/`）可分别指定服务器路径
- **密码登录**：首次访问设置密码，之后每次进入需验证；PBKDF2 存储 + HMAC 会话，改密码后旧会话自动失效
- **完整数据**：无水印视频、图集原图、封面、背景音乐，评论（含二级回复）、点赞 / 收藏 / 分享数
- **断点续跑**：重跑自动跳过已下载文件；暂停 / 继续 / 取消
- **打包下载**：任意任务可一键流式打包 ZIP 下载到本机，不占服务器临时空间
- **文件库**：网页内浏览服务器下载目录，逐级进入、单文件取回

## 快速开始

### 方式一：飞牛 NAS（fpk）

```bash
python build_fpk.py -v 1.0.0        # 生成 dist/douyin-dl_1.0.0_x86_64.fpk
```

在飞牛应用中心手动安装 fpk，首次启动会自动准备 Python 依赖（走阿里云镜像），日志在 `var/app.log`。

### 方式二：Docker

```bash
docker compose up -d
# 打开 http://<服务器IP>:8080
```

`./config` 存数据库与配置，`./downloads` 是下载根目录，两者都是宿主机目录。

### 方式三：裸机 / systemd

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8080
```

飞牛一键部署（SSH + venv + systemd，幂等）：

```bash
FNOS_HOST=192.168.1.10 FNOS_USER=nasuser FNOS_PASS='密码' python scripts/deploy_fnos.py
```

当前测试机：`/vol1/1000/programs/douyin-dl`，systemd 服务名 `douyin-dl`（沿用旧名，避免破坏已有部署），
下载目录 `/vol1/1000/programs/douyin-dl/downloads`。

## 首次使用

1. 打开 `http://<服务器IP>:8080` → 设置访问密码（至少 6 位）
2. 进入「设置 → Cookie 账号池」粘贴**已登录 douyin.com 的浏览器 Cookie**（F12 → Network → 复制 Cookie 请求头），可添加多个账号
3. 回「下载任务」，粘贴博主主页链接 → 选择来源、保存位置、目录结构 → 开始下载

> Cookie 过期后点「测试」会显示失效，替换该账号的 Cookie 即可；账号池里可随时启停单个账号。

## 目录结构

两种结构各用一条可配置路径（设置 → 下载路径）：

| 目录结构 | 默认路径 | 内部形态 |
|---|---|---|
| 博主作品合集 | `downloads/` | `video/` `image/` `cover/` `music/` `data/` `comments/` |
| 单作品目录 | `downloads_per_aweme/` | `works/日期_ID_描述/` |

下面是合集模式（默认 `<合集路径>/<博主昵称>_<sec_uid 后 8 位>/`）：

```
<合集路径>/<博主昵称>_<sec_uid 后 8 位>/
├── video/                          # 合集模式：所有视频
│   └── 20250101_7001_作品描述.mp4
├── image/7001/01.jpg               # 合集模式：图集按作品 ID 分目录
├── cover/7001.jpg
├── music/7001.mp3
├── data/7001.json                  # 元数据（含互动数据）
├── comments/7001.jsonl             # 评论（每行一条）
└── index.csv                       # 该博主全部作品汇总
```

选择「每个作品一个目录」时改为：

```
<单作品路径>/<博主>_<id>/works/20250101_7001_作品描述/
├── video.mp4
├── images/01.jpg …
├── cover.jpg / music.mp3
├── meta.json
└── comments.jsonl
```

## 命令行

```bash
python cli.py -u "https://www.douyin.com/user/MS4wLjABAAAA..." --limit 50
python cli.py -u MS4w... --source like --layout per_aweme --comments 200
python cli.py -u MS4w... --cookie-file cookie.txt --interval 2
```

`--layout collection|per_aweme` 控制目录结构；Cookie 会自动导入账号池。

## 配置项

| 设置 | 说明 | 默认 |
|---|---|---|
| 请求间隔 | 两次接口请求的最小间隔（秒） | 1.2 |
| 随机抖动 | 额外随机等待，降低规律性 | 0.8 |
| 单作品并发下载 | 同一作品内视频 / 图片并发数 | 4 |
| 同时运行任务数 | 并行任务上限 | 1 |
| 合集下载路径 | 服务器本机绝对路径，留空为程序目录同级 `downloads/` | 空 |
| 单作品下载路径 | 服务器本机绝对路径，留空为程序目录同级 `downloads_per_aweme/` | 空 |
| API 令牌 | 填写后调用 API 可带 `X-Token` 跳过登录页 | 空 |

环境变量：`DOUYIN_DATA_DIR`、`DOUYIN_DOWNLOAD_ROOT`、`DOUYIN_DOWNLOAD_ROOT_PER_AWEME`、`DOUYIN_COOKIE`、`DOUYIN_INTERVAL`、`DOUYIN_TOKEN` 等（优先级高于网页设置）。

## 安全说明

- 访问密码用 PBKDF2-SHA256（15 万轮）存储，会话是 HMAC 签名的 HttpOnly Cookie，有效期 30 天
- 密码错误 8 次锁定该 IP 120 秒
- 所有文件接口限定在下载根目录内，越界返回 400
- 媒体代理只放行抖音 CDN 域名，避免被当成任意 URL 代理
- 公网暴露时建议同时设置 API 令牌，并在反向代理上再加一层 HTTPS

## 代码结构

```
app/            FastAPI 服务 + 前端页面（原生 JS，无需构建）
core/           签名 (a_bogus/SM3)、客户端、解析、下载、任务、账号池、认证、流式 ZIP
cli.py          命令行入口
scripts/        飞牛部署脚本
tests/          本地桩服务 + 端到端测试
build_fpk.py    飞牛 fpk 打包
```

## 测试

```bash
python tests/e2e_test.py
```

用本地桩服务模拟抖音接口，覆盖签名、分页、视频 / 图集 / 封面 / 音乐下载、评论采集、CSV/JSONL 落盘、幂等重跑、多账号轮询、单作品目录、浏览器本机清单等 33 项断言。
