# BuddyZ Gateway

把本机已登录的五家 AI 桌面端订阅，统一转成 **OpenAI 兼容 API**，供任意客户端（ChatBox、Cherry Studio、OpenWebUI、Hermes Agent、脚本…）调用。

单文件 Python + Tkinter GUI，**零外部服务依赖**：不装 Docker、不填 Cookie、不需要手动粘贴任何密钥 —— 只要本机对应的桌面端登录过，网关自己会去找凭证。

| 通道 | 上游 | 凭证来源 | 备注 |
|------|------|----------|------|
| ① CodeBuddy / WorkBuddy | `copilot.tencent.com` / `codebuddy.ai` | 桌面端 `auth/*.info` | 带原生 function calling / tool_calls |
| ② MonkeyCode | `ai-models.app.baizhi.cloud` | 桌面端 `config.json` | 支持多 key 号池轮转、今日用量、积分、每日签到 |
| ③ 华为云 CodeArts（码道） | `snap-access` / `opengw` | 桌面端加密会话 → AK/SK | DPoP 自动续期、余额查询、一键 OAuth 授权 |
| ④ 商汤小浣熊办公 | `xiaohuanxiong.com` | 桌面端 `auth.json` | 401 自动刷新 token 并落盘 |
| ⑤ Loomy（讯飞 iModel） | `loomyad.xunfei.cn` | 桌面端会话 / 内嵌 opencode | 12 个模型（`spark-x` 免费，其余按倍率扣积分）、积分查询、原生 tool_calls |

五个服务**各自独立端口**、独立启停，互不影响。

---

## 快速开始

```bash
pip install fastapi "uvicorn[standard]" httpx
python BuddyZGateway.py            # 打开 GUI
```

GUI 里逐个面板点「启动」，或点顶部「一键启动」全部拉起。启动后客户端填：

```
Base URL : http://127.0.0.1:8787/v1     # WorkBuddy 通道
Base URL : http://127.0.0.1:9000/v1     # MonkeyCode 通道
Base URL : http://127.0.0.1:9100/v1     # 华为云 CodeArts 通道
Base URL : http://127.0.0.1:9200/v1     # 小浣熊通道
Base URL : http://127.0.0.1:9400/v1     # Loomy 通道
API Key  : 留空
```

### 命令行模式

```bash
python BuddyZGateway.py --selftest                 # 无界面自检
python BuddyZGateway.py --serve                    # 无界面三服务（常驻）
python BuddyZGateway.py --serve --mc-port 9100     # 自定义端口
```

Windows 想彻底去掉控制台黑框，用 `launch_silent.vbs`（内部走 `pythonw.exe`）。

---

## 各通道端点

五个通道都实现 OpenAI 标准接口：

- `GET  /v1/models` — 模型列表
- `POST /v1/chat/completions` — 对话（流式 / 非流式）

各自额外的增强接口：

| 通道 | 端点 | 说明 |
|------|------|------|
| WorkBuddy | `GET /v1/credits` | 剩余积分（上游 billing 汇总） |
| MonkeyCode | `GET /v1/usage` | 今日已用 token（本地累计） |
| MonkeyCode | `GET /v1/wallet` | 积分余额 + 每日 token 额度 |
| MonkeyCode | `GET/POST /v1/checkin` | 签到状态 / 执行签到（Cap.js PoW 自动求解） |
| CodeArts | `GET /v1/balance` | 每日 token 额度余额 |
| CodeArts | `POST /v1/claim` | 每日福利领取 |
| CodeArts | `GET /v1/auth/url` | 生成 OAuth 授权链接 |
| CodeArts | `GET /oauth/callback` | OAuth 回调（自动换票并持久化） |
| Loomy | `GET /v1/points` | 积分余额（永久积分 / 每日积分，读 Loomy 本地缓存） |

---

## 设计要点

**凭证全部自动探测，不落外部配置**
桌面端的登录态就是凭证来源。CodeArts 走 DPAPI + AES-GCM 解密桌面端会话库；WorkBuddy 读 `auth/*.info`；MonkeyCode 读 `config.json`。

**MonkeyCode 号池（默认关闭）**
面板勾选「号池轮转」后，可挂多个透传 key 按序轮转：401/403 判定失效永久跳过，429/5xx 冷却 5 分钟，传输异常冷却 60 秒；本机桌面端 key 永远打底。

**CodeArts 两条凭证链**
- DPoP 链（桌面端同款）：有完整模型路由，`refresh_token` 单次轮转，网关内加文件锁 + 成功后立即落盘，所以不会烧 token。
- ticket 链（独立 OAuth 授权）：有效期约 24h，负责余额 / 签到；免费模型需配合 `maas_type: benefit` 头才有路由。

**Hermes Agent 集成**
一键把五条通道写进 Hermes 的 `providers`（含 `.env` 里的占位 key），自动覆盖本机**全部** profile。

**高 DPI 适配**
Per-Monitor DPI Aware v2；窗口创建后按所在显示器 `rcWork`（已扣任务栏）回位，多屏不跑出可视区。

**细节**
- 运行日志右键可复制 / 全选 / 清空
- 托盘常驻，关闭窗口缩到托盘，服务继续跑
- 退出时强制结束进程，不留控制台残留
- 「启动程序」按钮可直接拉起对应桌面端

---

## 目录结构

```
BuddyZGateway.py       # 单文件程序（GUI + 五个反代核心，内嵌为 base64）
BuddyZGateway.spec     # PyInstaller 打包配置（无控制台窗口）
launch_silent.vbs      # Windows 静默启动脚本
```

五个反代核心（`codebuddy2openai` / `monkeycode2openai` / `codearts2openai` /
`raccoon2openai` / `loomy2openai`）以 base64 内嵌在 `BuddyZGateway.py` 中，
首次运行自动解包到：

```
%LOCALAPPDATA%\BuddyZGateway\runtime\
```

配置与日志：

```
%LOCALAPPDATA%\BuddyZGateway\data\settings.json    # GUI 设置（端口、模型白名单、号池 key）
%LOCALAPPDATA%\BuddyZGateway\data\logs\             # 各服务日志
```

---

## 打包

```bash
pip install pyinstaller
pyinstaller BuddyZGateway.spec
# 产物：dist/BuddyZGateway.exe
```

---

## 免责声明

本项目仅用于**本机个人账号**的接口协议转换，方便在自选客户端里使用自己已订阅的服务。
请遵守各上游平台的服务条款，不要用于账号共享、批量刷取额度等滥用场景。
使用者需自行承担因使用本工具产生的一切