'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { mkdtemp, mkdir, writeFile, symlink, rm, readFile } = require('node:fs/promises')
const { tmpdir } = require('node:os')
const path = require('node:path')
const vm = require('node:vm')
const { ENTRY_URL, CSP, isEntryUrl, shouldBlockRendererRequest, createInvokeHandler, createAssetHandler } = require('../security.cjs')

test('IPC checks the exact window, main frame, origin, and operation before dispatch', async () => {
  const calls = []
  let focused = false
  const contents = { id: 7, isDestroyed: () => false, isFocused: () => focused, mainFrame: { url: ENTRY_URL } }
  const runtime = { snapshot: () => ({ generation: 3 }), invoke: (...args) => { calls.push(args); return { ok: true } } }
  const handler = createInvokeHandler(runtime, () => contents)
  const event = { sender: contents, senderFrame: contents.mainFrame }
  assert.equal((await handler(event, 'getSnapshot', [])).ok, true)
  assert.deepEqual(calls, [['getSnapshot', [], 7]])
  const invalidEvents = [
    { ...event, sender: { ...contents } },
    { ...event, senderFrame: { url: ENTRY_URL } },
    { ...event, senderFrame: null },
  ]
  for (const invalid of invalidEvents) assert.equal((await handler(invalid, 'getSnapshot', [])).error.code, 'ACCESS_DENIED')
  for (const url of ['https://desktop/desktop.html', 'zhijun://desktop.evil/desktop.html',
    'zhijun://desktop/desktop.html?x=1', 'zhijun://user@desktop/desktop.html', 'zhijun://desktop/assets/test.js']) {
    contents.mainFrame.url = url
    assert.equal((await handler(event, 'getSnapshot', [])).error.code, 'ACCESS_DENIED')
  }
  contents.mainFrame.url = ENTRY_URL + '#materials'
  assert.equal((await handler(event, 'fetch', ['http://127.0.0.1:8618'])).error.code, 'INVALID_REQUEST')
  assert.equal((await handler(event, 'getSnapshot', {})).error.code, 'INVALID_REQUEST')
  assert.equal(calls.length, 1)
  const loginContext = { callId: 'remembered-login-1', expectedGeneration: 3 }
  assert.equal((await handler(event, 'product.requestMicrophone', [{}])).error.code, 'ACCESS_DENIED')
  assert.equal((await handler(event, 'openProvisioning', [loginContext])).error.code, 'ACCESS_DENIED')
  focused = true
  assert.equal((await handler(event, 'product.requestMicrophone', [{}])).ok, true)
  assert.deepEqual(calls[1], ['product.requestMicrophone', [{}], 7])
  for (const [operation, args] of [['getRememberedLogin', [loginContext]], ['signInWithSavedPassword', [loginContext, false]],
    ['resetPassword', [loginContext, { phone: '13800000000', code: '123456', password: 'Synthetic-new-password-1' }]],
    ['openProvisioning', [loginContext]]]) {
    for (const invalid of invalidEvents) assert.equal((await handler(invalid, operation, args)).error.code, 'ACCESS_DENIED')
    const before = calls.length
    assert.equal((await handler(event, operation, args)).ok, true)
    assert.deepEqual(calls[before], [operation, args, 7])
  }
  assert.equal(isEntryUrl(ENTRY_URL + '#materials'), true)
  assert.equal(isEntryUrl('invalid'), false)
})

test('asset protocol serves only build files, rejects traversal/symlinks and sets restrictive CSP', async t => {
  const directory = await mkdtemp(path.join(tmpdir(), 'zhijun-assets-'))
  t.after(() => rm(directory, { recursive: true, force: true }))
  const root = path.join(directory, 'dist')
  await mkdir(path.join(root, 'assets'), { recursive: true })
  await writeFile(path.join(root, 'desktop.html'), '<html>synthetic</html>')
  await writeFile(path.join(root, 'assets', 'entry-a.js'), 'export const value=1')
  await writeFile(path.join(root, 'assets', 'worker-a.mjs'), 'export const value=2')
  await writeFile(path.join(directory, 'outside.js'), 'private fixture')
  await symlink(path.join(directory, 'outside.js'), path.join(root, 'assets', 'outside.js'))
  const serve = createAssetHandler(root)
  const entry = await serve(new Request(ENTRY_URL))
  assert.equal(entry.status, 200)
  assert.equal(entry.headers.get('Content-Security-Policy'), CSP)
  assert.equal(entry.headers.get('X-Content-Type-Options'), 'nosniff')
  assert.equal(entry.headers.get('Permissions-Policy'), 'microphone=(self), camera=(), display-capture=()')
  assert.match(await entry.text(), /synthetic/)
  assert.equal((await serve(new Request('zhijun://desktop/assets/entry-a.js'))).status, 200)
  const worker = await serve(new Request('zhijun://desktop/assets/worker-a.mjs'))
  assert.equal(worker.status, 200); assert.equal(worker.headers.get('content-type'), 'text/javascript; charset=utf-8')
  for (const url of ['zhijun://evil/desktop.html', 'file:///desktop.html',
    'zhijun://desktop/assets/%2e%2e%2foutside.js', 'zhijun://desktop/assets/outside.js',
    'zhijun://desktop/desktop.html?file=outside.js', 'zhijun://desktop/other.html']) {
    const response = await serve(new Request(url))
    assert.ok(response.status >= 400, url)
    assert.equal(await response.text(), '')
  }
  assert.equal((await serve(new Request(ENTRY_URL, { method: 'POST' }))).status, 403)
  assert.equal(await (await serve(new Request(ENTRY_URL, { method: 'HEAD' }))).text(), '')
})

test('desktop document policy allows only the controlled media protocol for previews', async () => {
  const html = await readFile(path.join(__dirname, '../../mindos-web/desktop.html'), 'utf8')
  const main = await readFile(path.join(__dirname, '../main.js'), 'utf8')
  const policy = html.match(/http-equiv="Content-Security-Policy" content="([^"]+)"/)?.[1]
  assert.ok(policy)
  assert.match(policy, /(?:^|; )img-src 'self' data: blob: zhijun-media:(?:;|$)/)
  assert.match(policy, /(?:^|; )media-src blob: zhijun-media:(?:;|$)/)
  assert.match(policy, /(?:^|; )connect-src zhijun-media: blob:(?:;|$)/)
  assert.match(policy, /(?:^|; )worker-src 'self'(?:;|$)/)
  assert.match(policy, /(?:^|; )frame-src 'none'(?:;|$)/)
  assert.match(policy, /(?:^|; )object-src 'none'(?:;|$)/)
  assert.doesNotMatch(policy, /https?:|file:/)
  assert.match(main, /contextIsolation:\s*true,\s*sandbox:\s*true,\s*nodeIntegration:\s*false/)
  assert.match(main, /webSecurity:\s*true,\s*webviewTag:\s*false,\s*plugins:\s*false/)
})

test('renderer request filter allows only application assets and generation-bound media', () => {
  for (const url of ['https://outside.invalid/a', 'http://127.0.0.1/a', 'ws://outside.invalid/a',
    'wss://outside.invalid/a', 'file:///private/outside.pdf', 'chrome-extension://viewer/main.js',
    'chrome://resources/css/text.css', 'custom://outside/value', 'zhijun://outside/desktop.html',
    'zhijun-media://session/not-a-handle', 'blob:https://outside.invalid/id', 'data:text/html,hello', 'not a url']) {
    assert.equal(shouldBlockRendererRequest(url), true, url)
  }
  for (const url of ['zhijun://desktop/desktop.html', 'zhijun-media://session/' + 'a'.repeat(32),
    'blob:zhijun://desktop/5b2af8e0-ccdf-4664-b24d-8bb0ef9d1ea1', 'data:image/png;base64,AAAA']) {
    assert.equal(shouldBlockRendererRequest(url), false, url)
  }
})

test('preload exposes narrow methods, strips events, and unsubscribes exactly once', async () => {
  const callbacks = new Map(); const invocations = []; let api; let removed = 0
  const fakeIpc = {
    invoke: (...args) => { invocations.push(args); return Promise.resolve({ ok: true }) },
    on: (channel, listener) => callbacks.set(channel, listener),
    removeListener: (channel, listener) => { assert.equal(callbacks.get(channel), listener); removed++; callbacks.delete(channel) },
  }
  const script = await readFile(path.join(__dirname, '../preload.cjs'), 'utf8')
  const navigator = { userActivation: { isActive: false } }
  vm.runInNewContext(script, { navigator, require(name) {
    assert.equal(name, 'electron')
    return { contextBridge: { exposeInMainWorld: (name, value) => { assert.equal(name, 'zhijunDesktop'); api = value } }, ipcRenderer: fakeIpc }
  } })
  assert.deepEqual(Object.keys(api).sort(), ['protocolVersion', 'getSnapshot', 'subscribe', 'beginSignIn', 'signInWithPassword', 'getRememberedLogin', 'signInWithSavedPassword', 'sendRegistrationCode', 'resetPassword', 'registerWithPassword', 'listDevices', 'claimDevice', 'openProvisioning', 'connect', 'disconnect', 'signOut', 'materials', 'product', 'cancelRead'].sort())
  assert.deepEqual(Object.keys(api.product).sort(), ['start', 'poll', 'cancel', 'uploadCreate', 'uploadChunk', 'uploadComplete', 'uploadStatus', 'uploadCancel', 'blobRead', 'save', 'openMedia', 'closeMedia', 'requestMicrophone'].sort())
  assert.equal(Object.isFrozen(api.product), true)
  const microphone = await api.product.requestMicrophone({ callId: 'microphone-denied-1', expectedGeneration: 3 })
  assert.equal(microphone.error.code, 'OPERATION_NOT_ALLOWED')
  assert.equal(microphone.generation, 3)
  assert.equal(invocations.length, 0)
  const received = []
  const unsubscribe = api.subscribe((...args) => received.push(args))
  const snapshot = { phase: 'signed_out' }
  callbacks.get('zhijun:snapshot')({ secret: 'event must not escape' }, snapshot)
  assert.deepEqual(received, [[snapshot]])
  unsubscribe(); unsubscribe(); assert.equal(removed, 1)
  await api.materials.list({ callId: 'read-1234', expectedGeneration: 0 }, { limit: 20, offset: 0 })
  assert.equal(invocations[0][0], 'zhijun:invoke')
  assert.equal(invocations[0][1], 'materials.list')
  assert.equal(invocations[0][2].length, 2)
  navigator.userActivation.isActive = true
  const context = { callId: 'microphone-active-1', expectedGeneration: 3 }
  await api.product.requestMicrophone(context)
  assert.equal(invocations[1][1], 'product.requestMicrophone')
  assert.equal(invocations[1][2].length, 1)
  assert.equal(invocations[1][2][0], context)
  navigator.userActivation.isActive = false
  assert.equal((await api.product.requestMicrophone(context)).error.code, 'OPERATION_NOT_ALLOWED')
  assert.equal(invocations.length, 2)
  await api.getRememberedLogin(context)
  await api.signInWithSavedPassword(context, false)
  await api.sendRegistrationCode(context, '13800000000')
  await api.sendRegistrationCode(context, '13800000000', 'consumer_register')
  await api.sendRegistrationCode(context, '13800000000', 'consumer_reset_password')
  await api.resetPassword(context, { phone: '13800000000', code: '123456', password: 'Synthetic-new-password-1' })
  await api.registerWithPassword(context, { phone: '13800000000', password: 'Synthetic-pass-1', code: '123456', rememberPassword: true })
  await api.claimDevice(context, 'ABCDEFGH23')
  await api.openProvisioning(context)
  assert.deepEqual(invocations.slice(2).map(value => [value[0], value[1], [...value[2]]]), [
    ['zhijun:invoke', 'getRememberedLogin', [context]],
    ['zhijun:invoke', 'signInWithSavedPassword', [context, false]],
    ['zhijun:invoke', 'sendRegistrationCode', [context, '13800000000']],
    ['zhijun:invoke', 'sendRegistrationCode', [context, '13800000000', 'consumer_register']],
    ['zhijun:invoke', 'sendRegistrationCode', [context, '13800000000', 'consumer_reset_password']],
    ['zhijun:invoke', 'resetPassword', [context, { phone: '13800000000', code: '123456', password: 'Synthetic-new-password-1' }]],
    ['zhijun:invoke', 'registerWithPassword', [context, { phone: '13800000000', password: 'Synthetic-pass-1', code: '123456', rememberPassword: true }]],
    ['zhijun:invoke', 'claimDevice', [context, 'ABCDEFGH23']],
    ['zhijun:invoke', 'openProvisioning', [context]],
  ])
})
