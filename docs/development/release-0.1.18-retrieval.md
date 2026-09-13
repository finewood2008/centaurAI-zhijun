# 知君 0.1.18 检索更新发布记录

## 发布计划（2026-09-13，已完成）

用户授权：生成新安装包并更新盒端程序。

- 桌面目标：现有正式 macOS Apple Silicon DMG / ZIP，输出 `frontend/shell/release/`；版本 0.1.17 → 0.1.18，不生成 test 包。
- 盒端目标：公司 `192.168.0.7`，`user-M900`；当前 Mac 与其同属 192.168.0.0/24，已核对身份及运行实例。家里 192.168.1.18 当前不可达，不更新。
- 范围：本次知君检索优化的 worker 源代码与 product operation catalog。保留 Data Engine 镜像、Remote Agent、资料、账号、长期 App 凭据及模型配置。
- 版本文件：`frontend/shell/package.json`、`frontend/shell/package-lock.json`；构建目录和临时部署材料继续由 `.gitignore` 排除，不提交 Git。
- 并行分工：主代理负责正式安装包、发布集成及最终验证；独立只读核对盒子部署；独立构建经哈希/资源审计的 source-only worker 包。

## 验收与回退

1. 安装包走现有完整发布脚本：Shell 测试、桌面测试、Electron E2E、生产构建、签名、DMG/ZIP 内容检查、独立配置目录启动冒烟。
2. worker 包显式包含新增 `retrieval_tools.py`，不含资料、数据库、凭据、测试和其他团队源码；核对清单与源哈希。
3. 盒端核对精确容器/镜像/挂载基线，保留旧容器与只读代码目录。更新前做一致性状态备份；回退程序不自动覆盖新产生的业务数据。
4. 在原镜像隔离环境验证新代码，再切换 worker 挂载；核对容器健康、HTTP health、未认证访问仍拒绝、部署代码哈希与无关服务未变化。
5. 不以健康检查冒充真实检索/模型业务验收；不自动发送用户资料到模型。

## 当前限制

- 现有安装配置使用 Developer ID 签名，但 `notarize: false`；不宣称已完成 Apple 公证。
- 当前公司集成实例不是可直接使用的签名 manager OTA 发行，本次沿用有回退保护的现有容器部署方式。

## 安装包结果

完整 `npm run package:mac-arm64` 流程通过（使用缓存 Node 22.23.2），版本 0.1.18。Developer ID 签名、DMG/ZIP 解包验证和隔离用户配置目录下的启动冒烟均通过；未自动替换本机已安装的应用。

产物位于 `frontend/shell/release/`：

- `Zhijun-0.1.18-mac-arm64.dmg`：SHA256 `6b2791aa69ee2e208a68116fd48c9cbd6a039c4b209f74c8fe22cf387c96b7ae`
- `Zhijun-0.1.18-mac-arm64.zip`：SHA256 `2d69bc5a861b8579e021f3c06187913c277a8a8ef4b74784e3f4e44797ad94f8`
- `Zhijun-0.1.18-mac-arm64.SHA256SUMS`：上述两个产物校验和。

0.1.17 的 DMG/ZIP 保留；`release/mac-arm64` 中的展开应用已由正常打包流程更新为 0.1.18。

## 盒端结果

公司 `192.168.0.7` 已完成 worker-only 更新，发行 `retrieval-20260913-239c782`。

- 当前容器 ID：`80f4b8dd3680194ae2bb06fba64c393c333a7c640ae1534be45641950a681d43`。
- 镜像保持 `sha256:953f207ba8896d279d65eb669c819e1a7166e125093ad17c6ecf20f28f35d66d`，没有重新部署 Data Engine、Remote Agent 或模型。
- 新只读代码目录：`/srv/zhijun-integration-0907/release-retrieval-20260913-239c782/worker`。
- 源码包包含 176 个文件；SHA256 `239c78287191b517319cec0d6b4f115bb16c811e175737a020cd7f5ef74283de`。基于上述 Git commit 的工作区增量构建，不能把 commit 本身当作这次源码包完整身份。
- 部署收据：发行目录的 `private/receipt.json`；旧容器保留为 `zhijun-integration-0907-pre-retrieval-20260913-239c782`，停止运行。
- 停机一致性备份：发行目录的 `private/pre-switch-state.tar.gz`，19,023,875 字节，SHA256 `b4783cc3d5855e58dead5b3e182e2e256461b8bd8937206494039c11b3f533c5`；未恢复覆盖数据。

验证通过：

1. 在原镜像、无网络、无真实 workspace 的隔离容器中解析 173 个 Python 文件，真实导入 app/routing/retrieval_tools；合成测试确认材料选择后只交付选中子集、重新检索后旧 Search ID 拒绝。
2. 新容器 healthy；8618/8619/8620 的 `/api/health` 返回 200；8618/8620 未认证 context 请求仍返回 401。
3. 核对 Search、Confirm、Confirm-Unverified、Evidence Resolve、Capabilities、规则状态 6 个 REST 合同端点；容器内 8 个关键源码/catalog 哈希与清单一致。
4. 原镜像、环境、只读根、安全约束及其他挂载保持不变；Remote Agent、proxy、Ollama 和 legacy 容器未被重启。
5. 独立只读复核：176 个源码文件与清单全量一致，无缺失、额外文件或哈希差异；新容器重启计数 0、未 OOM，旧容器已停止；备份文件摘要与收据一致。

8618/8619/8620 按现有安全拓扑仅监听 `127.0.0.1`。从 Mac 直接访问 `192.168.0.7:8618` 被拒绝是预期行为，不因此向局域网开放服务端口；桌面仍通过原有受认证连接访问。

部署中处理了 Docker inspect 的等价表示差异：`OomKillDisable` 在创建和启动后可分别为 `false` / `null`，均未禁用 OOM killer；挂载列表按目标路径比较，不能依赖返回顺序。初次切换后过于严格的等价值检查造成一次短暂服务中断，已通过重启同一个新容器完成前向恢复，未切回旧代码、未回滚数据。现场记录保留在 `private/recovery-required.json`，已由 `private/recovery-resolved.json` 标明恢复完成。

本次未读取客户资料、未调用真实云端模型，收据明确 `realClientAcceptance=false`。安装 0.1.18 后仍需由用户重连公司盒子，验证真实检索、材料选择和流式回答。
