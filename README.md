# 🐴 半人马AI 个人记忆库

一套本地优先、持续沉淀的个人 AI 记忆系统。它让不同 AI 调用同一份属于你的个人记忆，并支持 PDF/Word/Excel/PPT/Markdown/图片/**音频/视频**的自动向量化、语义搜索和 Obsidian 风格个人 Wiki 知识层。

## 功能

- **自动向量化** — 文件放入监控目录后自动解析、向量化、存入 ChromaDB
- **语义搜索** — 用自然语言搜索本地文档，返回语义相关内容
- **Wiki 知识层** — 导入资料会沉淀为 `wiki/Sources` 与 `wiki/Concepts` Markdown 页面，支持反链、出链、局部图谱和二次编辑
- **本地模型自动整理** — 通过本机 Ollama `qwen3:1.7b` 生成 Wiki 摘要、概念、标签和链接；按需加载并在调用后卸载，低内存时使用本地规则降级，资料不离开设备
- **自动维护** — 后端每日维护 Wiki：刷新页面索引、链接关系、向量索引和首页维护摘要
- **手机导入** — 手机连入 Tailscale 后，打开 `/mobile` PWA 或使用 `frontend/mobile-native` 原生壳，用 App Token 导入文件、剪贴内容或录音，并自动进入向量化与 Wiki 整理队列；安装为 PWA 后可作为系统分享目标，接收文本、URL 和文件
- **移动端待同步队列** — 手机端采集时如果 Tailscale/服务器暂时不可达，录音、文件、系统分享文件和剪藏文本会先保存到手机 IndexedDB，保存 Token、网络恢复或手动点击同步后自动重试
- **录音转写** — `.m4a/.mp3/.wav/.aac/.ogg/.opus/.flac` 会通过 faster-whisper 转写、切块、向量化；手机浏览器录音上传的 `.webm` 会作为音频导入；手机结果页会自动刷新待处理项并展示转录全文、摘要和 Wiki 页面
- **A2A Context Pack** — 用户可以创建带独立 Token 的上下文包，对外提供 Agent Card、`message:send` 和只读 context 拉取接口；手机端可复制或调用系统分享面板发送邀请包，用于受控 A2A 社交
- **个人 Context 快照** — 后端定期把近期手机采集、Wiki 概念、记忆摘录和文档概览沉淀为 `wiki/Resources/Personal-Context-Snapshot.md`，手机端可查看、刷新、复制给 AI 使用
- **多格式支持** — PDF、Word (.docx)、Excel (.xlsx/.xlsm/.xls)、PPT (.pptx)、Markdown、纯文本、图片、音频、视频
- **视频理解** — 视频自动拆三路：①音轨语音转写(faster-whisper) ②关键帧画面检索(Chinese-CLIP) ③帧内文字 OCR；每条结果带 **时间戳**，命中即知第几分几秒
- **人工标注** — 给文件打**标签**(分类/筛选)、标**重要度**(检索加权)、**置顶**(命中相关查询必回)、写**说明**(让无文字的纯图/LOGO 也能被「公司标识」这类语义搜到)。标注存独立 sidecar，重建索引不丢
- **本地运行** — 全部 CPU 推理，不依赖云服务
- **桌面应用** — Electron 桌面壳，支持拖拽上传和托盘最小化
- **开机自启** — systemd 用户服务

## 技术栈

| 组件 | 技术 |
|------|------|
| 后端 | Python FastAPI |
| 文本嵌入 | BAAI/bge-small-zh-v1.5 (24MB) |
| 图片嵌入 | OpenAI CLIP-ViT-B-32 (600MB) |
| 视觉检索 | Chinese-CLIP (图↔中文同空间) |
| 语音转写 | faster-whisper (CTranslate2, CPU/int8) |
| 视频抽帧/抽音轨 | ffmpeg / ffprobe（系统依赖） |
| 向量存储 | ChromaDB (余弦相似度) |
| Wiki 存储 | Markdown Vault + SQLite 元数据 + ChromaDB Wiki 集合 |
| 导入整理模型 | Ollama + Qwen3 1.7B（本机运行） |
| 文档解析 | pymupdf (PDF), python-docx (Word), openpyxl/xlrd (Excel) |
| 文件夹监控 | watchdog |
| 前端 | Electron + Mobile PWA + Capacitor Native Shell |

### 视频索引说明

- 一个视频被拆成：**转写块**（按 ~30s 聚合，各带 `start_time`）+ **关键帧**（场景切分，低动态回退定时抽帧）+ **帧 OCR 文本**，分别落入文本集合与图片集合，`source_path` 统一为视频路径。
- **graceful degrade**：缺 `ffmpeg` → 跳过该视频；缺 `faster-whisper` → 仅走帧+OCR；无音轨 → 跳过转写；无视频流 → 仅转写；损坏文件 → 跳过且服务不崩。
- 视频上传/落库走**后台单 worker 串行池**（不阻塞事件循环、避免多转写实例 OOM），前端经 `/api/jobs` 轮询进度。
- 默认 whisper 模型 `small`（中文/CPU 平衡）；追质量改 `config.py` 的 `WHISPER_MODEL` 为 `medium`/`large-v3`。装好 whisper 后旧视频会因「能力指纹」变化在下次扫描自动补出转写。
- **转写超时**：墙钟上限 `max(600s, 时长×WHISPER_TIMEOUT_RTF)`，封顶超长/卡死视频对后台串行池的占用（超时返回已转出的部分）。
- **繁→简**：whisper 对中文默认出繁体，`TRANSCRIPT_TO_SIMPLIFIED=True` 经 opencc 统一为简体（与库主体/bge-zh 一致）；opencc 缺失则保留原文。
- **跳到时刻播放**：搜索命中视频块时结果显示可点的 `⏱ mm:ss ▶`，点击在内置播放器 seek 到该秒（经 `/api/video` Range 流式）。

## 快速开始

```bash
cd ~/桌面/local-vector-db
bash install.sh
```

安装完成后：
- 后端自动启动（systemd 服务）
- 双击桌面「半人马AI 个人记忆库」图标打开 Electron 界面
- 把 PDF/Word/Excel/MD/图片拖入 `data/watch_folder/` 即自动向量化
- 本机 Ollama 与 `qwen3:1.7b` 就绪后，新导入资料会自动生成 Wiki 摘要、概念和链接
- 在设置里启用「手机 App 导入」并生成 App Token 后，手机连入 Tailscale 即可导入资料；桌面端可生成配对链接和本地二维码，手机扫码后自动保存 Token

### 运行模式：Web / 桌面（命令约定）

`npm start` 不再隐式启动 Electron，而是提示你选择明确命令，避免把 Web 启动误当桌面：

```bash
cd frontend
npm run web          # Web 开发：只启动 Vite（http://127.0.0.1:5173/mindos/），不启动 Electron
npm run web:build    # 构建 Web 产物（frontend/mindos-web/dist）
npm run desktop      # Electron 桌面应用（唯一显式桌面入口，可托管后端子进程）
npm start            # 拒绝执行并提示选择 web / desktop
```

根目录也提供等价脚本：

```bash
./start-web.sh       # 只启动 Web 开发服务（Vite），后端需另行 ./start-backend.sh
./start-backend.sh   # 启动 Python 后端（127.0.0.1:8618，生产模式亦由后端在 /mindos 提供前端）
./start-desktop.sh   # 只启动 Electron 桌面应用
```

Windows PowerShell 使用：

```powershell
.\start-backend.ps1 # 启动 Python 后端
.\start-web.ps1     # 只启动 Vite，不启动 Electron
```

Web 生产访问路径为后端 `http://127.0.0.1:8618/mindos/`（独立后端进程，不启动 Electron）。
`start-frontend.sh` 已废弃并改为失败提示，防止名称暗示它是 Web 前端。

## API

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/health` | GET | 健康检查（含 video/transcribe 能力位）|
| `/api/upload` | POST | 上传文件并向量化（视频异步入队）|
| `/api/jobs/{doc_id}` | GET | 后台索引任务状态（视频：queued/processing/done/failed）|
| `/api/search` | POST | 语义搜索（text/visual/hybrid）|
| `/api/retrieve` | POST | 应用端统一检索：按 knowledge/memory/all 返回有界、去重、结构化结果 |
| `/api/documents` | GET | 已索引文档列表 |
| `/api/documents/{id}` | DELETE | 删除文档（连带清磁盘帧+标注）|
| `/api/annotations` | GET | 全部标注，或 `?source_path=` 单文件 |
| `/api/annotations` | POST | 写/改标注（tags/importance/pinned/note/caption；改 caption 触发该文件重索引）|
| `/api/annotations/{path}` | DELETE | 删标注 |
| `/api/image` | GET | 图片缩略图（限监控目录）|
| `/api/file` | GET | 预览或下载原文件（限监控目录与受支持格式）|
| `/api/frame` | GET | 视频帧缩略图（限 video_frames 目录）|
| `/api/video` | GET | 视频流（限监控目录，支持 Range，供命中跳到时刻播放）|
| `/api/stats` | GET | 统计信息（含 video_documents/video_frames）|
| `/api/wiki/stats` | GET | Wiki 页面、链接和整理队列统计 |
| `/api/wiki/pages` | GET/POST | 列出或新建 Wiki 页面 |
| `/api/wiki/pages/{path}` | GET/PUT | 读取或保存 Wiki Markdown 页面 |
| `/api/wiki/search` | POST | 搜索 Wiki 语义索引 |
| `/api/wiki/graph` | GET | 获取全局或局部 Wiki 图谱 |
| `/api/wiki/organizer/status` | GET | 查看本地 Ollama Wiki 整理模型状态 |
| `/api/wiki/organize` | POST | 对某个监控目录内源文件重新提交 Wiki 整理 |
| `/api/wiki/reindex` | POST | 重建 Wiki 向量索引和链接索引 |
| `/api/wiki/maintenance` | POST | 手动触发 Wiki 维护 |
| `/mobile` | GET | 手机端 PWA 页面：录音、文件上传、剪藏、语义搜索、结果查看、个人 Context 和 A2A Context Pack |
| `/api/mobile/config` | GET/POST | 查看或保存手机导入开关和 App Token |
| `/api/mobile/pairing` | POST | 本机生成手机配对链接，手机打开后自动保存 App Token |
| `/api/mobile/uploads` | POST | 手机端上传文件并入队索引，需要 Bearer/App Token |
| `/api/mobile/clips` | POST | 手机端提交剪贴文本并入队索引，需要 Bearer/App Token |
| `/api/mobile/recordings` | POST | 手机端上传录音并入队转写/索引，需要 Bearer/App Token |
| `/api/mobile/items` | GET | 手机端拉取服务端最近导入历史与处理状态，需要 Bearer/App Token |
| `/api/mobile/jobs/{doc_id}` | GET | 查询手机导入文件的索引与 Wiki 整理状态 |
| `/api/mobile/results/{doc_id}` | GET | 查询手机导入结果：状态、转录文本、摘要、关联 Wiki 页面 |
| `/api/mobile/search` | POST | 手机端语义搜索个人资料、Wiki 和记忆，需要 Bearer/App Token |
| `/api/mobile/context/query` | POST | 手机端按当前任务调取个人 Context，需要 Bearer/App Token |
| `/api/mobile/context/snapshot` | GET | 手机端读取自动整理的个人 Context 快照，需要 Bearer/App Token |
| `/api/mobile/context/snapshot/refresh` | POST | 手机端手动刷新个人 Context 快照，需要 Bearer/App Token |
| `/api/mobile/context/packs` | GET/POST | 手机端列出或创建 A2A Context Pack，需要 Bearer/App Token |
| `/api/mobile/context/packs/{id}/preview` | GET | 手机端预览某个 Context Pack 拼出的上下文，需要 Bearer/App Token |
| `/api/mobile/context/packs/{id}/invite` | GET | 手机端生成 A2A 邀请包：Agent Card、Message URL 和 Context Token |
| `/api/context/packs` | GET/POST | 列出或创建 A2A Context Pack |
| `/api/context/packs/{id}` | PUT/DELETE | 更新或删除 Context Pack |
| `/api/context/packs/{id}/preview` | GET | 本机预览某个 Context Pack 拼出的上下文 |
| `/.well-known/agent-card.json` | GET | 默认启用 Context Pack 的 A2A Agent Card |
| `/api/a2a/{id}/agent-card.json` | GET | 指定 Context Pack 的 Agent Card |
| `/api/a2a/{id}/context` | GET | Bearer Token 授权后拉取上下文 |
| `/api/a2a/{id}/message:send` | POST | A2A REST 消息入口，返回授权上下文 |

> **安全**：存储后端永远只绑定 `127.0.0.1:8618`。局域网文件导入页通过 Caddy HTTP 白名单对外提供，避免自签名证书警告；MCP、手机和 A2A 仍使用 HTTPS。普通 `/api/*` 不对外代理，桌面管理接口仍只允许 loopback。HTTP 导入只应在可信局域网中使用。

```bash
# 搜索示例
curl -X POST http://127.0.0.1:8618/api/search \
  -H "Content-Type: application/json" \
  -d '{"query": "Linux GPU 驱动安装", "n_results": 5}'
```

手机 App 导入请求示例：

手机浏览器可打开 `https://192.168.1.86:8443/mobile`，保存桌面端生成的 App Token 后录音、上传文件或剪藏文本。首次使用前需先安装项目 CA 根证书。桌面端也可以复制配对链接，手机打开 `.../mobile#token=...` 后会自动保存 Token；Token 位于 URL fragment，不会发送到后端日志。

桌面设置里的「复制配对链接」会同时生成二维码；二维码由本机后端生成 SVG data URL，不调用第三方二维码服务。需要撤销手机访问时点「关闭并清空」，旧 App Token 会立即失效。

安装为 PWA 后，系统分享菜单可把网页、文字、图片、文件或录音分享到 `/mobile/share`。文本/URL 会预填剪藏；文件会暂存到浏览器 IndexedDB，页面读取已保存的 App Token 后自动上传到个人 AI 主机。

手机端所有采集入口都有待同步兜底：直传失败时，文件/录音 Blob 或剪藏文本会留在本机 IndexedDB，并在保存 App Token、网络恢复、打开结果页或手动点「同步」时重试。结果页会把本地待同步项排在顶部，避免采集内容因为临时断网丢失。

原生 App 壳位于 `frontend/mobile-native`，复用同一套 `/frontend/mobile` 页面。它适合手机上长期使用，App 内填写 Tailscale 节点地址即可连接个人 AI 主机：

```bash
cd frontend/mobile-native
npm install
npm run add:android
npm run open:android
```

改动移动端页面后同步到原生工程：

```bash
cd frontend/mobile-native
npm run sync
```

Android 原生壳已经接入 `@capgo/capacitor-share-target` 和 `@capacitor/share`。Manifest 注册了 `SEND`/`SEND_MULTIPLE` 分享入口，手机系统分享文本会进入剪藏，分享文件会复用移动端上传/录音接口进入自动向量化与 Wiki 整理流程；A2A 邀请可从 App 内直接调起系统分享面板。原生壳默认启用 CapacitorHttp，让 App 内请求走原生 HTTP 层，不需要把后端全局 CORS 放开；Android 明文 HTTP 仅建议用于 Tailscale/内网，公网应改成 HTTPS。打包 APK 需要本机安装 Java JDK 和 Android SDK。

```bash
curl -X POST https://192.168.1.86:8443/api/mobile/clips \
  -H "Authorization: Bearer <APP_TOKEN>" \
  -H "Content-Type: application/json" \
  -d '{"title": "会议纪要", "text": "今天讨论的重点..."}'
```

手机录音上传与结果轮询：

```bash
curl -X POST https://192.168.1.86:8443/api/mobile/recordings \
  -H "Authorization: Bearer <APP_TOKEN>" \
  -F "file=@meeting.m4a" \
  -F "title=客户会议录音"

curl https://192.168.1.86:8443/api/mobile/results/<DOC_ID> \
  -H "Authorization: Bearer <APP_TOKEN>"
```

A2A Context Pack 调用示例：

```bash
curl https://192.168.1.86:8443/api/a2a/<PACK_ID>/agent-card.json

curl -X POST https://192.168.1.86:8443/api/a2a/<PACK_ID>/message:send \
  -H "Authorization: Bearer <CONTEXT_TOKEN>" \
  -H "Content-Type: application/a2a+json" \
  -d '{"message":{"role":"ROLE_USER","parts":[{"text":"给我产品价格相关上下文"}],"messageId":"msg-1"}}'
```

## 项目结构

```
local-vector-db/
├── backend/          # Python 后端
│   ├── server.py     # FastAPI 服务
│   ├── embedder.py   # 嵌入模型 (BGE + CLIP) + 语音转写 (whisper)
│   ├── parser.py     # 文档解析
│   ├── video.py      # 视频处理 (ffmpeg 抽帧/抽音轨/探测)
│   ├── vector_store.py  # ChromaDB
│   ├── watcher.py    # 文件夹监控 + 后台索引池
│   └── config.py     # 配置
├── frontend/         # Electron 桌面应用、手机 PWA 和 Capacitor 原生壳
│   ├── mobile/       # 手机端 PWA / 原生壳复用页面
│   └── mobile-native/ # Capacitor Android/iOS 打包入口
├── data/             # 默认运行时数据根（不纳入 Git）
│   ├── db/           # SQLite：任务、派生内容、治理、Wiki 元数据等
│   ├── watch_folder/ # 监控目录与上传原文件
│   ├── wiki/         # Markdown Wiki Vault（Sources/Concepts/PARA folders）
│   ├── chroma_data/  # 向量数据库文件
│   └── video_frames/ # 视频关键帧及其它派生文件（可删后重建）
```

### 运行时数据、迁移与重置

默认情况下，用户资料、SQLite 数据库、Wiki、向量索引和派生文件均保存在
`data/`，与源码隔离且已被 Git 忽略。可通过 `CENTAURAI_DATABASE_DATA_ROOT`
将整个数据目录设到项目外，例如 `D:\MindOSData` 或 `/var/lib/centaurai-database`。

旧版本将数据直接写在项目根目录。升级前先停止后端，再执行迁移脚本；默认仅预览，
不会改动文件：

```bash
python scripts/migrate_data_root.py
# 确认预览无误后执行迁移；遇到同名目标会拒绝覆盖
python scripts/migrate_data_root.py --execute
```

迁移会将根目录的 `watch_folder/`、`wiki/`、`chroma_data/`、`memory/`、派生文件和
各 SQLite 数据库移动到 `data/`；其中 `wiki.sqlite3` 会归入 `data/db/`。脚本可重复执行，
不会覆盖目标数据。若需空白初始化，只需在停止服务后备份或移走整个 `data/` 目录，再重新启动服务。

> 音视频转写需 `faster-whisper`（`pip install faster-whisper`）与系统 `ffmpeg`；缺失时视频仅走帧+OCR，音频会跳过转写，不影响其它格式。

## 与 CentaurAI 集成

在半人马AI主程序设置 → 系统 → 个人记忆库中启用，发消息时即可自动检索相关个人记忆作为对话上下文。

## MCP 接入

本项目提供与 Agent 厂商无关的只读 MCP Server。任何支持 MCP `2025-11-25` Streamable HTTP 的 Agent 均可接入，不需要 Codex、Claude 或 CentaurAI 专用代码。

### 局域网 Streamable HTTP

| 模式 / 权限 | MCP URL | 工具 |
|---|---|---|
| 普通模式 | `https://192.168.1.86:8443/mcp/basic` | 知识库 4 项 + 记忆搜索 + Agent 上下文 |
| 知识库 | `https://192.168.1.86:8443/mcp/kb` | 4 个 `kb_*` 只读工具 |
| 完整私人记忆 | `https://192.168.1.86:8443/mcp/full` | 5 个 `memory_*` + 4 个 `kb_*` 工具 |

在桌面端「设置 → 连接与访问 → 标准 MCP 远程接入」中，普通模式和高级模式互斥：

1. 普通模式：保存连接证书，生成一个共享连接密钥，再复制通用 JSON 配置。密钥只显示一次。
2. 高级模式：配置 OAuth 管理员密码，或按 Agent 创建独立 Bearer Token，并选择知识库/完整记忆权限。
3. 切换模式会暂停另一种模式的已有连接，但不删除客户端和密钥。

Bearer 兼容配置使用标准 HTTP 字段：

```json
{
  "url": "https://192.168.1.86:8443/mcp/basic",
  "headers": {
    "Authorization": "Bearer <CLIENT_TOKEN>"
  }
}
```

OAuth 服务支持 Protected Resource Metadata、Authorization Server Metadata、动态客户端注册、Authorization Code + PKCE S256、刷新令牌轮换和撤销。长期 Bearer Token 与 OAuth Token 都绑定到具体 MCP 资源，不能跨权限端点复用。

服务安装/修复命令：

```bash
./scripts/setup_remote_mcp.sh
systemctl --user status centaurAI-memory-mcp.service centaurAI-memory-edge.service
```

> 当前用户服务没有绑定特权端口的系统权限，所以默认使用 `8443`。如需切换到 `443`，先执行 `sudo setcap cap_net_bind_service=+ep ~/.local/bin/caddy`，再执行 `CENTAUR_MCP_HTTPS_PORT=443 ./scripts/setup_remote_mcp.sh`。

### 本机 stdio

启动命令：

```bash
./start-mcp.sh
```

MCP 配置中可使用：

```json
{
  "mcpServers": {
    "local-vector-db": {
      "command": "/home/user/项目/centaurAI-database/start-mcp.sh"
    }
  }
}
```

可用工具：

| 工具 | 说明 |
|------|------|
| `memory_get_user_profile` | 读取 `memory/USER.md`，用于识别用户是谁、称呼和偏好 |
| `memory_get_context` | 获取共享记忆、Agent 专属导入记忆和最近日记摘要 |
| `memory_search` | 语义搜索记忆文件和日记 |
| `memory_list_files` | 列出记忆文件 |
| `memory_read_file` | 读取指定记忆文件 |
| `kb_search` | 搜索本地向量知识库 |
| `kb_get_stats` | 获取索引统计 |
| `kb_list_documents` | 列出已索引文档 |
| `kb_health` | 检查后端状态和能力 |

MCP Server 是薄适配层，所有工具请求都转发到 loopback FastAPI 后端。它不直接写 ChromaDB，也不修改任何 Agent 原生记忆文件。

## TokenManager Agent 记忆与统一身份同步

个人记忆库可以从 TokenManager 的本机 API 增量导入 Claude、Codex、Gemini、OpenCode、OpenClaw、Hermes 以及外部进程适配器提供的会话和 Agent 原生记忆；同一连接还会把统一身份发布给各个 Agent。

1. 在 TokenManager 的“设置 → 对话归档 → 本地 Agent 对话与记忆接入”中启用所需的自动归档、记忆抓取、本机 API 和“允许身份写入”。
2. 点击“复制令牌”。API 固定监听 `http://127.0.0.1:15722`，不会监听局域网地址。
3. 在个人记忆库“Agent 记忆/身份记忆 → 接入设置”中粘贴令牌、保存并测试连接；两处打开的是同一份设置。

首次连接会分别回填已有个人会话和 Agent 记忆，之后使用两个独立游标增量同步。对话写入 `memory/conversations/`，记忆按来源写入 `memory/imports/tokenmanager/`，两者都会进入语义检索；按 Agent 获取上下文时会聚合该 Agent 的 API 记忆文件。

私人记忆库优先使用 TokenManager 的 `memories` capability；旧版 TokenManager、不启用本机 API 或尚未配置令牌时才运行原有文件直扫。临时连接失败不会在两种模式间反复切换。Agent 源记忆删除后，对应 Markdown 和向量会随 delete 事件删除；会话仍按长期归档规则保留。

“身份记忆”中的 `SOUL.md`、`AGENTS.md`、`IDENTITY.md`、`USER.md` 是唯一身份来源。保存任一文件都会发布四文件完整快照；TokenManager 使用托管区块保留各 Agent 原有规则，并把身份映射到 OpenClaw 原生四文件、Hermes 的 Soul/User/Agents 文件以及 Claude、Codex、Gemini、OpenCode 的全局规则文件。同步失败不会回滚本地编辑，后台会继续重试最新版本。

## License

MIT

## Codeup 使用说明
### 3 分钟了解如何进入开发

欢迎使用云效代码管理 Codeup，通过阅读以下内容，你可以快速熟悉 Codeup ，并立即开始今天的工作。

### 提交**文件**

Codeup 支持两种方式进行代码提交：网页端提交，以及本地 Git 客户端提交。

* 如需体验本地命令行操作，请先安装 Git 工具，安装方法参见[安装Git](https://help.aliyun.com/document_detail/153800.html)。

* 如需体验 SSH 方式克隆和提交代码，请先在平台账号内配置 SSH 公钥，配置方法参见[配置 SSH 密钥](https://help.aliyun.com/document_detail/153709.html)。

* 如需体验 HTTP 方式克隆和提交代码，请先在平台账号内配置克隆账密，配置方法参见[配置 HTTPS 克隆账号密码](https://help.aliyun.com/document_detail/153710.html)。

现在，你可以在 Codeup 中提交代码文件了，跟着文档「[__提交第一行代码__](https://help.aliyun.com/document_detail/153707.html?spm=a2c4g.153710.0.0.3c213774PFSMIV#6a5dbb1063ai5)」一起操作试试看吧。

<img src="https://img.alicdn.com/imgextra/i3/O1CN013zHrNR1oXgGu8ccvY_!!6000000005235-0-tps-2866-1268.jpg" width="100%" />


### 进行代码检测

开发过程中，为了更好的维护你的代码质量，你可以开启 Codeup 内置开箱即用的「[代码检测服务](https://help.aliyun.com/document_detail/434321.html)」，开启后提交或合并请求的变更将自动触发检测，识别代码编写规范和安全漏洞问题，并及时提供结果报表和修复建议。

<img src="https://img.alicdn.com/imgextra/i2/O1CN01BRzI1I1IO0CR2i4Aw_!!6000000000882-0-tps-2862-1362.jpg" width="100%" />

### 开展代码评审

功能开发完毕后，通常你需要发起「[代码评审并执行合并](https://help.aliyun.com/document_detail/153872.html)」，Codeup 支持多人协作的代码评审服务，你可以通过「[保护分支设置合并规则](https://help.aliyun.com/document_detail/153873.html?spm=a2c4g.203108.0.0.430765d1l9tTRR#p-4on-aep-l5q)」策略及「[__合并请求设置__](https://help.aliyun.com/document_detail/153874.html?spm=a2c4g.153871.0.0.3d38686cJpcdJI)」对合并过程进行流程化管控，同时提供在线代码评审及冲突解决能力，让评审过程更加流畅。

<img src="https://img.alicdn.com/imgextra/i1/O1CN01MaBDFH1WWcGnQqMHy_!!6000000002796-0-tps-2592-1336.jpg" width="100%" />

### 成员协作

是时候邀请成员一起编写卓越的代码工程了，请点击左下角「成员」邀请你的小伙伴开始协作吧！

### 更多

Git 使用教学、高级功能指引等更多说明，参见[Codeup帮助文档](https://help.aliyun.com/document_detail/153402.html)。
