# 知君连接恢复实施计划

## 范围与验收

账号登录保持7天；连接短票据保持不变。已建立连接关闭/到期或网络短暂异常后，不调用退出登录、不清安全存储，自动重新获取票据并验证相同账号、盒子与工作区。真正账号失效/撤销仍退出。权限拒绝不无限重试。未知结果的写操作不自动重放。

- [x] 主进程区分账号失败与连接失败，单飞有界退避恢复。
- [x] 主动断开、退出、换盒、窗口关闭取消恢复，迟到结果不得恢复旧身份。
- [x] 更新错误文案与旧测试，覆盖连接到期、重连失败、并发、未知写结果。
- [x] 补齐审阅发现的 renderer 边界：在旧请求取消前捕获在途写操作，跨重连保留同账号/设备/工作区的核对提示，不放行旧代次业务响应。
- [x] 运行shell、UI及相关端到端回归，独立审阅。
- [x] 记录交付状态，明确源码修复与已安装客户端的区别。

主代理拥有runtime/desktop-runtime.cjs及必要product-session/consumer客户端集成；测试代理仅拥有新reconnect测试和connection-errors/production-storage测试；文档代理拥有public-error文案、相关UI测试、连接恢复文档及Admin错误规范。保留各工程现有脏工作区，不提交、不推送、不更改线上服务或覆盖已安装应用。

审阅补充：测试代理另行拥有 renderer 的 `productClient.ts`、`DesktopApp.vue`、`writeUncertainty.ts`、新增 `write-uncertainty.test.mjs` 与最小测试脚本接线；账号边界代理拥有 `consumer-client.cjs` 和对应测试。主代理集成并复核这些改动，独立审阅代理只读检查。

依赖：现有Consumer refresh协调器、SDK重新申请票据能力、generation失效控制与业务context授权。复用connecting/authorizing/ready状态，不扩大IPC权限。默认最多3次退避恢复，稳定60秒后恢复重试预算，防止刚连上即断开的无限循环。

## 结果

源码修改完成，独立审阅通过。Shell 241/241、桌面 UI/作用域 35/35、业务前端 24/24、Electron 启动与隔离 4/4；桌面类型检查与构建通过。详细命令和现场验收见 `connection-session-recovery.md`。未打包发布、未覆盖已安装客户端、未更改线上服务，跨网断连与长时间现场运行尚待更新客户端后验收。
