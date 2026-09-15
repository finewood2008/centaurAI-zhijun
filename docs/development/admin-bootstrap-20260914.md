# 2026-09-14 Admin Bootstrap 配置恢复

## 范围及计划

用户授权检查并更新线上 Admin 配置。目标 `8.138.1.109`，应用目录
`/data/apps/nexusaos_admin_backend-0.1`。不修改盒端、绑定关系、凭据或 Data Engine。

- [x] 核实当前服务、功能开关、数据库字段及旧盒子兼容条件。
- [x] 使用线上代码执行 bootstrap 回滚事务预检，不持久化测试写入。
- [x] 备份配置和启动参数，仅增加 `NEXUSAOS_CONSUMER_PAIRING_V2_ENABLED=true`。
- [x] 精确重启原 Admin 进程，验证健康、功能开关及授权轮询。
- [ ] 用户刷新知君，验收真实登录身份下的列表和连接。

验收条件：服务恢复健康；总开关生效；已有两个 Owner/epoch 不变；原有精确限时
legacy 兼容仍有效；不得为了列表放开 console claim、BLE 或旧 v1/internal-test。
若出现数据库迁移缺口或已有 v2 历史，则停止，不能删除历史或自动降级。

## 只读预检结果

- 原 Admin PID `2755469`，启动时间 2026-09-13 20:27:15，监听 9099。
- v2 总开关在实际启动环境及两个配置文件中均未设置，默认 false。
- 20260910 相关八张 Consumer 表存在，新增账号/客户端/绑定字段齐备。
- 全库 v2 session/update/ACK 均为 0；两个 Owner 均 active/ready、epoch 1。
- 公司及家里盒子均匹配已存在的精确 legacy 白名单，未扩大授权范围。
- 线上 service 回滚事务预检：设备数 2，当前客户端有活动会话，两台 ready/canConnect。
  这是服务/数据兼容测试，不冒充客户端签名 HTTP 验收，也不声称设备已有 v2 ACK。
- 公司旧协议兼容有效期到北京时间 2026-09-17 20:00；家里到 2026-09-18 23:59。
  本次不延长有效期，后续必须协调正式迁移。

## 待变更文件与回滚

仅修改 `/data/secrets/nexusaos-remoteops/admin-remoteops.env`，保留 0600 权限。
启动使用原 argv/cwd/environment，仅加入上述总开关，追加原日志，不执行宽范围 kill，
不安装依赖或覆盖代码。配置、旧进程启动信息仅保存服务器 root-only 目录，不入仓库。
若新进程不能启动，恢复备份配置并用旧环境启动原服务；不回滚数据库或绑定记录。

## 实际执行记录

- 配置及旧进程启动信息备份目录：
  `/data/secrets/nexusaos-remoteops/bootstrap-20260914-0j3sh42t`，目录 0700，秘密文件 0600。
- 只增加 v2 总开关；原两个 legacy 条目、到期时间及其他环境变量不变。
- 原进程是 root、无自动监管器、单个 Uvicorn 监听器；唯一子进程是 Python resource tracker。
  精确 TERM 后用原 argv/cwd/environment 启动，新 PID `2761945`；未强杀、未截断日志。
- `/openapi.json` 返回 200，并确认 9099 监听属于新进程。
- 新进程启动环境中的 v2 总开关为 true；console claim 和旧 v1/internal-test 仍未设置。
- 未认证访问 bootstrap 返回 401，身份认证未被绕过。
- 两台盒子真实 `consumer-authorization` 轮询均已出现 200；没有新增 Traceback 或 Unknown column。
- 随后 6 次、每次 10 秒的观察窗口记录 28 条授权轮询 200，零 Traceback/Unknown column。
  最终健康检查 200；Owner 状态/epoch 未变，v2 session/update/ACK 仍全部为 0，未发生自动迁移。
- 原启动脚本读取持久配置也确认总开关为 true，不仅是一次性进程环境变更。
- 从本机直连 `192.168.0.7:22` 被拒绝，因此没有声称读取了盒端本地快照，也没有修改或重启盒子。
- 尚需用户在知君点击“刷新盒子”，完成真实客户端签名请求与连接验收；不需要重新打包。
