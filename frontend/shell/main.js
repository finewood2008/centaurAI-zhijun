'use strict'
const { app, BrowserWindow, ipcMain, protocol, session } = require('electron')
const path = require('node:path')
const { access } = require('node:fs/promises')
const { createDesktopRuntime } = require('./runtime/desktop-runtime.cjs')
const { ENTRY_URL, INVOKE_CHANNEL, SNAPSHOT_CHANNEL, isEntryUrl,
  createInvokeHandler, createAssetHandler } = require('./security.cjs')

protocol.registerSchemesAsPrivileged([
  { scheme: 'zhijun', privileges: { standard: true, secure: true, supportFetchAPI: true } },
])
app.setName('知君桌面')
// Tests use new temporary directories; development uses a separate app profile.
app.setPath('userData', process.env.ZHIJUN_DESKTOP_USER_DATA
  ? path.resolve(process.env.ZHIJUN_DESKTOP_USER_DATA)
  : path.join(app.getPath('appData'), 'zhijun-desktop'))
if (process.env.ZHIJUN_SHELL_NOGPU === '1') app.disableHardwareAcceleration()

const mode = !app.isPackaged && process.env.ZHIJUN_DESKTOP_MODE === 'simulation'
  ? 'simulation' : 'unconfigured'
const runtime = createDesktopRuntime({ mode })
const assetRoot = path.resolve(__dirname, '../mindos-web/dist-desktop')
let window
let quitting = false
const unsubscribe = runtime.subscribe(snapshot => {
  if (window && !window.isDestroyed() && isEntryUrl(window.webContents.getURL())) {
    window.webContents.send(SNAPSHOT_CHANNEL, snapshot)
  }
})

async function createWindow() {
  await access(path.join(assetRoot, 'desktop.html'))
  const isolatedSession = session.fromPartition('zhijun-desktop-m0')
  isolatedSession.setPermissionRequestHandler((_contents, _permission, callback) => callback(false))
  isolatedSession.setPermissionCheckHandler(() => false)
  isolatedSession.on('will-download', event => event.preventDefault())
  isolatedSession.webRequest.onBeforeRequest(
    { urls: ['http://*/*', 'https://*/*', 'ws://*/*', 'wss://*/*', 'file://*/*'] },
    (_details, callback) => callback({ cancel: true }),
  )
  await isolatedSession.protocol.handle('zhijun', createAssetHandler(assetRoot))
  window = new BrowserWindow({
    width: 1200, height: 820, minWidth: 760, minHeight: 580,
    title: '知君', backgroundColor: '#FFFCF6',
    webPreferences: {
      preload: path.join(__dirname, 'preload.cjs'), session: isolatedSession,
      contextIsolation: true, sandbox: true, nodeIntegration: false,
      webSecurity: true, webviewTag: false,
    },
  })
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.webContents.on('will-navigate', (event, url) => { if (!isEntryUrl(url)) event.preventDefault() })
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
  unsubscribe()
  ipcMain.removeHandler(INVOKE_CHANNEL)
  const deadline = setTimeout(() => app.exit(0), 2500)
  const finish = () => { clearTimeout(deadline); app.quit() }
  Promise.resolve().then(() => runtime.dispose()).then(finish, finish)
})
