'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const { mkdtemp, mkdir, writeFile, symlink, rm, readFile } = require('node:fs/promises')
const { tmpdir } = require('node:os')
const path = require('node:path')
const vm = require('node:vm')
const { ENTRY_URL, CSP, isEntryUrl, createInvokeHandler, createAssetHandler } = require('../security.cjs')

test('IPC checks the exact window, main frame, origin, and operation before dispatch', async () => {
  const calls = []
  const contents = { id: 7, isDestroyed: () => false, mainFrame: { url: ENTRY_URL } }
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
  await writeFile(path.join(directory, 'outside.js'), 'private fixture')
  await symlink(path.join(directory, 'outside.js'), path.join(root, 'assets', 'outside.js'))
  const serve = createAssetHandler(root)
  const entry = await serve(new Request(ENTRY_URL))
  assert.equal(entry.status, 200)
  assert.equal(entry.headers.get('Content-Security-Policy'), CSP)
  assert.equal(entry.headers.get('X-Content-Type-Options'), 'nosniff')
  assert.match(await entry.text(), /synthetic/)
  assert.equal((await serve(new Request('zhijun://desktop/assets/entry-a.js'))).status, 200)
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

test('preload exposes narrow methods, strips events, and unsubscribes exactly once', async () => {
  const callbacks = new Map(); const invocations = []; let api; let removed = 0
  const fakeIpc = {
    invoke: (...args) => { invocations.push(args); return Promise.resolve({ ok: true }) },
    on: (channel, listener) => callbacks.set(channel, listener),
    removeListener: (channel, listener) => { assert.equal(callbacks.get(channel), listener); removed++; callbacks.delete(channel) },
  }
  const script = await readFile(path.join(__dirname, '../preload.cjs'), 'utf8')
  vm.runInNewContext(script, { require(name) {
    assert.equal(name, 'electron')
    return { contextBridge: { exposeInMainWorld: (name, value) => { assert.equal(name, 'zhijunDesktop'); api = value } }, ipcRenderer: fakeIpc }
  } })
  assert.deepEqual(Object.keys(api).sort(), ['protocolVersion', 'getSnapshot', 'subscribe', 'beginSignIn', 'signInWithPassword', 'listDevices', 'connect', 'disconnect', 'signOut', 'materials', 'cancelRead'].sort())
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
})
