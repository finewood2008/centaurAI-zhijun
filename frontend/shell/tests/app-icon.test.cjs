'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const vm = require('node:vm')
const { APP_ICON, installDockIcon } = require('../app-icon.cjs')
const root = path.resolve(__dirname, '..')

test('only macOS uses the Dock API and uses the same application PNG', () => {
  const calls = []
  installDockIcon({ dock: { setIcon: icon => calls.push(icon) } }, 'darwin')
  assert.deepEqual(calls, [APP_ICON])
  for (const platform of ['linux', 'win32']) installDockIcon({}, platform)
})

test('checked-in PNG and ICNS retain their original centaur source and packaging paths', () => {
  const provenance = require('../assets/centaur-source.json')
  const hash = filename => crypto.createHash('sha256').update(fs.readFileSync(filename)).digest('hex')
  const assets = path.join(root, 'assets')
  assert.equal(hash(path.resolve(assets, provenance.source)), provenance.sourceSha256)
  for (const [name, digest] of Object.entries(provenance.files)) assert.equal(hash(path.join(assets, name)), digest)
  const png = fs.readFileSync(APP_ICON)
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a')
  assert.equal(png.readUInt32BE(16), 1024)
  assert.equal(png.readUInt32BE(20), 1024)
  const icns = fs.readFileSync(path.join(assets, 'centaur.icns'))
  assert.equal(icns.subarray(0, 4).toString(), 'icns')
  assert.equal(icns.readUInt32BE(4), icns.length)
  const entries = new Set()
  let offset = 8
  while (offset < icns.length) {
    const size = icns.readUInt32BE(offset + 4)
    assert.ok(size >= 8 && offset + size <= icns.length)
    entries.add(icns.subarray(offset, offset + 4).toString())
    offset += size
  }
  assert.equal(offset, icns.length)
  for (const type of ['ic07', 'ic08', 'ic09', 'ic10']) assert.ok(entries.has(type))
  const { build } = require('../package.json')
  assert.equal(path.resolve(root, build.icon), APP_ICON)
  assert.equal(build.mac.icon, 'assets/centaur.icns')
  assert.equal(build.win.icon, build.icon)
  assert.equal(build.linux.icon, build.icon)
})

test('the real main entry installs the Dock icon after ready, without launching Electron', async () => {
  let ready
  const readyPromise = new Promise(resolve => { ready = resolve })
  const calls = []
  const app = {
    dock: { setIcon: icon => calls.push(['dock', icon]) },
    setName() {}, setPath() {}, getPath: () => '/synthetic-app-data',
    requestSingleInstanceLock: () => true, on() {},
    whenReady: () => readyPromise, exit: code => calls.push(['exit', code]),
  }
  const imports = {
    electron: { app, protocol: { registerSchemesAsPrivileged() {} } },
    'node:path': path,
    'node:fs/promises': { access: async () => { throw new Error('Synthetic missing renderer: stop before creating a window') } },
    './app-icon.cjs': { APP_ICON, installDockIcon: value => installDockIcon(value, 'darwin') },
    './runtime/desktop-runtime.cjs': {}, './security.cjs': {},
  }
  vm.runInNewContext(fs.readFileSync(path.join(root, 'main.js'), 'utf8'), {
    require: name => { assert.ok(name in imports, name); return imports[name] },
    __dirname: root, process: { env: {} }, console: { error() {} },
  })
  assert.deepEqual(calls, [])
  ready()
  await new Promise(resolve => setImmediate(resolve))
  assert.deepEqual(calls, [['dock', APP_ICON], ['exit', 1]])
})
