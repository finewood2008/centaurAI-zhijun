# 测试问题修复发布：公司 248

## 计划与验收

用户要求重新打包并更新相关盒端。目标为此前现场公司 AMD-A2A-248（192.168.0.7）。

- 客户端：正式 macOS arm64 0.1.26，保留生产配网信任配置，签名、内容核验和独立启动验证，不覆盖本机已安装程序。
- 盒端：完整知君 worker 与匹配 catalog，包含本体承接、检索触发、事情修订和回答收尾修复。保留 Data Engine 现有镜像及容量补丁，不更新 Admin、Remote Agent、模型服务或家盒。
- 依赖：核对现有容器挂载、活动任务及源文件；源码发行审计；停机一致性备份。不能清空任务或自动重试真实聊天。
- 验收：全部源码哈希与 catalog 匹配、服务健康、未签名请求拒绝、原数据库保留；合成测试与真实用户业务验收分别记录。
- 回滚：只恢复旧源码与 catalog，使用当前数据；禁止以旧数据备份直接覆盖升级后的业务写入。

## 进度

- [x] 正式安装包、验包与 SHA-256。
- [x] 盒端身份/活动任务/版本只读核查。
- [x] 源码审计、目标环境预检与一致性备份。
- [x] 切换、运行哈希和健康核验。
- [x] 实际结果及回滚说明。

## 实际部署

2026-09-15 17:57:08（北京时间）完成公司盒更新。经 SSH 定向切换完整 worker，不是 manager/OTA 发布；没有更改当前容器环境、挂载或 Data Engine 镜像。

- 容器 `zhijun-integration-0907`，ID `81bc9de19440208c685cafe62b207502c08a03b6262bcdef1b77c238ba53333b`，running/healthy，RestartCount=0，ReadonlyRootfs=true。
- 镜像保持 `sha256:2d4805c9354b03e44e25dd927aa86736fb71973b991a3a3238747d45f61010a2`。
- 源码审计 178 文件、175 Python 文件、1,319 本地导入；包含工作树修改，不冒称纯 Git HEAD 制品。
- worker 归档 SHA-256：`7fa093b6caabef5737d01ef604ec8b9be3c8d7629db92a6b0527e0faddfb467b`。
- 全部 178 个运行文件与 manifest 一致；规范 JSON manifest SHA-256：`d62f8615fdeaddb0a2504fbc868e86d46c321757f209d7786dcfbb182015ece6`。
- catalog SHA-256：`3b4d0a8169334cb8d6548a51f44583c5c5e56e13e54ae19832b25dd722b8722d`，与客户端清单一致。
- 运行目录：`/srv/centauros-releases/company248-business-20260914-1/zhijun`；原目录保留为同级 `zhijun-before-qa-20260915`。
- 暂存及审计脚本：`/home/user/zhijun-qa-update-20260915.5RP8uZ`。
- 一致性备份：同级 `.qa-upgrade-backup-20260915`（root 0700），内有 0600 的 `workspace-state-secrets.tar.gz`，覆盖 domain、gateway-state、gateway-secrets；未下载或提交任何用户数据/密钥。

## 核验

切换前无 queued/running 本体任务及网关操作。停止原容器后备份并核验源码未被并发改变，再成对换入 worker 和 catalog、启动同一容器。容量补丁 store.py/manager.py 的实际运行 SHA-256 与前次部署一致。

独立、无网络、只读代码、临时工作区预检通过真实签名 dispatch、聊天 SSE 正常结束、本体 working 候选和原话证据；仅模型 HTTP 响应为合成，未使用真实账号和模型密钥。额外 23 项模型配置/GPU 合同/流收尾回归通过。

对停机备份与当前数据库逐条比对：原会话行及 118 条消息完整保留；未重放聊天、未批量重试历史整理。盒子 loopback 8618/8620 健康返回 200，8620 未签名 context 请求仍拒绝为 401。局域网直连这两个业务端口的探测未通，桌面仍应通过原加密连接访问，未为探测而开放端口。

正式客户端 0.1.26 及签名/验包结果见 [客户端发布记录](release-0.1.26-qa.md)。尚未覆盖本机 `/Applications`，用户安装后重新连接 AMD-A2A-248，再验收真实对话、本体待核对候选、检索和事情切换；健康和合成预检不等于全部真实业务已验收。

## 回滚

先确认没有正在进行的写入并停止同一容器，保留新目录，恢复 `zhijun-before-qa-20260915` 到原 `zhijun` 路径，再启动容器并验哈希/健康。只切换源码和匹配 catalog，继续使用当前数据；不要直接恢复旧数据库覆盖升级后的写入。Data Engine 两个容量补丁挂载在此次回滚中应保持不变。
