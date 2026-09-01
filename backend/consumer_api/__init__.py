"""Consumer API（Stateful Strict Mock）。

阶段 2：外部 Consumer API 服务尚未部署时，本包提供确定性的 Mock 实现，覆盖
- 手机号登录即注册、Refresh 合并、Logout、Client 列表与撤销；
- 设备列表/详情/重命名/所有权认领与 OTA 状态；
- /sync/bootstrap 与 /sync/changes 光标增量同步；
- 绑定 Account/Client/Device/Session/scope/有效期/epoch_generation/nonce
  的短期连接票据签发与 JWKS 发布。

约束（遵循迭代指南）：
- Mock 只服务开发与联调，不调用旧 /nexus/*、Admin、企业或明文配网接口；
- 云端权威：设备所有权、Client/Session 状态、同步与票据只由本 API 管理，
  MindOS 与客户端不得伪造 Owner 或权威 device_id；
- 测试私钥/票据仅限测试夹具，禁止进入生产包。
"""
