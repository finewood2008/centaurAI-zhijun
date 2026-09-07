'use strict'
const { app, BrowserWindow, ipcMain, protocol, session, safeStorage, dialog, systemPreferences } = require('electron')
const path = require('node:path')
const { access } = require('node:fs/promises')
const { APP_ICON, installDockIcon } = require('./app-icon.cjs')
const { createDesktopRuntime } = require('./runtime/desktop-runtime.cjs')
const { ENTRY_URL, INVOKE_CHANNEL, SNAPSHOT_CHANNEL, isEntryUrl,
  createInvokeHandler, createAssetHandler } = require('./security.cjs')

protocol.registerSchemesAsPrivileged([
  { scheme: 'zhijun', privileges: { standard: true, secure: true, supportFetchAPI: true } },
  { scheme: 'zhijun-media', privileges: { standard: true, secure: true, stream: true } },
])
app.setName('知君桌面')
// Tests use new temporary directories; development uses a separate app profile.
app.setPath('userData', process.env.ZHIJUN_DESKTOP_USER_DATA
  ? path.resolve(process.env.ZHIJUN_DESKTOP_USER_DATA)
  : path.join(app.getPath('appData'), 'zhijun-desktop'))
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
  microphone = require('./runtime/microphone-permission.cjs').createMicrophonePermission({ getContents: () => window?.webContents, systemPreferences })
  runtime = createDesktopRuntime({ mode: config ? 'production' : mode, adapter,
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
    { urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*', 'file://*/*'] },
    (_details, callback) => callback({ cancel: true }),
  )
  await isolatedSession.protocol.handle('zhijun', createAssetHandler(assetRoot))
  await isolatedSession.protocol.handle('zhijun-media', request => runtime.mediaResponse(request))
  window = new BrowserWindow({
    width: 1200, height: 820, minWidth: 760, minHeight: 580,
    title: '知君', backgroundColor: '#FFFCF6', icon: APP_ICON,
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'), session: isolatedSession,
      contextIsolation: true, sandbox: true, nodeIntegration: false,
      webSecurity: true, webviewTag: false,
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

if (!app.requestSingleInstanceLock()) {
  app.quit()
} else {
  app.on('second-instance', () => { if (window && !window.isDestroyed()) { window.show(); window.focus() } })
  app.whenReady().then(createWindow).catch(() => {
    console.error('知君桌面启动失败，请先在 frontend/mindos-web 执行 npm run build:desktop，并检查桌面构建产物。')
    app.exit(1)
  })
}
app.on('window-all-closed', () => app.quit())
app.on('before-quit', event => {
  if (quitting) return
  event.preventDefault()
  quitting = true
  microphone?.dispose()
  unsubscribe()
  ipcMain.removeHandler(INVOKE_CHANNEL)
  const deadline = setTimeout(() => app.exit(0), 2500)
  const finish = () => { clearTimeout(deadline); app.quit() }
  Promise.resolve().then(() => runtime?.dispose()).then(finish, finish)
})
