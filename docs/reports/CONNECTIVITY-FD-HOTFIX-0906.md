# Connectivity SQLite 文件描述符泄漏热修

日期：2026-09-06。范围：既有盒端 DE 服务的连接生命周期修复；**不是 v2 完整产品上线或业务验收报告**。

## 故障与定位

主代理在授权盒子检查发现 DE 进程 PID `3108033` 达到 soft FD 上限1024，其中约970条描述符指向 `/home/user/centaurAI-database/db/connectivity.db`。HTTP8618连接反复reset，日志出现重复SQLite OperationalError。

代码根因是 `backend/mindos/stores/connectivity_store.py` 的21处 `with _connect() as conn:`。SQLite Connection上下文只负责commit/rollback，不负责close；高频读、nonce及清理调用依赖GC才释放底层句柄，在负载或GC延迟下耗尽FD。

临时缓解仅将故障进程soft限制提高到4096，health恢复200；这不是根因修复，也未作为长期容量方案。

## 最小代码修复与验证

单文件运行修复导入 `contextlib.closing`，把21处改为 `with closing(_connect()) as conn, conn:`。退出顺序先完成原SQLite提交/回滚，再确定关闭连接；业务SQL、锁、API和重放语义未改。

源码SHA256：`24290b789e5a8be443bc2eb37f3e437d17ca35232a6c0d3916a4edaa1580caa8`。

新增隔离测试 `backend/tests/test_connectivity_store_lifecycle.py`：禁用GC后1000轮、11000次读/nonce/清理连接操作，每100轮验证总FD完全稳定、connectivity.db FD始终0；另覆盖异常回滚、初始化失败、提交失败、六类提前返回、并发nonce仅一次接受。测试保留连接对象引用，避免GC偶然回收掩盖缺陷。

必要回归为 **211 passed，3 subtests passed**，含12项新生命周期用例，以及connectivity state/session/admin/ticket、v1 agent bridge、v2 protocol、consumer端到端隔离用例；两条依赖弃用警告。该数量属于此次明确测试集合，不与其他模块测试相加冒充完整产品验收。所有测试使用临时数据与密钥目录。

## 实际部署与观察

主代理已将热修单独提交 `132b97d` 并推送 `dev/zhijun-business-bridge-0906-live`，完成盒端原文件备份、单文件替换和服务重启。

备份目录：`/home/user/apps/centuarai-data-engine/patch-backups/20260906T040931Z-connectivity-close`。备份包含运行文件，位于盒端，未提交Git；回退需使用匹配原文件并受控重启，不能只提高FD上限掩盖泄漏。

| 观察项 | 重启后主代理记录 |
| --- | --- |
| DE新PID | 3144495 |
| 四次样本间隔 | 30秒 |
| 总FD | 52 / 55 / 52 / 52 |
| connectivity.db FD | 0 / 0 / 0 / 0 |
| HTTP | 200 |
| systemd NRestarts | 0 |

这些实际指标证明观察窗口内服务恢复且对应连接不再积累；不宣称覆盖长期全负载。v2新应用、完整产品业务和UI验收继续按[真实验收记录](../development/REAL-ACCEPTANCE-0906.md)推进。
