#!/bin/bash
set -e

echo "===== 半人马AI 个人记忆库安装 ====="
echo ""

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BACKEND_DIR="$PROJECT_DIR/backend"
DATA_ROOT="${CENTAURAI_DATABASE_DATA_ROOT:-$PROJECT_DIR/data}"
CACHE_DIR="$BACKEND_DIR/models_cache"
MODELS_MS_DIR="$BACKEND_DIR/models_cache_ms"

# 1. Python 虚拟环境
echo "[1/7] 创建 Python 虚拟环境..."
cd "$BACKEND_DIR"
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip -q

# 2. 安装 Python 依赖（直连 HF 镜像，不用代理）
# P0-5：优先安装锁定版本（chromadb==1.5.9 等精确 pin），保证开发/测试/生产
# 环境一致；锁文件缺失时回退宽松 requirements.txt 并告警。
echo "[2/7] 安装 Python 依赖..."
if [ -f requirements-lock.txt ]; then
    pip install -r requirements-lock.txt
else
    echo "警告: requirements-lock.txt 不存在，回退到宽松版本 requirements.txt。" >&2
    echo "警告: 新环境可能安装不同 Chroma 版本，P0-5 版本验证将无法复现。" >&2
    pip install -r requirements.txt
fi

# 3. 下载模型
echo "[3/7] 下载嵌入模型..."

# BGE 文本模型（modelscope 直连）
echo "  - BGE-small-zh (24MB)..."
ALL_PROXY="" HTTP_PROXY="" HTTPS_PROXY="" python -c "
import os
for v in list(os.environ.keys()):
    if 'proxy' in v.lower(): del os.environ[v]
from modelscope import snapshot_download
snapshot_download('BAAI/bge-small-zh-v1.5', cache_dir='$CACHE_DIR')
"

# Chinese-CLIP 视觉检索模型（图片与中文查询处于同一向量空间）
echo "  - Chinese-CLIP ViT-B/16 (约 753MB)..."
ALL_PROXY="" HTTP_PROXY="" HTTPS_PROXY="" python -c "
import os
for v in list(os.environ.keys()):
    if 'proxy' in v.lower(): del os.environ[v]
from huggingface_hub import snapshot_download
snapshot_download(
    'OFA-Sys/chinese-clip-vit-base-patch16',
    local_dir='$MODELS_MS_DIR/OFA-Sys/chinese-clip-vit-base-patch16',
    allow_patterns=['config.json', 'preprocessor_config.json', 'vocab.txt',
                    'pytorch_model.bin', 'README.md'],
)
"

# 4. 创建必要目录
echo "[4/7] 创建数据目录..."
mkdir -p "$DATA_ROOT/db" "$DATA_ROOT/chroma_data" "$DATA_ROOT/watch_folder"
DATA_ROOT="$(cd "$DATA_ROOT" && pwd -P)"

# 5. 安装 Electron
echo "[5/7] 安装 Electron 前端..."
cd "$PROJECT_DIR/frontend"
npm install

# 6. 配置自启和快捷方式
echo "[6/7] 配置开机自启和桌面快捷方式..."

mkdir -p ~/.config/systemd/user/ ~/.local/share/applications ~/桌面
cat > ~/.config/systemd/user/centaurAI-vector-db.service << EOF
[Unit]
Description=半人马AI 个人记忆库后端
After=network.target

[Service]
Type=simple
ExecStart=$BACKEND_DIR/.venv/bin/python $BACKEND_DIR/server.py
WorkingDirectory=$BACKEND_DIR
Environment="CENTAURAI_DATABASE_DATA_ROOT=$DATA_ROOT"
Restart=on-failure
RestartSec=10

[Install]
WantedBy=default.target
EOF

systemctl --user daemon-reload
systemctl --user enable centaurAI-vector-db.service

cat > ~/.config/systemd/user/centaurAI-memory-gui.service << EOF
[Unit]
Description=半人马AI 个人记忆库桌面界面
After=graphical-session.target centaurAI-vector-db.service
Wants=centaurAI-vector-db.service

[Service]
Type=exec
WorkingDirectory=$PROJECT_DIR/frontend
ExecStart=$PROJECT_DIR/frontend/node_modules/electron/dist/electron --no-sandbox .
UnsetEnvironment=ELECTRON_RUN_AS_NODE
Restart=on-failure
RestartSec=2

[Install]
WantedBy=graphical-session.target
EOF

cat > ~/.local/share/applications/centaurai-personal-memory.desktop << EOF
[Desktop Entry]
Name=半人马AI 个人记忆库
Name[zh_CN]=半人马AI 个人记忆库
Comment=让不同 AI 调用同一份属于你的个人记忆
Exec=/usr/bin/systemctl --user restart centaurAI-memory-gui.service
TryExec=/usr/bin/systemctl
Icon=$PROJECT_DIR/frontend/assets/icon-256.png
Terminal=false
Type=Application
Categories=Utility;
StartupWMClass=local-vector-db
EOF

cp ~/.local/share/applications/centaurai-personal-memory.desktop ~/桌面/centaurai-向量数据库.desktop
chmod +x ~/桌面/centaurai-向量数据库.desktop

echo "[7/7] 配置标准 MCP 远程服务..."
CENTAURAI_DATABASE_DATA_ROOT="$DATA_ROOT" "$PROJECT_DIR/scripts/setup_remote_mcp.sh"

echo ""
echo "===== 安装完成! ====="
echo "后端服务: systemctl --user status centaurAI-vector-db.service"
echo "MCP 服务: systemctl --user status centaurAI-memory-mcp.service centaurAI-memory-edge.service"
echo "桌面应用: 双击桌面上的「半人马AI 个人记忆库」图标"
echo "Web API:  http://127.0.0.1:8618"
echo "监控目录: $DATA_ROOT/watch_folder"
echo ""
echo "使用方法: 把 PDF/Word/MD/图片 放入 $DATA_ROOT/watch_folder/"
echo "         即可自动向量化，支持语义搜索"
