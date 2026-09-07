'use strict'
const fs = require('node:fs')
const http = require('node:http')
const net = require('node:net')
const os = require('node:os')
const path = require('node:path')
const { execFileSync, spawn } = require('node:child_process')

const shell = path.resolve(__dirname, '..')
const metadata = require('../package.json')
const source = path.join(shell, 'release/mac-arm64/知君.app')
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))

async function freePort() {
  const server = net.createServer()
  await new Promise((resolve, reject) => server.once('error', reject).listen(0, '127.0.0.1', resolve))
  const port = server.address().port
  await new Promise(resolve => server.close(resolve))
  return port
}

function readPages(port) {
  return new Promise((resolve, reject) => {
    const request = http.get({ host: '127.0.0.1', port, path: '/json/list', timeout: 750 }, response => {
      const chunks = []
      response.on('data', chunk => chunks.push(chunk))
      response.on('end', () => {
        try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))) } catch (error) { reject(error) }
      })
    })
    request.on('timeout', () => request.destroy(new Error('SMOKE_HTTP_TIMEOUT')))
    request.on('error', reject)
  })
}

async function waitForDesktop(port, child) {
  const deadline = Date.now() + 20_000
  while (Date.now() < deadline) {
    if (child.exitCode !== null || child.signalCode !== null) throw new Error('PACKAGED_APP_EXITED_EARLY')
    try {
      const pages = await readPages(port)
      const page = pages.find(item => item.type === 'page' && item.url === 'zhijun://desktop/desktop.html#/')
      if (page && typeof page.title === 'string' && page.title.includes('知君')) return page
    } catch { /* The DevTools endpoint starts after the main process. */ }
    await delay(250)
  }
  throw new Error('PACKAGED_APP_WINDOW_TIMEOUT')
}

async function stop(child) {
  if (child.exitCode !== null || child.signalCode !== null) return
  child.kill('SIGTERM')
  const exited = new Promise(resolve => child.once('exit', resolve))
  await Promise.race([exited, delay(5_000)])
  if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL')
}

async function main() {
  if (process.platform !== 'darwin' || process.arch !== 'arm64') throw new Error('PACKAGE_PLATFORM_UNSUPPORTED')
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-package-smoke-'))
  const app = path.join(temporary, '知君.app')
  let child
  try {
    execFileSync('/usr/bin/ditto', [source, app], { stdio: ['ignore', 'pipe', 'pipe'], timeout: 60_000 })
    const port = await freePort()
    const executable = path.join(app, 'Contents/MacOS/知君')
    const env = { ...process.env, ZHIJUN_DESKTOP_USER_DATA: path.join(temporary, 'profile'), ZHIJUN_SHELL_NOGPU: '1' }
    delete env.ELECTRON_RUN_AS_NODE
    child = spawn(executable, [`--remote-debugging-port=${port}`], { env, stdio: ['ignore', 'pipe', 'pipe'] })
    const page = await waitForDesktop(port, child)
    console.log(JSON.stringify({ ok: true, version: metadata.version, title: page.title, url: page.url, relocated: true }))
  } finally {
    if (child) await stop(child)
    fs.rmSync(temporary, { recursive: true, force: true })
  }
}
main().catch(error => { console.error(error.message); process.exit(1) })
