'use strict'
// Real Electron protocol/preload/build with synthetic runtime identity only.
// No Consumer, SDK, user profile, microphone or server is involved.
const test = require('node:test')
const assert = require('node:assert/strict')
const { mkdtemp, rm, writeFile } = require('node:fs/promises')
const { tmpdir } = require('node:os')
const path = require('node:path')
const { _electron: electron } = require('../../mindos-web/node_modules/playwright')
const { expect } = require('../../mindos-web/node_modules/playwright/test')

async function openFailedAccount(t) {
  const directory = await mkdtemp(path.join(tmpdir(), 'zhijun-product-navigation-e2e-'))
  const wrapper = path.join(directory, 'main.cjs')
  const root = path.resolve(__dirname, '..')
  await writeFile(wrapper, `
    'use strict';
    const runtimePath = ${JSON.stringify(path.join(root, 'runtime/desktop-runtime.cjs'))};
    const callbacks = new Set();
    const emptyCapabilities = {materialsRead:false,product:false,streamChat:false,uploads:false,matters:false,provisioning:false};
    let snapshot = {protocolVersion:1,generation:7,sequence:1,environment:'production',phase:'failed',subject:{accountId:'synthetic-electron-account'},capabilities:emptyCapabilities,error:{code:'TRANSPORT_UNAVAILABLE',message:'合成连接失败',recovery:'user_reconnect'}};
    globalThis.__navigationFixture = {business:0,reads:0,subscriptions:0};
    const runtime = {
      snapshot:()=>snapshot,
      subscribe(fn){callbacks.add(fn);globalThis.__navigationFixture.subscriptions++;return()=>callbacks.delete(fn)},
      invoke:async(operation,args)=>{
        if(operation==='getSnapshot'){globalThis.__navigationFixture.reads++;return{ok:true,generation:snapshot.generation,data:snapshot}}
        if(operation==='disconnect'||operation==='signOut'){
          snapshot={...snapshot,generation:snapshot.generation+1,sequence:snapshot.sequence+1,phase:operation==='signOut'?'signed_out':'selecting_device',subject:operation==='signOut'?null:{accountId:'synthetic-electron-account'},error:undefined};
          callbacks.forEach(fn=>fn(snapshot));return{ok:true,generation:snapshot.generation,data:snapshot}
        }
        if(operation==='listDevices')return{ok:true,generation:snapshot.generation,data:[]};
        globalThis.__navigationFixture.business++;
        return{ok:false,generation:snapshot.generation,error:{code:'SESSION_NOT_READY',message:'合成工作区未就绪',recovery:'user_reconnect'}};
      },
      dispose:async()=>{},
      mediaResponse:async()=>new Response(null,{status:403}),
    };
    require.cache[runtimePath]={id:runtimePath,filename:runtimePath,loaded:true,exports:{createDesktopRuntime:()=>runtime}};
    require(${JSON.stringify(path.join(root, 'main.js'))});
  `)
  const env = { ...process.env, ZHIJUN_DESKTOP_MODE: 'simulation', ZHIJUN_DESKTOP_CONFIG: '', ZHIJUN_DESKTOP_USER_DATA: directory, ZHIJUN_SHELL_NOGPU: '1' }
  delete env.ELECTRON_RUN_AS_NODE
  let app, timer
  const errors = [], consoleErrors = [], toasts = new Set(), network = []
  t.after(async () => {
    clearInterval(timer)
    try {
      if (app) {
        const child = app.process()
        try { await app.close() }
        finally { if (child.exitCode === null && child.signalCode === null) child.kill('SIGKILL') }
      }
    } finally { await rm(directory, { recursive: true, force: true }) }
  })
  app = await electron.launch({ executablePath: require('electron'), args: [wrapper], env, timeout: 20000 })
  const page = await app.firstWindow()
  page.on('pageerror', error => errors.push(error.message))
  page.on('console', message => { if (message.type() === 'error') consoleErrors.push(message.text()) })
  page.on('request', request => { if (/^(https?|wss?|file):/.test(request.url())) network.push(request.url()) })
  timer = setInterval(() => { void page.locator('.ws-toast__message').allTextContents().then(values => values.forEach(value => toasts.add(value)), () => {}) }, 30)
  const diagnostics = () => ({ errors, consoleErrors, toasts: [...toasts], network })
  return { app, page, diagnostics }
}

test('Electron custom protocol renders an initially failed signed-in account and survives renderer reload without business calls', async t => {
  const { app, page, diagnostics } = await openFailedAccount(t)
  try {
    const navigation = page.getByRole('navigation', { name: '主导航' })
    await expect(navigation).toBeVisible({ timeout: 10000 })
    await expect(navigation.getByRole('link')).toHaveCount(5)
    await expect(page.getByTestId('workspace-unavailable')).toBeVisible()
    await expect(page.getByTestId('error')).toContainText('TRANSPORT_UNAVAILABLE')
    assert.match(page.url(), /^zhijun:\/\/desktop\//)
    for (const label of ['对话', '回看', '我的本体', '资料与边界']) {
      await navigation.getByRole('link', { name: label, exact: true }).click()
      await expect(page.getByTestId('workspace-unavailable')).toBeVisible()
      await expect(page.getByTestId('workspace-unavailable')).toContainText(`${label}需要连接盒子后使用`)
    }
    await page.getByRole('link', { name: '偏好', exact: true }).click()
    await expect(page.getByTestId('workspace-unavailable')).toContainText('偏好需要连接盒子后使用')
    await page.reload({ waitUntil: 'domcontentloaded' })
    await expect(navigation).toBeVisible({ timeout: 10000 })
    await expect(page.getByTestId('workspace-unavailable')).toBeVisible()
    // The secure-connection card intentionally replaces the old account banner
    // on failure. Verify retained identity through the actual preload snapshot,
    // and keep the visible failed-state/recovery assertions below.
    const restored = await page.evaluate(() => window.zhijunDesktop.getSnapshot())
    assert.equal(restored.ok, true)
    assert.equal(restored.data.subject.accountId, 'synthetic-electron-account')
    assert.equal(restored.data.phase, 'failed')
    await expect(page.getByTestId('secure-connection-progress')).toContainText('连接暂未就绪')
    await expect(page.getByTestId('password-login')).toHaveCount(0)
    const stats = await app.evaluate(() => globalThis.__navigationFixture)
    assert.equal(stats.business, 0)
    assert.ok(stats.reads >= 2, 'reload obtains the existing main-process snapshot')
    assert.equal(stats.subscriptions, 1, 'renderer reload does not create another main runtime')
    assert.deepEqual(diagnostics(), { errors: [], consoleErrors: [], toasts: [], network: [] })
    await page.getByTestId('disconnect').click()
    await expect(page.getByTestId('refresh-devices')).toBeVisible()
    await expect(page.getByTestId('account')).toContainText('synthetic-electron-account')
    await expect(navigation).toBeVisible()
  } catch (error) {
    t.diagnostic(JSON.stringify({ ...diagnostics(), body: await page.locator('body').innerText().catch(() => 'unavailable') }))
    throw error
  }
})
