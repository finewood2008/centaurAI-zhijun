'use strict'
const { app, BrowserWindow, ipcMain, protocol, session, safeStorage, dialog, systemPreferences } = require('electron')
const path = require('node:path')
const { access } = require('node:fs/promises')
const { APP_ICON, installDockIcon } = require('./app-icon.cjs')
const { createDesktopRuntime } = require('./runtime/desktop-runtime.cjs')
const { isProvisioningEnabled, createProvisioningWindow, closeProvisioningWindow } = require('./provisioning-window.cjs')
const { ENTRY_URL, INVOKE_CHANNEL, SNAPSHOT_CHANNEL, isEntryUrl,
  shouldBlockRendererRequest, createInvokeHandler, createAssetHandler, createLocalProfile } = require('./security.cjs')

// 本机模式（第二形态，见 docs/development/local-mode.md）：没有盒子、没有云账号，
// 窗口直接加载本机后端自己服务的那份前端。必须显式声明，不从别的状态推断——
// 这条决定了渲染进程能不能直接联网，不该被猜出来。
const LOCAL = process.env.ZHIJUN_LOCAL_MODE === '1'
  ? createLocalProfile(process.env.ZHIJUN_LOCAL_PORT || 8618)
  : null
const provisioningTestBuild = require('./package.json').zhijunProvisioningTestBuild === true

protocol.registerSchemesAsPrivileged([
  { scheme: 'zhijun', privileges: { standard: true, secure: true, supportFetchAPI: true } },
  { scheme: 'zhijun-media', privileges: { standard: true, secure: true, stream: true, supportFetchAPI: true, corsEnabled: true } },
])
app.setName(provisioningTestBuild ? '知君配网测试版' : '知君桌面')
// Chromium/BlueZ still requires this opt-in on supported Linux builds. It must
// be set before ready and does not bypass the dedicated window's permissions.
if (process.platform === 'linux' && isProvisioningEnabled({ platform: process.platform, testBuild: provisioningTestBuild })) {
  app.commandLine.appendSwitch('enable-experimental-web-platform-features')
}
// Tests use new temporary directories; development uses a separate app profile.
app.setPath('userData', process.env.ZHIJUN_DESKTOP_USER_DATA
  ? path.resolve(process.env.ZHIJUN_DESKTOP_USER_DATA)
  : path.join(app.getPath('appData'), provisioningTestBuild ? 'zhijun-provisioning-test' : 'zhijun-desktop'))
if (process.env.ZHIJUN_SHELL_NOGPU === '1') app.disableHardwareAcceleration()

const mode = !app.isPackaged && process.env.ZHIJUN_DESKTOP_MODE === 'simulation'
  ? 'simulation' : 'unconfigured'
let runtime
let microphone
let unsubscribe = () => {}
const assetRoot = app.isPackaged
  ? path.join(process.resourcesPath, 'mindos-web-dist')
  : path.resolve(__dirname, '../mindos-web/dist-desktop')
let window
let quitting = false

async function createWindow() {
  installDockIcon(app)
  if (LOCAL) return createLocalWindow()
  await access(path.join(assetRoot, 'desktop.html'))
  let config
  if (mode !== 'simulation') {
    const filename = app.isPackaged ? path.join(process.resourcesPath, 'zhijun-product.json') : process.env.ZHIJUN_DESKTOP_CONFIG
    try {
      config = await require('./production/config.cjs').loadConfig(filename,
        app.isPackaged ? { resourceRoot: process.resourcesPath } : undefined)
    } catch { /* Invalid configuration stays closed. */ }
  }
  const adapter = config ? await require('./production/adapter.cjs').createProductionAdapter({
    config, directory: app.getPath('userData'), safeStorage,
    bridge: require('./production/business-bridge.cjs').createBusinessBridge(),
  }) : undefined
  const provisioningConfigured = Boolean(config?.provisioning?.electronWebBluetoothDiscoveryV1
    && config?.provisioning?.electronBleProvisioningV2)
  const provisioningReady = Boolean(config && isProvisioningEnabled({ platform: process.platform, testBuild: provisioningTestBuild })
    && (provisioningTestBuild || provisioningConfigured))
  microphone = require('./runtime/microphone-permission.cjs').createMicrophonePermission({ getContents: () => window?.webContents, systemPreferences })
  runtime = createDesktopRuntime({ mode: config ? 'production' : mode, adapter,
    provisioningHost: provisioningReady ? {
      open: async () => {
        const provisioningContext = provisioningTestBuild ? undefined : await adapter.provisioningContext()
        await createProvisioningWindow({ BrowserWindow, session, dialog, ipcMain, parent: window,
          isPackaged: app.isPackaged, testBuild: provisioningTestBuild, env: process.env,
          provisioningConfig: config.provisioning, provisioningContext })
        return { opened: true }
      },
      close: closeProvisioningWindow,
    } : undefined,
    productHost: { save: require('./runtime/native-save.cjs').createNativeSave({ dialog, getWindow: () => window }),
      requestMicrophone: owner => microphone.request(owner), revokeMicrophone: owner => microphone.revoke(owner) },
  })
  unsubscribe = runtime.subscribe(snapshot => {
    if (window && !window.isDestroyed() && isEntryUrl(window.webContents.getURL())) {
      window.webContents.send(SNAPSHOT_CHANNEL, snapshot)
    }
  })
  const isolatedSession = session.fromPartition('zhijun-desktop-m0')
  isolatedSession.setPermissionRequestHandler(microphone.permissionRequest)
  isolatedSession.setPermissionCheckHandler(microphone.check)
  isolatedSession.on('will-download', event => event.preventDefault())
  isolatedSession.webRequest.onBeforeRequest(
    { urls: ['<all_urls>'] },
    (details, callback) => callback({ cancel: shouldBlockRendererRequest(details.url) }),
  )
  await isolatedSession.protocol.handle('zhijun', createAssetHandler(assetRoot))
  await isolatedSession.protocol.handle('zhijun-media', request => runtime.mediaResponse(request))
  window = new BrowserWindow({
    width: 1200, height: 820, minWidth: 760, minHeight: 580,
    title: '知君', backgroundColor: '#FFFCF6', icon: APP_ICON,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'), session: isolatedSession,
      contextIsolation: true, sandbox: true, nodeIntegration: false,
      webSecurity: true, webviewTag: false, plugins: false,
    },
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event, url) => { if (!isEntryUrl(url)) event.preventDefault() })
  window.webContents.on('did-start-navigation', (_event, _url, isInPlace, isMainFrame) => { if (isMainFrame && !isInPlace) microphone.revoke() })
  window.webContents.on('will-attach-webview', event => event.preventDefault())
  window.webContents.on('render-process-gone', () => app.quit())
  ipcMain.handle(INVOKE_CHANNEL, createInvokeHandler(runtime, () => window?.webContents))
  await window.loadURL(ENTRY_URL)
}

/** 本机模式的窗口。与盒端那条路完全分开，不共用 runtime、IPC 与资源协议。 */
async function createLocalWindow() {
  const isolatedSession = session.fromPartition('zhijun-local-m0')
  isolatedSession.on('will-download', event => event.preventDefault())
  // 拦截器只放行本机后端自己（见 security.cjs 的 createLocalProfile）。
  isolatedSession.webRequest.onBeforeRequest(
    { urls: ['<all_urls>'] },
    (details, callback) => callback({ cancel: LOCAL.shouldBlockRendererRequest(details.url) }),
  )
  // 后端不给 /mindos/ 发 CSP，这份由壳注入：渲染进程的边界是壳的责任，不能指望被加载方自律。
  isolatedSession.webRequest.onHeadersReceived((details, callback) => callback({
    responseHeaders: { ...details.responseHeaders,
      'Content-Security-Policy': [LOCAL.CSP],
      'X-Content-Type-Options': ['nosniff'] },
  }))
  // 本机模式不暴露任何 IPC，也就不需要权限中介：一律拒绝。
  // 代价是网页里的语音输入用不了，这是已知取舍，记在 local-mode.md。
  isolatedSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  isolatedSession.setPermissionCheckHandler(() => false)

  window = new BrowserWindow({
    width: 1200, height: 820, minWidth: 760, minHeight: 580,
    title: '知君', backgroundColor: '#FFFCF6', icon: APP_ICON,
    webPreferences: {
      // 不挂 preload：本机模式不向渲染进程暴露任何桌面能力。
      session: isolatedSession,
      contextIsolation: true, sandbox: true, nodeIntegration: false,
      webSecurity: true, webviewTag: false, plugins: false,
    },
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event, url) => { if (!LOCAL.isEntryUrl(url)) event.preventDefault() })
  window.webContents.on('will-attach-webview', event => event.preventDefault())
  window.webContents.on('render-process-gone', () => app.quit())
  await window.loadURL(LOCAL.entryUrl)
}

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => { if (window && !window.isDestroyed()) { window.show(); window.focus() } })
  app.whenReady().then(createWindow).catch(() => {
    console.error(LOCAL
      ? `知君本机模式启动失败：连不上 ${LOCAL.entryUrl}。先确认本机后端已在运行（ZHIJUN_STANDALONE=1，绑 127.0.0.1），并已执行 npm run build。`
      : '知君桌面启动失败，请先在 frontend/mindos-web 执行 npm run build:desktop，并检查桌面构建产物。')
    app.exit(1)
  })
}
app.on('window-all-closed', () => app.quit())
app.on('before-quit', event => {
  if (quitting) return
  event.preventDefault()
  quitting = true
  microphone?.dispose()
  closeProvisioningWindow()
  unsubscribe()
  ipcMain.removeHandler(INVOKE_CHANNEL)
  const deadline = setTimeout(() => app.exit(0), 2500)
  const finish = () => { clearTimeout(deadline); app.quit() }
  Promise.resolve().then(() => runtime?.dispose()).then(finish, finish)
})
