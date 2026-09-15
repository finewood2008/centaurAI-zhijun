# 0.1.22 工作区模型发布记录

## 安装包：已完成

- 正式 macOS arm64 包：`frontend/shell/release/Zhijun-0.1.22-mac-arm64.dmg` 和同名 ZIP。
- 使用现有 `package:mac-arm64` 流程，非 test flavor。Developer ID 签名、宿主与桌面测试、Electron E2E、构建、DMG 挂载和 ZIP 解压内容校验、搬迁启动冒烟通过。
- 当前发行配置未启用 Apple 公证；生产配网信任配置未提供，配网 Gate 保持关闭。不能据签名或本机冒烟宣称已通过 Apple 公证。
- DMG SHA-256：`95dc6a15286e7eccd1628e0671ac40824c841358e043bdd98a59f80bc785a40f`。
- ZIP SHA-256：`4d247e2c41dc4c14fb5b104352a6021c56e5c799aba5597a1e91338fd4ce9f9d`。
- 包内操作清单 SHA-256：`3b4d0a8169334cb8d6548a51f44583c5c5e56e13e54ae19832b25dd722b8722d`，与源码相同。

## 盒端：已暂存，尚未切换

目标为公司盒子 `192.168.0.7` 的 `zhijun-integration-0907`。
本次保留现场 Data Engine `2026.09.14-003-db7319022d60-cpu-amd64` 镜像，不改 Remote Agent、材料索引和推理服务。

已完成：

1. 使用源码发行审计逻辑收集 178 个文件，排除用户数据、密钥、测试和构建缓存；审计 175 个 Python 文件、1,317 个本地导入及操作清单资源。
2. 上传到 `/home/user/zhijun-model-release.FSqJl2`，源码归档 SHA-256 为 `c4ee8c2d60817378c6047e2d994a2ab0f84b086f408492d1c229fce121eb3032`。
3. 在当前部署镜像中以无网络、只读源码、临时独立工作区运行签名 dispatch 和模型设置验收，通过；未使用真实用户模型凭据或对话。
4. 新代码暂存至 `/srv/centauros-releases/company248-business-20260914-1/zhijun-0.1.22-model-direct-pending`。旧代码仍在 `zhijun`，尚未替换。

部署前检查发现并发维护：在本次脚本尚未调用 `docker stop` 时，容器已被外部流程停止；停止时间为北京时间 2026-09-14 22:58:30，约 23:00 再次启动。脚本在写入/停机前中止，未生成数据备份、未切换线上代码。需要确认另一维护流程结束后重新核验镜像、挂载、代码与容器身份，再进行一致性备份和切换。

现场私有目录保存 `container-before.json`，包含配置，不能下载进仓库或安装包。暂存代码和脚本不是已部署证明，也不是 manager / OTA 发行。

## 升级后配置与验收

- 客户端与盒端操作清单必须配套更新，9 条聊天模型设置操作由 `models` 改为 `domain`。
- 旧 DE 模型密钥不自动复制；需在知君重新配置在线模型，并确认新服务的来源授权。
- 本地推理只允许显式配置已验收的 NPU 服务；不得用现场通用 Ollama 名称推断硬件路径或自动开启 CPU 回退。
- 详见 [工作区模型配置](../workspace-model-configuration.md)。
- 切换后需核对全部源码哈希、容器健康、未认证访问拒绝及真实连接；不要自动重放历史对话或失败的本体任务。
