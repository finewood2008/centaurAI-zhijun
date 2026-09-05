// 知君桌面薄壳：一个 BrowserWindow 加载 http://127.0.0.1:8618/mindos/。
// 没有 preload、没有通用 fetch 桥、webSecurity 保持默认开启；后端不在时显示提示页并自动重试。
const { app, BrowserWindow, shell } = require('electron')

const BASE = process.env.ZHIJUN_BASE_URL || 'http://127.0.0.1:8618'
const SMOKE = process.env.ZHIJUN_SHELL_SMOKE === '1'
// 远程桌面 / 无 GPU 的机器上 GPU 进程会崩，冒烟与 ZHIJUN_SHELL_NOGPU=1 时禁用硬件加速。
if (SMOKE || process.env.ZHIJUN_SHELL_NOGPU === '1') {
  app.disableHardwareAcceleration()
  app.commandLine.appendSwitch('disable-gpu')
}

function waitingPage() {
  return 'data:text/html;charset=utf-8,' + encodeURIComponent(`<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><title>知君</title>
  <style>body{font-family:-apple-system,"PingFang SC",sans-serif;background:#FFFCF6;color:#1D211F;display:flex;align-items:center;justify-content:center;height:100vh;margin:0}
  .card{max-width:420px;text-align:center}.seal{display:inline-block;border:2px solid #A6452E;color:#A6452E;font-family:"Songti SC",serif;font-size:28px;padding:4px 10px;margin-bottom:16px}
  code{background:#F3EFE6;padding:2px 6px;border-radius:4px}</style></head>
  <body><div class="card"><div class="seal">知</div><h2>正在等待本机的知君服务</h2><p>请先启动后端：<code>./start-backend.sh</code></p><p>地址：<code>${BASE}/mindos/</code>，每 3 秒重试一次。</p></div></body></html>`)
}

async function backendReady() {
  try {
    const res = await fetch(`${BASE}/api/health`, { headers: { 'X-Requested-By': 'centaur-vdb' } })
    return res.ok
  } catch {
    return false
  }
}

async function createWindow() {
  const win = new BrowserWindow({
    width: 1280,
    height: 860,
    minWidth: 900,
    minHeight: 600,
    title: '知君',
    backgroundColor: '#FFFCF6',
    webPreferences: { contextIsolation: true, nodeIntegration: false, sandbox: true },
  })
  win.webContents.setWindowOpenHandler(({ url }) => {
    if (url.startsWith(BASE)) return { action: 'allow' }
    shell.openExternal(url)
    return { action: 'deny' }
  })
  win.webContents.on('will-navigate', (event, url) => {
    if (!url.startsWith(BASE)) {
      event.preventDefault()
      shell.openExternal(url)
    }
  })

  let loaded = false
  const tryLoad = async () => {
    if (loaded || win.isDestroyed()) return
    if (await backendReady()) {
      loaded = true
      await win.loadURL(`${BASE}/mindos/`)
      if (SMOKE) {
        console.log('ZHIJUN_SHELL_SMOKE: loaded ' + win.webContents.getURL())
        setTimeout(() => app.quit(), 500)
      }
    } else {
      if (win.webContents.getURL() === '') await win.loadURL(waitingPage())
      if (SMOKE) {
        console.log('ZHIJUN_SHELL_SMOKE: backend not ready, showed waiting page')
        setTimeout(() => app.quit(), 500)
        return
      }
      setTimeout(tryLoad, 3000)
    }
  }
  await tryLoad()
}

app.whenReady().then(createWindow)
app.on('window-all-closed', () => app.quit())
app.on('activate', () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow()
})
