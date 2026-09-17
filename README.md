# 知君

知君是一个有记忆边界、可核对、不会替用户决定的长期思考伙伴。它通过对话理解用户，把尚待确认的推测与用户已经确认的理解分开，并保留来源和模型使用回执。

当前文档从 [docs/README.md](docs/README.md) 开始。产品定义见 [PRODUCT.md](docs/product/PRODUCT.md)，系统边界见 [architecture.md](docs/development/architecture.md)。历史计划、部署回执和旧原型通过 Git 历史追溯，不再混入现行文档。

## 仓库结构

```text
backend/                         FastAPI、领域逻辑、存储和盒端 worker
frontend/mindos-web/             Vue 3 Web/桌面产品界面
frontend/shell/                  Electron 宿主、连接、权限和打包
frontend/shared/                 桌面公开合同与操作目录
docs/                            当前产品、架构、功能和机器契约
```

## 本机开发

后端使用 Python 3.11。前端和桌面打包使用 Node 22。

```bash
python3.11 -m venv backend/.venv
backend/.venv/bin/pip install -r backend/requirements.txt
npm --prefix frontend/mindos-web ci
bash zhijun.sh start
```

`zhijun.sh status` 查看状态，`zhijun.sh stop` 只停止该入口创建且身份仍匹配的进程。数据、端口和测试隔离说明见 [本机开发文档](docs/development/local-runtime.md)。

## 验证

```bash
backend/.venv/bin/python scripts/run_tests.py --isolated-modules -- -q
npm --prefix frontend/mindos-web run test:all
npm --prefix frontend/mindos-web run typecheck
npm --prefix frontend/mindos-web run build
```

## 桌面应用

开发启动、真实连接、安全边界和打包命令统一维护在 [frontend/shell/README.md](frontend/shell/README.md)。macOS ARM64 正式打包入口：

```bash
npm --prefix frontend/shell run package:mac-arm64
```

该流水线执行测试、桌面构建、版本递增、资源准备、Developer ID 签名、DMG/ZIP 校验和搬迁启动冒烟测试。是否公证以 Electron Builder 当前配置为准，不从文件名推断。

## Admin 与生产连接

Admin 已正式发布上线，是生产环境的账号、应用登记与授权控制面。知君桌面应用使用固定的 `applicationId=zhijun-desktop`、`purpose=zhijun.workspace` 和 `remote.p2p` 权限申请，通过 Electron main 中安装的 Connectivity/Consumer SDK 完成设备认领、短期连接票据和会话建立；Renderer 不直接调用 Admin 接口，也不持有长期凭据。

Admin 上线只表示控制面已经可用，不等同于桌面安装包、盒端业务服务或 NPU 推理运行时完成同一次发布。这些发布单元必须分别核对版本与健康状态。

## 模型与 NPU 边界

桌面端负责让用户明确选择本地或在线模型，并执行资料授权；在线失败不会自动回落本地。某一轮实际使用的通道以消息回执的 `provider/model/external` 和盒端路由审计为准，不能以模型正文中的自称判断。

盒端推理服务、模型加载和 NPU-only 限制由 CentaurOS 管理。本仓库不应启动 CPU 推理服务，也不实现 CPU fallback。当前部署要求只使用 NPU 时，必须在 CentaurOS 的运行配置和健康检查中落实。

## 关键数据边界

- 对话、理解、章程、判断、事项和成果使用各自的领域存储；不要用“清空一个数据根”代替正式清理流程。
- Renderer 不直接持有密码、长期令牌或任意网络/文件系统能力。
- 在线调用只允许使用当前服务、用途、版本和授权均匹配的必要内容。
- 正式盒端按账号、设备、workspace 和所有权代次隔离。
