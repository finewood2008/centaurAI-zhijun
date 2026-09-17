# 公司盒网关容量修复部署

## 计划与边界

目标：192.168.0.7，公司 AMD-A2A-248。用户在确认旧版 1024 条任务限制后授权继续部署。

- 仅部署 Data Engine `backend/mindos/zhijun_gateway/store.py` 与 `manager.py`。
- 不部署其它工作树改动，不更新模型、Remote Agent、桌面安装包或 Admin。
- 依赖现有容器及密钥/状态挂载；停机前检查活动任务，停机后备份程序及网关状态。
- 不清空 jobs，不删除写入防重回执；回滚只恢复程序，不覆盖更新后的业务数据。
- 验收：回归测试、运行哈希、健康及未授权拒绝；聚合任务分布确认回执保留、结果槽可用，客户端只读请求验证。

## 进度

- [x] 现场旧版确认：jobs 1024 条，旧回收 3600 秒，磁盘剩余 272 GB。
- [x] 容量回归与部署依赖复核：244 项通过，1 条既有 Starlette 弃用警告。
- [x] 活动任务核查、备份与定向更新。
- [x] 运行版本、健康及数据保留核验。
- [ ] 用户重新连接后，今日来信、本体、对话端到端读取验收。

## 部署记录

2026-09-15 16:57:13（北京时间）新容器启动。此次为 SSH 定向补丁，不是 manager/OTA，也不是完整 Data Engine 版本升级。

容器根文件系统只读，首次原地复制被 Docker 拒绝，随后先恢复原服务。最终采用同镜像、相同 Config/HostConfig 加两个只读源码 bind mount 的替换容器；未关闭只读保护，保留原状态、密钥和 worker 挂载，不额外更新其余源码。

- 当前容器：`zhijun-integration-0907`，`running/healthy`，RestartCount=0，ReadonlyRootfs=true。
- 旧容器：`zhijun-integration-0907-before-capacity-20260915`，停止并设为不自动启动，保留用于程序回滚。
- 新代码目录：`/srv/centauros-releases/company248-capacity-20260915`，只读挂载至容器 `mindos/zhijun_gateway/store.py`、`manager.py`。
- `store.py` SHA256：`f053f0bdd6ef181a78fef014a2ad16079efa0ade278ab2ebddf7fb7e83574571`。
- `manager.py` SHA256：`2d44b15ee4bf4149147e2d39bd32a545befceddb176ecf4ee0513adf34438391`。
- 私有备份目录：`/srv/centauros-releases/company248-capacity-backup-20260915`，root 0700；包含 0600 停机网关状态/密钥快照及容器原配置。不下载、不提交密钥和配置。
- 暂存与操作脚本：`/home/user/capacity-update-20260915.DAqxW7`；原两个程序文件位于其 `previous` 子目录。

## 验收事实

- 切换前网关 1024 条均终态；本体任务 188 done、24 failed，无运行中任务。
- 容器内新文件哈希与测试文件一致。8618、8620 `/api/health` 均 200；8620 `/api/mindos/zhijun/context` 未签名请求仍为 401。
- 部署后聚合：总记录 955，完整结果 506，过期回执 449；结果槽不再按全部历史记录计数。
- 对照停机备份逐条核验 757 条写入或未知类型记录：缺失 0，稳定 ID、请求指纹、客户端及会话所有权变化 0。
- 服务维护已回收一部分旧读取记录；未清空任务库，未删除写入回执。旧快照保留，但不得覆盖升级后真实数据作常规回滚。
- 现场知君仍停留在服务切换后的“连接未就绪”。未伪造登录/签名、未自动重发历史操作；需用户重新选择 AMD-A2A-248 验证页面读取。

## 回滚与后续发布

回滚时先停当前新容器，保留其诊断现场，再恢复旧容器原名称和 `unless-stopped` 策略并启动；只切程序，继续挂载当前数据。旧程序可能恢复 1024 条限制。

后续完整 Data Engine/manager 发布须包含这两个源码补丁；当前 bind mount 定向补丁在容器重新创建时不会自动继承，不能把今天的现场修复当作 OTA 清单已经更新。
