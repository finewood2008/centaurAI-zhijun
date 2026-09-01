# 半人马个人 AI 节点手机 App

这个目录是移动端原生壳，使用 Capacitor 复用 `frontend/mobile` 的同一套页面和 API。

## 使用方式

```bash
cd frontend/mobile-native
npm install
npm run add:android
npm run open:android
```

如果 `android/` 已经存在，改动页面或插件后执行：

```bash
npm run sync:android
```

iOS：

```bash
cd frontend/mobile-native
npm install
npm run add:ios
npm run open:ios
```

每次改动 `frontend/mobile` 后执行：

```bash
npm run sync
```

## 首次连接

1. 手机和服务器都登录同一个 Tailscale 网络。
2. 桌面端启用「手机 App 导入」并生成 App Token。
3. 在 App 的「节点地址」填入 `http://100.x.y.z:8618`。
4. 填入 App Token 后即可录音、上传文件、剪藏、搜索、查看处理结果和使用 A2A Context Pack。

采集时如果 Tailscale 或个人 AI 主机暂时不可达，App 会把录音 Blob、文件或剪藏文本保存到本机 IndexedDB 待同步队列。保存 Token、网络恢复、打开结果页或手动点击「同步」后会自动重试。

## Android 系统分享

Android 工程已经接入 `@capgo/capacitor-share-target`：

- App 会出现在系统分享菜单中，接收文本、图片、音频、视频、PDF、Word、PPT 和通用文件。
- 分享文本会预填到「剪藏」。
- 分享文件会从插件缓存路径读取为 `File`，再复用 `/api/mobile/uploads` 或 `/api/mobile/recordings` 上传到个人 AI 主机。
- 如果尚未保存 App Token，分享文件会暂存为待处理项，保存 Token 后自动继续上传。

## A2A 邀请分享

Android/iOS 原生壳同时接入 `@capacitor/share`。在 A2A Context Pack 中生成邀请后，可以直接点「分享」或「分享最近邀请」调起系统分享面板，把 Agent Card、Message endpoint 和 Bearer Token 发给另一个 Agent 或联系人。浏览器/PWA 环境会优先走 Web Share API，不支持时回退到复制。

本机打包需要 Java JDK 和 Android SDK：

```bash
cd frontend/mobile-native/android
./gradlew assembleDebug
```

## 技术说明

- `scripts/prepare-web.js` 会把 `frontend/mobile` 与 `frontend/assets` 复制到 Capacitor 的 `www/`。
- `capacitor.config.json` 启用了 `CapacitorHttp`，让原生 App 内的 `fetch` 走原生 HTTP 层，避免把后端全局 CORS 放开。
- `server.cleartext=true` 允许 Android 通过 Tailscale HTTP 地址连接个人服务器。只建议用于 Tailscale/内网；公网部署应改为 HTTPS。
- 当前 PWA 和 Android 原生壳都支持系统分享目标；iOS 原生接收分享还需要在 Xcode 中增加 Share Extension，并通过 App Group 与主 App 通信。
