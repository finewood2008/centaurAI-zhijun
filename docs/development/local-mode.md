# 本机模式（分支 `newzhijun-local`）

盒子是核心产品形态，本机模式是第二形态：同一套内核装在普通电脑上，没有盒子、没有云账号。
本文记录读代码得到的设计，实现还没做。

## 一个反直觉的结论：渲染层一行都不用改

`transportRequest`（`frontend/mindos-web/src/services/transport.ts:134`）是**唯一的 HTTP 分发点**：

```
if (!isDesktopProduct()) return fetch(path, requestInit)   // 直连本机
...                                                        // 否则走 IPC → 壳 → 盒端网关
if (!desktopTransport || !hasProductScope()) throw new Error('请先连接盒子，再使用知君。')
```

`isDesktopProduct()` 只由 `src/main-desktop.ts:11` 的 `enableDesktopProduct()` 打开。
网页入口 `src/main.ts` 不打开它，挂的也是没有两道门的 `App` 而不是 `DesktopApp`。

**所以网页入口本身就是本机模式的界面。** 不需要第三个 Vue 入口，也不需要 `main-local.ts`。

## 页面必须走 http://127.0.0.1:8618/mindos/，不能走 file://

后端刻意不回任何跨域头（`backend/server.py:1141` 注释原话：不向任何跨域来源回 ACAO）。
`file://` 页面对本机后端是跨域，发得出请求但读不到响应。

所以本机模式下 Electron 窗口要加载后端**自己服务的**那份前端
（`server.py:1126` 的 `/mindos/`，目录 `frontend/mindos-web/dist`），
这样渲染进程与后端同源，`fetch` 正常工作。

顺带：`build/boundaries.ts` 的 `entryBoundary('desktop')` 禁止桌面包引入网页路由与直接 `fetch`。
那条边界**保持不变**——本机模式根本不构建新的桌面包，它加载的是网页包。

## 后端侧已经就绪

`ZHIJUN_STANDALONE=1` + 绑回环 → `local_web_debug.access_context` 返回 `standalone`，
网页闸门不再要求云端票据（`server.py:require_mindos_web_access`）。
`provisionMindosSession`（`api.ts:1743`）看到非 `connectivity_ticket_required` 就直接返回，不换票据。

## 壳里的实现（已完成）

`ZHIJUN_LOCAL_MODE=1` 开启，端口取 `ZHIJUN_LOCAL_PORT`（默认 8618）。必须显式声明，
不从别的状态推断——这条决定渲染进程能不能直接联网，不该被猜出来。

**盒端那套判定一个字没动。** `security.cjs` 里另给一份 `createLocalProfile(port)`，
两份各自独立；测试里有一条专门钉住这件事（「盒端那份判定不受本机模式影响」）。

| 本机模式 | 做法 |
|---|---|
| 入口 | `http://127.0.0.1:<port>/mindos/`，`isEntryUrl` 只认这个来源下 `/mindos/` 之下的路径 |
| 请求拦截 | 只放行同一来源，外加 `blob:`（必须属于该来源）与内联图片。**`localhost` 不放行**：那是个名字，可以被解析到别处 |
| CSP | 由壳经 `onHeadersReceived` 注入（后端不给 `/mindos/` 发 CSP）。`connect-src 'self'`——`'self'` 在这里正好解析成本机后端，不会多放行一个来源 |
| 会话分区 | 独立的 `zhijun-local-m0`，与盒端的 `zhijun-desktop-m0` 不共用 |
| **IPC** | **完全不暴露，连 preload 都不挂** |

### 为什么本机模式不暴露 IPC

这是和原计划不同的一处，也是这次改动里最要紧的一个决定。

原计划是放宽 `isEntryUrl` 去容纳本机地址。问题在于 `isEntryUrl` **同时把守
`isTrustedSender`**——放宽入口判定，等于顺带放宽了「谁能调用桌面能力」。那是一条
不该在实现第二形态时被捎带松开的边界。

而本机模式加载的是网页构建（`src/main.ts`），那份代码根本不调 `window.zhijunDesktop`。
既然用不上，就不暴露：不挂 preload、不注册 `ipcMain.handle`、不订阅 runtime 快照。
最危险的耦合因此不存在，而不是被小心地绕开。

**已知取舍**：权限一律拒绝，所以网页里的语音输入在本机模式下用不了。
要恢复它，应该单独给一个只含麦克风的窄通道，而不是把整个 IPC 面打开。

## 还没做的

- 拉起本机后端：现在要求后端已经在跑（`ZHIJUN_STANDALONE=1`，绑 127.0.0.1）。
  壳还没有代托管后端进程的逻辑。
- 打包：`electron-builder` 的配置仍然只面向盒端形态。
- 端到端验证：这台 Mac 没装 Electron 与厂商包（壳测试 78 个失败全是 `MODULE_NOT_FOUND`，
  改动前后同样 78 个），所以本机模式只验到了判定函数这一层，没有真跑起来过。

