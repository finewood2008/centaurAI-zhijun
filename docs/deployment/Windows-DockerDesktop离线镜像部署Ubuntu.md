# MindOS Windows Docker Desktop 离线镜像部署 Ubuntu

本文记录在 Windows Docker Desktop 构建 MindOS 镜像、导出为 tar 文件、上传到 Ubuntu 并以 Docker Compose 运行的流程。目标主机为 Linux x86_64（`amd64`）。

## 1. 构建机准备

在 Windows 的项目根目录执行。Docker Desktop 必须处于 Linux containers 模式，且能连接 Docker Hub。

```powershell
docker pull docker/dockerfile:1
docker pull node:20-bookworm-slim
docker pull python:3.11-slim-bookworm
docker build --platform linux/amd64 -t mindos:1.0.0 .
```

若 `docker pull` 出现 `registry-1.docker.io:443` 超时，先恢复 Docker Desktop 的网络、DNS 或代理配置；基础镜像下载成功后再重新构建。`apt` 和 `pip` 步骤首次构建可能耗时较长，成功完成的 Docker 层会被缓存。

确认镜像：

```powershell
docker images mindos:1.0.0
```

## 2. 导出并上传镜像

在执行命令的当前目录生成 tar 文件：

```powershell
docker save -o .\mindos-1.0.0-linux-amd64.tar mindos:1.0.0
Get-Item .\mindos-1.0.0-linux-amd64.tar
```

PowerShell 提示符重新出现且 tar 文件大小稳定，即表示导出完成。将以下文件上传到 Ubuntu 的 `/opt/mindos/`（可先上传到用户家目录，再用 `sudo mv` 移动）：

```text
mindos-1.0.0-linux-amd64.tar
compose.yaml
.env
```

示例：

```powershell
scp .\mindos-1.0.0-linux-amd64.tar user@服务器IP:/home/user/
scp .\compose.yaml user@服务器IP:/home/user/
scp .\.env user@服务器IP:/home/user/
```

## 3. Ubuntu 主机准备

确认架构、Docker Engine 和 Compose：

```bash
uname -m
docker --version
docker compose version
```

`uname -m` 应输出 `x86_64`。创建部署、数据与模型目录：

```bash
sudo mkdir -p /opt/mindos
sudo mkdir -p /srv/mindos/data
sudo mkdir -p /srv/mindos/models-cache
sudo mkdir -p /srv/mindos/models-cache-ms
sudo mkdir -p /srv/mindos/whisper-models
sudo chown -R 10001:10001 /srv/mindos/data
```

若文件先上传到家目录：

```bash
sudo mv ~/mindos-1.0.0-linux-amd64.tar ~/compose.yaml ~/.env /opt/mindos/
```

允许登录用户调用 Docker（只应授予受信任用户）：

```bash
sudo usermod -aG docker user
newgrp docker
docker ps
```

`newgrp docker` 只对当前终端生效；重新登录 SSH 后组权限会自动生效。

## 4. 配置离线运行 Compose

编辑 `/opt/mindos/compose.yaml`：

- 将镜像设置为待导入的标签：`image: mindos:1.0.0`。
- 删除 `build:`、`context:` 和 `dockerfile:` 三行，避免服务器尝试重新构建。
- 保留 `network_mode: host`，不要添加 `ports:`。

服务配置的关键部分如下：

```yaml
services:
  mindos:
    image: mindos:1.0.0
    container_name: mindos
    restart: unless-stopped
    network_mode: host
    environment:
      CENTAURAI_DATABASE_DATA_ROOT: /var/lib/mindos
      TOKENIZERS_PARALLELISM: "false"
```

`/opt/mindos/.env` 使用 Linux 绝对路径：

```dotenv
MINDOS_DATA_ROOT=/srv/mindos/data
MINDOS_MODELS_ROOT=/srv/mindos/models-cache
MINDOS_MODELS_MS_ROOT=/srv/mindos/models-cache-ms
MINDOS_WHISPER_MODELS_ROOT=/srv/mindos/whisper-models
```

## 5. 放置必需模型

模型必须位于宿主机的挂载源目录，而不是宿主机的 `/opt/mindos/app/...` 目录。正确结构为：

```text
/srv/mindos/models-cache/BAAI/bge-small-zh-v1.5/config.json
/srv/mindos/models-cache/BAAI/bge-small-zh-v1.5/model.safetensors
# 或 pytorch_model.bin
```

若旧服务器部署已有模型，可复制：

```bash
sudo mkdir -p /srv/mindos/models-cache/BAAI
sudo rsync -a \
  /home/user/local-vector-db/backend/models_cache/BAAI/bge-small-zh-v1.5/ \
  /srv/mindos/models-cache/BAAI/bge-small-zh-v1.5/
sudo chown -R root:root /srv/mindos/models-cache
sudo chmod -R a+rX /srv/mindos/models-cache
```

Compose 会将宿主机 `/srv/mindos/models-cache` 挂载为容器内 `/opt/mindos/app/backend/models_cache`。模型放错宿主机路径时，日志会显示容器内路径 `.../models_cache/BAAI/bge-small-zh-v1.5 not found`。

`models-cache-ms` 与 `whisper-models` 是可选模型目录，可保持为空。数据目录 `/srv/mindos/data` 由容器 UID `10001` 写入；不要把整个 `/srv` 目录交给登录用户。

## 6. 导入并启动

```bash
cd /opt/mindos
docker load -i mindos-1.0.0-linux-amd64.tar
docker images mindos:1.0.0
docker compose config
docker compose up -d
docker compose ps
curl http://127.0.0.1:8618/api/health
```

预期 `docker compose ps` 显示 `healthy`，健康检查返回 JSON 中的 `"status":"ok"`。本机 Web 页面为：

```text
http://127.0.0.1:8618/mindos/
```

如出现 `/usr/bin/env: 'bash\\r': No such file or directory`，说明使用了旧镜像中的 Windows CRLF 脚本。请在已包含当前 Dockerfile 修复的源码上重新构建并导出镜像。当前 Dockerfile 会在构建时将入口脚本转换为 Linux LF 换行。

## 7. 常见故障

### Docker socket 权限被拒绝

```text
permission denied while trying to connect to the Docker daemon socket
```

使用 `sudo docker ...` 临时执行，或将用户加入 Docker 组后重新登录：

```bash
sudo usermod -aG docker user
newgrp docker
```

### 8618 端口已占用

host network 使容器直接监听宿主机 `127.0.0.1:8618`。定位占用者：

```bash
sudo ss -ltnp 'sport = :8618'
ps -fp <PID>
sudo tr '\0' ' ' < /proc/<PID>/cmdline; echo
```

停止确认无用的旧源码服务后，再启动容器。不要杀掉当前容器的 Python 进程；可用以下命令确认当前容器 PID：

```bash
docker inspect -f '{{.State.Status}} pid={{.State.Pid}}' mindos
```

### 模型预热失败

```text
Path /opt/mindos/app/backend/models_cache/BAAI/bge-small-zh-v1.5 not found
```

检查宿主机目录，不要只检查容器内或 `/opt/mindos/app`：

```bash
ls -la /srv/mindos/models-cache/BAAI/bge-small-zh-v1.5
docker exec mindos ls -la /opt/mindos/app/backend/models_cache/BAAI/bge-small-zh-v1.5
```

模型补齐后重启容器：

```bash
cd /opt/mindos
docker compose restart mindos
```

## 8. 可选：通过 Nginx 开放完整 Web 页面到局域网

应用仍只监听 `127.0.0.1:8618`。Nginx 运行在同一台 Ubuntu 上，将局域网地址转发给本机服务。以下示例使用服务器 LAN IP `192.168.31.248` 和端口 `8080`；请按实际网络替换。

```bash
sudo apt-get update
sudo apt-get install -y nginx
sudo systemctl enable --now nginx
sudo nano /etc/nginx/sites-available/mindos-lan
```

写入：

```nginx
server {
    listen 192.168.31.248:8080;
    server_name _;

    client_max_body_size 500m;

    location = / {
        return 302 /mindos/;
    }

    location = /mindos {
        return 301 /mindos/;
    }

    location ^~ /mindos/ {
        proxy_pass http://127.0.0.1:8618;
        proxy_http_version 1.1;
        proxy_set_header Host $host;
    }

    location ^~ /api/ {
        proxy_pass http://127.0.0.1:8618;
        proxy_http_version 1.1;
        proxy_request_buffering off;
        proxy_set_header Host $host;
    }
}
```

启用并验证：

```bash
sudo ln -s /etc/nginx/sites-available/mindos-lan /etc/nginx/sites-enabled/mindos-lan
sudo nginx -t
sudo systemctl reload nginx
sudo ss -ltnp 'sport = :8080'
```

局域网访问地址：

```text
http://192.168.31.248:8080/mindos/
```

该示例将完整页面及其 `/api/` 请求交给所有能够连接该地址的设备。生产环境应至少限制可信子网，并优先配置 HTTPS 与认证；不要将该端口直接映射到公网。

## 9. 升级与备份

升级时构建新标签、导出并上传新的 tar 文件，在服务器执行：

```bash
cd /opt/mindos
docker load -i mindos-新版本-linux-amd64.tar
# 将 compose.yaml 中 image 标签改为新版本后：
docker compose up -d
docker compose logs --tail=100 mindos
```

升级只替换镜像，必须保留 `/srv/mindos/data` 与模型目录。备份前停止容器：

```bash
cd /opt/mindos
docker compose stop mindos
sudo tar -C /srv/mindos -czf /srv/mindos-backup-$(date +%Y%m%d-%H%M%S).tar.gz data
docker compose start mindos
```
