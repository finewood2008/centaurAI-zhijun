# MindOS Linux 服务器部署指南（Ubuntu、Debian 与 CentaurOS）

本文面向在基于 Debian 的 Linux 服务器上从零部署 MindOS（个人本地知识库），覆盖后端、Web 前端、模型、systemd 常驻服务，以及备份恢复与升级。已覆盖 Ubuntu 22.04+、Debian，以及 `ID_LIKE=ubuntu` 的定制发行版（例如 CentaurOS）。部署完成后后端只监听 `127.0.0.1:8618`，浏览器入口为 `http://127.0.0.1:8618/mindos/`。

本文不涵盖把普通管理 API 直接暴露公网。手机导入、远程 MCP 和局域网接入属于可选扩展，应在本地部署验收完成后再配置（见第 9 节）。

### 部署方式与代码保密边界

本项目可采用以下三种交付方式；第 4 至 6 节描述的是源码部署路径。

| 方式 | 服务器上保留的内容 | 适用场景 | 当前仓库支持情况 |
|---|---|---|---|
| 源码部署 | Git 工作区、Python 源码、依赖和模型 | 可信服务器，日常开发与快速修复 | 已支持，本文默认路径 |
| 离线运行包 | 精简运行树、预装虚拟环境、构建后的 Web、模型 | 无网或不希望保留 Git 历史/测试文件的可信服务器 | 已支持，见第 8.4 节 |
| 容器镜像 | 镜像层、运行依赖、模型或模型挂载目录 | 已有容器平台，需要版本化发布与回滚 | 已支持 Linux Docker host-network 部署，见《容器镜像部署指南》 |

离线运行包会排除 `.git`、测试、开发缓存和运行数据，但 Python 后端仍以可读取的 `.py` 文件形式包含在包内；它减少暴露面，不等于源码加密。容器镜像同样不能防止拥有服务器 root、容器运行权限或镜像读取权限的人员提取代码、依赖、模型或运行内存。

因此，若服务器由你或可信团队管理，可优先使用离线运行包以减少服务器上的开发痕迹；若服务器不可信且源码必须保密，不应把完整后端部署到该服务器，而应将敏感服务保留在受控环境，通过经过认证与授权的接口提供能力。

## 1. 部署范围

| 组件 | 是否必需 | 作用 |
|---|---:|---|
| Python 后端 | 是 | FastAPI、文档解析、ChromaDB、文件监控、检索和 MindOS API |
| BGE 文本嵌入模型 | 是 | 文本索引和语义检索 |
| MindOS Web 前端 | 是 | 浏览器版资料、知识图谱、三元组和治理界面 |
| Node.js | 构建 Web 前端时需要 | 构建 `frontend/mindos-web/dist` |
| ffmpeg/ffprobe | 音视频需要 | 视频抽帧、抽取音轨 |
| faster-whisper 模型 | 音视频需要 | 音频与视频转写 |
| Ollama + `qwen3:1.7b` | 推荐 | Wiki 整理、摘要、实体/关系三元组、问答；缺失时部分能力降级 |
| Caddy + HTTPS（可选） | 可选 | 局域网手机导入、远程 MCP、A2A 的对外网关 |

## 2. 资源与软件前置条件

- Ubuntu 22.04+、Debian 或兼容的定制发行版，x86_64（或 riscv64）。
- Python **3.11 x64**，Node.js 20 LTS 或更高版本。项目未验证 Python 3.14；不要用系统默认 Python 3.14 创建项目虚拟环境。
- 至少 16 GB 内存、4 核 CPU、50 GB 可用磁盘；启用 CLIP、Whisper 或 Ollama 时建议 32 GB 内存和更多磁盘。
- 首次安装 Python 依赖和下载模型时需要网络；完全离线部署请先在可联网的同架构机器制作依赖与模型包（见第 8 节）。

以下示例统一使用：

```text
应用目录：/opt/mindos/app
运行数据：/var/lib/mindos
服务账户：mindos
```

## 3. 安装系统依赖

```bash
sudo apt update
sudo apt install -y git curl ca-certificates build-essential ffmpeg libgl1 libglib2.0-0
```

### 3.1 确认系统与 Python 安装路径

先检查系统标识、CPU 架构与仓库是否提供 Python 3.11：

```bash
cat /etc/os-release
cat /etc/debian_version
uname -m
apt-cache policy python3.11 python3.11-venv
```

若仓库有对应软件包，安装并记录解释器路径：

```bash
sudo apt install -y python3.11 python3.11-venv python3.11-dev
python3.11 --version
```

仅对已确认的 Ubuntu 发行版可使用组织批准的 Ubuntu 软件源或内部镜像补充 Python 3.11。不要在 Debian、CentaurOS 或其他定制发行版上添加 `deadsnakes` PPA；这些系统可能缺失 Ubuntu 代号，`add-apt-repository` 会失败，混用软件源也可能破坏 APT 依赖。

### 3.2 Debian sid、CentaurOS 或仓库缺少 Python 3.11 时独立安装

当 `apt-cache policy` 没有候选版本时，将 Python 3.11 独立安装到 `/opt/python3.11`。这不会替换系统 Python、`python3` 命令或 APT 工具使用的 Python。示例使用 Python 3.11.11；企业离线环境应从内部制品库取得相同源码包并校验其完整性。

```bash
sudo apt install -y wget build-essential \
  libssl-dev zlib1g-dev libbz2-dev libreadline-dev libsqlite3-dev \
  libffi-dev liblzma-dev libgdbm-dev libncursesw5-dev uuid-dev tk-dev

cd /usr/local/src
sudo wget https://www.python.org/ftp/python/3.11.11/Python-3.11.11.tgz
sudo tar -xzf Python-3.11.11.tgz
cd Python-3.11.11
sudo ./configure --prefix=/opt/python3.11 --with-ensurepip=install
sudo make -j"$(nproc)"
sudo make altinstall
/opt/python3.11/bin/python3.11 --version
```

`make altinstall` 是必要约束：不要执行 `make install`，不要修改 `/usr/bin/python3` 的软链接，也不要卸载系统 Python 3.14。

安装 Node.js 20 时应使用与当前发行版匹配、且经组织认可的软件源或镜像。以下是 NodeSource 的示例；企业环境应替换为内部镜像或已批准的软件仓库：

```bash
curl -fsSL https://deb.nodesource.com/setup_20.x | sudo -E bash -
sudo apt install -y nodejs
```

确认版本满足要求：

```bash
node --version
npm --version
ffmpeg -version
```

Python 解释器根据第 3.1 或 3.2 节二选一确认：

```bash
python3.11 --version
# 或：
/opt/python3.11/bin/python3.11 --version
```

`ffmpeg`/`ffprobe` 缺失不影响 PDF、Office、Markdown、文本和图片的基础索引，但音视频能力会降级或不可用。

## 4. 源码部署：创建服务账户、目录并获取源码

```bash
sudo useradd --system --create-home --home-dir /var/lib/mindos --shell /usr/sbin/nologin mindos
sudo install -d -o mindos -g mindos /opt/mindos /var/lib/mindos
sudo -u mindos git clone <REPOSITORY_URL> /opt/mindos/app
```

服务账户必须对 `/opt/mindos/app` 和 `/var/lib/mindos` 有读写权限。模型放在应用目录，运行数据放在 `/var/lib/mindos`，两者不要混放。

## 5. 安装后端、构建前端和准备模型

根据第 3 节的安装方式，先在当前终端选择项目解释器。二者只能选其一：

```bash
# A. 系统仓库提供 Python 3.11 时：
MINDOS_PYTHON=/usr/bin/python3.11

# B. Debian sid、CentaurOS 或其他缺少 Python 3.11 的系统（改用这一行，并不要设置 A）：
# export MINDOS_PYTHON=/opt/python3.11/bin/python3.11
```

确认后再创建虚拟环境：

```bash
sudo -u mindos env MINDOS_PYTHON="$MINDOS_PYTHON" bash -lc '
  cd /opt/mindos/app
  "$MINDOS_PYTHON" -m venv backend/.venv
  backend/.venv/bin/python -m pip install --upgrade pip
  backend/.venv/bin/python -m pip install -r backend/requirements-lock.txt
  cd frontend/mindos-web
  npm ci
  npm run build
'
```

> 必须使用 `requirements-lock.txt`，其中锁定了 `chromadb==1.5.9` 等已验证版本。`pywin32` 已在锁文件中标记为仅 Windows 安装，Linux 不会尝试安装它。

构建成功后必须确认：

```bash
test -f /opt/mindos/app/frontend/mindos-web/dist/index.html
```

> 前端命令约定（阶段 A）：`frontend/package.json` 的 `npm start` 不再隐式启动
> Electron，而是拒绝执行并提示选择 `npm run web`（Web 开发，只启动 Vite）或
> `npm run desktop`（Electron 桌面）。服务器生产模式无需 Electron：由后端在
> `/mindos` 提供构建产物，仅 `npm run web:build` 构建 `dist`。桌面仅适合本机使用
> （`start-desktop.sh`），不应在服务器上启动。

### 5.1 必需的 BGE 文本模型

代码将文本模型固定读取为：

```text
backend/models_cache/BAAI/bge-small-zh-v1.5/
```

该目录必须至少包含模型配置和权重（例如 `config.json` 与 `model.safetensors` 或 `pytorch_model.bin`）。这是文本索引的必需资产；缺失时不要启动索引任务。

推荐从已验证机器复制完整模型目录；在线下载时：

```bash
sudo -u mindos /opt/mindos/app/backend/.venv/bin/hf download \
  BAAI/bge-small-zh-v1.5 \
  --local-dir /opt/mindos/app/backend/models_cache/BAAI/bge-small-zh-v1.5
test -f /opt/mindos/app/backend/models_cache/BAAI/bge-small-zh-v1.5/config.json
```

### 5.2 可选视觉与语音模型

- 普通图片 OCR 使用 Python 依赖内的 RapidOCR；纯视觉检索还需要本地 Chinese-CLIP 权重，缺失时自动关闭该能力。
- 语音转写默认使用 faster-whisper `small`。首次处理音频/视频时，若 `backend/whisper_models/` 下没有本地模型且机器可联网，会尝试下载；离线环境应预先复制 `backend/whisper_models/faster-whisper-small/`。

模型目录属于程序资产，不属于运行数据根；升级模型或更换嵌入模型后，应执行全量重建索引。

## 6. 配置并启用 systemd 服务

创建 `/etc/systemd/system/mindos.service`：

```ini
[Unit]
Description=MindOS local knowledge service
After=network.target

[Service]
Type=simple
User=mindos
Group=mindos
WorkingDirectory=/opt/mindos/app/backend
Environment=CENTAURAI_DATABASE_DATA_ROOT=/var/lib/mindos
Environment=PYTHONNOUSERSITE=1
Environment=PYTHONDONTWRITEBYTECODE=1
Environment=TOKENIZERS_PARALLELISM=false
ExecStart=/opt/mindos/app/backend/.venv/bin/python server.py
Restart=on-failure
RestartSec=5
TimeoutStopSec=45
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
```

> `CENTAURAI_DATABASE_DATA_ROOT` 指定独立运行数据根。数据根会保存：`db/`（SQLite 元数据、任务和派生产物）、`watch_folder/`（原始导入文件和 `.mindos_uploads`）、`chroma_data/`（ChromaDB 索引）、`wiki/`（Markdown 知识库）、`memory/`（Agent 记忆）、`video_frames/`（视频帧派生文件）。同一数据根只能由一个后端或维护脚本访问。

加载并启动服务：

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now mindos.service
sudo systemctl status mindos.service --no-pager
curl --fail http://127.0.0.1:8618/api/health
```

查看运行日志：

```bash
sudo journalctl -u mindos.service -f
```

首次启动会创建数据根、SQLite 文件和空的 ChromaDB 目录，并启动文件监控与后台任务。首次加载嵌入模型时可能需要数十秒。

在服务器浏览器或通过仅允许可信来源的反向代理访问 `http://127.0.0.1:8618/mindos/`。

### 首次导入验收

在数据根监控目录放入一个文本文件：

```bash
sudo -u mindos sh -c 'echo "MindOS 部署验收材料。" > /var/lib/mindos/watch_folder/部署验收.txt'
```

等待日志出现索引成功，再确认：

```bash
curl --fail http://127.0.0.1:8618/api/documents | python3 -m json.tool
```

如需验证文本检索，可在页面搜索“部署验收材料”。不要在首轮验收时并发启动全量重建、回填脚本或第二个后端。

## 7. 可选：本地 AI 能力（Ollama）

Ollama 可以安装在同一台 Linux 主机并运行 `qwen3:1.7b`：

```bash
ollama pull qwen3:1.7b
curl http://127.0.0.1:11434/api/tags
sudo systemctl restart mindos.service
```

默认服务地址为 `http://127.0.0.1:11434`，默认模型名是 `qwen3:1.7b`。如需替换模型，在 systemd 服务中追加环境变量：

```ini
Environment=CENTAUR_LOCAL_OLLAMA_MODEL=qwen3:1.7b
```

Wiki 自动整理与材料识别/摘要/实体/关系三元组/标签/草稿共用同一个 Ollama 服务地址；需要使用可信内网中的 Ollama 时，追加：

```ini
Environment=CENTAUR_LOCAL_OLLAMA_URL=http://192.168.1.20:11434
```

Wiki 自动整理与材料识别/摘要/实体/关系三元组/标签/草稿也共用该模型名。旧 `CENTAUR_RECOGNITION_AI_OLLAMA_URL`、`CENTAUR_RECOGNITION_AI_MODEL` 和 `CENTAUR_WIKI_AI_MODEL` 仍兼容，但新部署应使用统一变量。

Ollama 未安装不会阻断基础索引，但摘要、Wiki 整理、问答和关系三元组将退化到本地规则或不可用状态。问答可选使用 OpenAI 兼容接口（通过环境变量注入 base URL 与 API Key，不要把密钥写入仓库、脚本或文档），但 Wiki 自动整理仍只使用本机 Ollama。

## 8. 备份、恢复、升级与离线运行包

### 8.1 一致性备份

必须先停止后端，再执行备份。ChromaDB 索引由 SQLite 元数据和多个 HNSW 二进制文件共同组成，不能只备份 `chroma.sqlite3`。

```bash
sudo systemctl stop mindos.service
sudo -u mindos env CENTAURAI_DATABASE_DATA_ROOT=/var/lib/mindos \
  /opt/mindos/app/backend/.venv/bin/python /opt/mindos/app/scripts/backup_runtime_data.py \
  --output-dir /var/lib/mindos-backups
sudo systemctl start mindos.service
```

校验备份并在隔离副本上执行 Chroma 读取探测：

```bash
sudo -u mindos /opt/mindos/app/backend/.venv/bin/python \
  /opt/mindos/app/scripts/verify_runtime_backup.py \
  --backup /var/lib/mindos-backups/mindos-data-YYYYMMDD-HHMMSS
```

### 8.2 新机恢复

1. 按本文第 3 至 6 节部署相同代码、Python 依赖和模型。
2. 停止后端，确保目标数据根为空。
3. 将备份目录下的 `data/` **整体**复制为目标数据根（例如 `/var/lib/mindos`）。
4. 使用第 8.1 节的校验脚本检查备份副本，再启动后端。

不要单独复制 `chroma.sqlite3` 或 HNSW 目录中的某个文件。

### 8.3 升级

```bash
sudo systemctl stop mindos.service
# （可选）先执行第 8.1 节的备份
sudo -u mindos git -C /opt/mindos/app pull
sudo -u mindos bash -lc '
  cd /opt/mindos/app
  backend/.venv/bin/python -m pip install -r backend/requirements-lock.txt
  cd frontend/mindos-web
  npm ci
  npm run build
'
sudo systemctl start mindos.service
```

不要删除 `/var/lib/mindos`；它保存原文件、SQLite、Wiki 和 ChromaDB 索引。

### 8.4 离线运行包

离线运行包是源码部署的替代交付方式。应在受控构建机（同架构、同一 Linux 发行版族）完成 Python 依赖安装、BGE 模型下载、MindOS Web 构建和冒烟验证后再打包。不要在未验证的运行服务器上构建包。

```bash
cd /opt/mindos/app
backend/.venv/bin/python scripts/package_runtime.py --target linux-x86_64 --output-dir release
sha256sum release/*.tar.gz
```

打包脚本会校验运行 Python 的 Linux 架构、预装虚拟环境、BGE 模型与 `frontend/mindos-web/dist`，并排除 Git 历史、测试、开发缓存和所有可变运行数据。它不加密 Python 代码，不能作为对不可信服务器管理员的源码保护措施。

将 tar 包和对应 SHA-256 值通过受控渠道传到服务器后，先校验摘要，再按以下方式部署。示例不覆盖已有数据根；升级前仍须先执行一致性备份：

```bash
echo '<SHA256>  /path/to/centaurai-database-<VERSION>-linux-x86_64-runtime.tar.gz' | sha256sum -c -
sudo install -d -o mindos -g mindos /opt/mindos/releases /var/lib/mindos
sudo -u mindos tar -xzf /path/to/centaurai-database-<VERSION>-linux-x86_64-runtime.tar.gz \
  -C /opt/mindos/releases
sudo -u mindos ln -sfn \
  /opt/mindos/releases/centaurai-database-<VERSION>-linux-x86_64-runtime \
  /opt/mindos/app

sudo -u mindos env CENTAURAI_DATABASE_DATA_ROOT=/var/lib/mindos \
  /opt/mindos/app/run.sh
```

首次确认服务能启动后，使用第 6 节相同的 systemd 安全配置，但把运行入口改为运行包的 `run.sh`：

```ini
WorkingDirectory=/opt/mindos/app
ExecStart=/opt/mindos/app/run.sh
```

`CENTAURAI_DATABASE_DATA_ROOT` 必须继续放在包外。运行包内已包含 Python 虚拟环境与 `frontend/mindos-web/dist`，服务器无需安装 Python 3.11、Node.js、npm 或执行前端构建；是否还需要系统级 `ffmpeg`、图形库等依赖，取决于启用的音视频/OCR 能力，仍按第 3 节准备。

> 当前项目已提供 Linux Docker host-network 容器交付文件（`Dockerfile`、`compose.yaml`、`.dockerignore` 和 `docker/entrypoint.sh`）。模型交付、数据卷、健康检查、端口仅绑定 loopback、升级和备份流程见《容器镜像部署指南》；请勿将该配置直接用于 Docker Desktop 或改为 `ports:` 映射。

## 9. 可选：局域网手机导入与远程 MCP

后端本身永远只监听 `127.0.0.1:8618`。需要手机导入、远程 MCP 或 A2A 时，通过 Caddy 作为 HTTPS 网关对外提供服务（默认端口 `8443`）。项目提供了配置脚本：

```bash
sudo -u mindos /opt/mindos/app/scripts/setup_remote_mcp.sh
systemctl --user status centaurAI-memory-mcp.service centaurAI-memory-edge.service
```

首次使用前需安装项目 CA 根证书。不要把普通 `/api/*` 代理到公网；HTTP 导入只应在可信局域网中使用。当前用户服务默认使用 `8443`；如需切换到 `443`，先执行 `sudo setcap cap_net_bind_service=+ep ~/.local/bin/caddy`，再执行 `CENTAUR_MCP_HTTPS_PORT=443 ./scripts/setup_remote_mcp.sh`。

## 10. 常见故障

| 现象 | 检查与处理 |
|---|---|
| `/mindos/` 返回 404 或空白 | 执行 `cd /opt/mindos/app/frontend/mindos-web; npm ci; npm run build`，确认 `dist/index.html` 存在。 |
| 后端启动提示数据根被占用 | 检查是否已有后端、回填或维护脚本使用同一数据根；停止旧进程，勿手工删除锁文件。 |
| 文本索引时模型加载失败 | 检查 `backend/models_cache/BAAI/bge-small-zh-v1.5/` 是否有完整权重和配置。 |
| 音视频没有转写 | 检查 `ffmpeg`、`ffprobe`、faster-whisper 依赖及模型下载状态。 |
| 三元组显示规则降级 | 检查 `ollama list` 是否包含 `qwen3:1.7b`，以及 `http://127.0.0.1:11434/api/tags` 是否可访问。 |
| ChromaDB `Error loading hnsw index` | 停止全部访问该数据根的进程；备份整个 `chroma_data/`，将其改名保存后重启后端，从 `watch_folder/.mindos_uploads/` 的原始文件执行全量重建。不要只删除或替换单个 HNSW 文件。 |
| 端口 8618 被占用 | 仅停止冲突的本机服务后重启。当前代码端口在 `backend/config.py` 中固定为 `8618`。 |
| `python3.11` 无法由 APT 定位 | 执行第 3.1 节的系统和仓库诊断。Debian sid、CentaurOS 等定制系统不要添加 deadsnakes PPA；按第 3.2 节独立安装到 `/opt/python3.11`。 |
| `add-apt-repository` 报 `VERSION_CODENAME` | `/etc/os-release` 缺少 Ubuntu 代号，说明不是可直接使用 Ubuntu PPA 的环境。不要伪造代号或混用软件源，改用第 3.2 节的独立安装方案。 |

## 11. 上线验收清单

- [ ] 后端 `GET /api/health` 成功。
- [ ] `http://127.0.0.1:8618/mindos/` 可打开。
- [ ] 已导入一份文本资料并能在资料列表与搜索中看到。
- [ ] 数据根位于源码目录外，并已确认对 `mindos` 账户可写。
- [ ] 已验证同一数据根不存在第二个后端实例。
- [ ] 已执行一次停止服务后的完整数据快照与校验。
- [ ] 如启用音视频，`ffmpeg`、`ffprobe` 和 Whisper 已验证。
- [ ] 如需 AI 摘要、实体和关系三元组，Ollama 与 `qwen3:1.7b` 已验证。
- [ ] systemd 服务已设置开机自启，且日志无报错。
