'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const vm = require('node:vm')
const { inflateSync } = require('node:zlib')
const { APP_ICON, installDockIcon } = require('../app-icon.cjs')
const root = path.resolve(__dirname, '..')

// Inspect actual PNG samples: an RGBA header alone can still be fully opaque.
function rgba(png) {
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a')
  const width = png.readUInt32BE(16), height = png.readUInt32BE(20)
  assert.equal(png[24], 8)
  assert.equal(png[25], 6, 'RGBA color type is required')
  assert.equal(png[28], 0)
  const chunks = []
  for (let offset = 8; offset < png.length;) {
    const size = png.readUInt32BE(offset)
    assert.ok(offset + size + 12 <= png.length)
    if (png.subarray(offset + 4, offset + 8).toString() === 'IDAT') chunks.push(png.subarray(offset + 8, offset + 8 + size))
    offset += size + 12
  }
  const encoded = inflateSync(Buffer.concat(chunks)), stride = width * 4
  assert.equal(encoded.length, (stride + 1) * height)
  const pixels = Buffer.alloc(stride * height)
  for (let y = 0; y < height; y++) {
    const filter = encoded[y * (stride + 1)]
    assert.ok(filter <= 4)
    for (let x = 0; x < stride; x++) {
      const offset = y * stride + x
      const a = x >= 4 ? pixels[offset - 4] : 0
      const b = y ? pixels[offset - stride] : 0
      const c = y && x >= 4 ? pixels[offset - stride - 4] : 0
      const p = a + b - c, pa = Math.abs(p - a), pb = Math.abs(p - b), pc = Math.abs(p - c)
      const predictor = [0, a, b, Math.floor((a + b) / 2), pa <= pb && pa <= pc ? a : pb <= pc ? b : c][filter]
      pixels[offset] = encoded[y * (stride + 1) + 1 + x] + predictor
    }
  }
  return { width, height, pixels, pixel: (x, y) => [...pixels.subarray((y * width + x) * 4, (y * width + x) * 4 + 4)] }
}

test('application icon has real transparent margins, a warm opaque tile and an uncropped orange-blue centaur', () => {
  const image = rgba(fs.readFileSync(APP_ICON))
  assert.equal(image.width, 1024)
  assert.equal(image.height, 1024)
  for (let index = 0; index < 1024; index++) for (const edge of [0, 16, 1007, 1023]) {
    assert.equal(image.pixel(index, edge)[3], 0, 'transparent horizontal outer margin')
    assert.equal(image.pixel(edge, index)[3], 0, 'transparent vertical outer margin')
  }
  for (const [x, y] of [[72, 72], [951, 72], [72, 951], [951, 951]]) assert.equal(image.pixel(x, y)[3], 0)
  for (const [x, y] of [[512, 100], [100, 512], [923, 512], [512, 923], [512, 512]]) assert.equal(image.pixel(x, y)[3], 255)
  const [red, green, blue] = image.pixel(512, 110)
  assert.ok(red >= 248 && green >= 242 && blue >= 232 && red >= green && green > blue, 'warm ivory tile')
  let transparent = 0, orange = 0, horseBlue = 0, left = 1024, right = 0, top = 1024, bottom = 0
  for (let index = 0; index < image.pixels.length; index += 4) {
    const [r, g, b, a] = image.pixels.subarray(index, index + 4)
    if (!a) transparent++
    const orangePixel = a === 255 && r > 150 && r > g * 1.1 && g > b * 1.3 && b < 130
    const bluePixel = a === 255 && b > 80 && b > r * 1.4 && b > g * 0.8 && r < 100
    if (orangePixel) orange++
    if (bluePixel) horseBlue++
    if (orangePixel || bluePixel) {
      const x = (index / 4) % 1024, y = Math.floor(index / 4 / 1024)
      left = Math.min(left, x); right = Math.max(right, x); top = Math.min(top, y); bottom = Math.max(bottom, y)
    }
  }
  assert.ok(transparent / 1024 ** 2 > 0.20 && transparent / 1024 ** 2 < 0.40, 'substantial transparent surround, not a flattened square')
  assert.ok(orange > 30000 && horseBlue > 70000, 'both original brand colors remain visible')
  assert.ok(left >= 200 && right <= 824 && top >= 130 && bottom <= 894, 'whole figure has breathing room')
  assert.ok(bottom - top >= 700 && bottom - top <= 740, 'complete proportional figure occupies about 72% of canvas')
})

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
  assert.equal(hash(path.resolve(assets, provenance.renderer)), provenance.rendererSha256)
  assert.equal(hash(path.resolve(assets, provenance.buildScript)), provenance.buildScriptSha256)
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
    const type = icns.subarray(offset, offset + 4).toString()
    entries.add(type)
    if (type === 'ic10') assert.deepEqual(rgba(icns.subarray(offset + 8, offset + size)).pixels, rgba(png).pixels, '1024 ICNS and Dock PNG pixels agree')
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
