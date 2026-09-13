# 知君文档

这里仅保留会持续维护的产品、架构、运行说明和机器契约。带日期的实施计划、验收回执、故障现场、旧原型和模型评测不再保留在工作树；需要追溯时使用 Git 历史。

## 从哪里开始

| 需要了解 | 文档 | 权威来源 |
|---|---|---|
| 产品定位、入口和边界 | [产品定义](product/PRODUCT.md) | 产品行为与当前界面 |
| 系统组成、数据流和部署边界 | [架构](development/architecture.md) | 当前代码与运行配置 |
| 本机开发、测试隔离 | [本机开发](development/local-runtime.md) | `zhijun.sh`、测试脚本 |
| 在线/本地模型选择与授权 | [模型路由](development/task-routing.md) | 路由 API、消息回执 |
| 桌面运行与打包 | [桌面说明](../frontend/shell/README.md) | `frontend/shell/package.json` 脚本 |
| Windows 安装包、签名与验包 | [Windows 发布指南](development/windows-release.md) | Windows 构建与验包脚本 |
| 业务 HTTP/SSE 接口 | [知君接口契约](development/zhijun-api-contract.md) | 后端路由实现 |
| Consumer API | [Consumer 契约](development/consumer-api-contract/CONTRACT.md) | 同目录 OpenAPI 与版本文件 |

## 功能实现说明

- [对话、附件与回复恢复](development/conversations.md)
- [记忆、章程与判断](development/memory-and-decisions.md)
- [本地与在线模型路由](development/task-routing.md)

## 机器读取文件

以下文件不是普通说明文档，不能随意删除或手工改写：

- `development/consumer-api-contract/openapi.json`、`VERSION`：Consumer 合同测试的输入。
- `development/contracts/mindos-bridge-v1.json`、`zhijun-workspace-v2.json`：历史兼容和当前工作区合同。

## 维护规则

1. 文档描述长期有效的行为和边界，不记录单次构建哈希、PID、私网地址或测试流水账。
2. 版本、模型名、端口和能力以代码或部署配置为准；文档只在确有稳定合同的地方写死。
3. CentaurOS 负责盒端推理运行时与 NPU-only 约束；本仓库桌面端不实现 CPU 回退。模型实际去向以消息 `provider/model/external` 回执和盒端路由审计为准，不能以模型自称判断。
4. 一个主题只保留一份现行说明；完成的一次性计划从工作树删除，通过 Git 历史追溯。
