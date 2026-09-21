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

## 所以要改的全在壳里（`frontend/shell/`，共 218 行）

| 位置 | 改什么 | 注意 |
|---|---|---|
| `security.cjs:4` `ENTRY_URL` | 本机模式指向 `http://127.0.0.1:8618/mindos/` | 现在写死为 `zhijun://desktop/desktop.html` |
| `security.cjs:25` `isEntryUrl` | 接受本机地址 | 它同时把守 `isTrustedSender`，放宽等于放宽 IPC 的调用方校验 |
| `security.cjs:13` `shouldBlockRendererRequest` | 放行 `http://127.0.0.1:8618/*` | 现在除 `zhijun:` / `zhijun-media:` / `blob:` / `data:image` 外一律拦截 |
| `security.cjs:11` `CSP` | 现在是 `connect-src zhijun-media: blob:`，禁止任何 HTTP | 本机模式下页面由后端服务、带后端自己的 CSP，壳不该再注入这一份 |
| `main.js:103` | 按模式选 URL | |
| 启动 | 以 `ZHIJUN_STANDALONE=1` 拉起本机后端 | |

**IPC 要不要留是个待定**。本机模式不需要 `product.*` 那套（走直连 fetch 了），
但原生保存、麦克风权限、安全存储仍然有用。倾向：本机模式下把 `OPERATIONS` 收窄到原生能力那几项，
不暴露 `product.*` 与账号 / 连接相关的操作。

## 为什么值得单独一条分支

上面每一条都在动壳的安全边界：入口白名单、请求拦截、CSP、IPC 调用方校验。
盒端形态靠这几条保证「渲染进程绝不直接联网」，而本机模式恰恰要让它直接联网。
两种形态在这一点上要求相反，混在一条线上很容易把盒端的保证悄悄削弱。
