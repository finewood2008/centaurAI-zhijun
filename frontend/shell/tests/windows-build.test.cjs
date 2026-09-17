'use strict'
const test = require('node:test')
const assert = require('node:assert/strict')
const fs = require('node:fs')
const path = require('node:path')
const os = require('node:os')
const common = require('../scripts/windows-release-common.cjs')
const { buildSteps } = require('../scripts/build-windows-package.cjs')
test('old installers and reports are preserved separately before a new builder run', t => {
  const directory = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-old-win-build-'))
  t.after(() => fs.rmSync(directory, { recursive: true, force: true }))
  t.mock.method(common, 'releaseDir', () => directory)
  const name = `${common.artifactStem()}.exe`
  fs.writeFileSync(path.join(directory, name), 'old installer')
  fs.writeFileSync(path.join(directory, 'verification-windows.json'), 'old report')
  fs.writeFileSync(path.join(directory, 'unrelated.txt'), 'leave alone')
  require('../scripts/build-windows-package.cjs').archivePreviousArtifacts(false)
  assert.equal(fs.existsSync(path.join(directory, name)), false)
  const backup = fs.readdirSync(directory).find(value => value.startsWith('previous-build-'))
  assert.equal(fs.readFileSync(path.join(directory, backup, name), 'utf8'), 'old installer')
  assert.equal(fs.readFileSync(path.join(directory, 'unrelated.txt'), 'utf8'), 'leave alone')
})

test('Windows build requires native Windows x64 and Node 22', () => {
  assert.doesNotThrow(() => common.assertBuildHost('win32', 'x64', '22.16.0'))
  for (const args of [['darwin', 'arm64', '22.16.0'], ['win32', 'arm64', '22.16.0'], ['win32', 'x64', '23.0.0']]) {
    assert.throws(() => common.assertBuildHost(...args))
  }
})

test('unsigned mode must be explicitly requested and uses separate labeled output', () => {
  assert.equal(common.parseUnsigned([]), false)
  assert.equal(common.parseUnsigned(['--unsigned']), true)
  for (const args of [['--skip-verify'], ['--unsigned', '--unsigned'], ['--sign=false']]) assert.throws(() => common.parseUnsigned(args))
  assert.equal(path.basename(common.releaseDir()), 'release-windows')
  assert.equal(path.basename(common.releaseDir(true)), 'release-windows-unsigned')
  assert.match(common.artifactStem(true), /^Zhijun-UNSIGNED-/)
  assert.doesNotMatch(common.artifactStem(), /UNSIGNED/)
})

test('PE validation rejects wrong architectures and malformed headers', t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-win-pe-'))
  t.after(() => fs.rmSync(dir, { recursive: true, force: true }))
  const file = path.join(dir, 'fixture.exe')
  const buffer = Buffer.alloc(128)
  buffer.write('MZ'); buffer.writeUInt32LE(64, 0x3c); buffer.writeUInt32LE(0x4550, 64)
  buffer.writeUInt16LE(0x8664, 68); buffer.writeUInt16LE(0x20b, 88)
  fs.writeFileSync(file, buffer)
  assert.equal(common.assertPeX64(file), true)
  buffer.writeUInt16LE(0xaa64, 68); fs.writeFileSync(file, buffer)
  assert.throws(() => common.assertPeX64(file), /PE_X64_REQUIRED/)
  buffer.writeUInt32LE(0xffffff, 0x3c); fs.writeFileSync(file, buffer)
  assert.throws(() => common.assertPeX64(file), /PE_X64_REQUIRED/)
})

test('fixed native Windows sidecar has expected architecture and source pin when available', t => {
  const file = path.join(common.WORKSPACE_ROOT, 'data/desktop/native-direct-budget-20260913/windows-amd64/nexusaos-connectivity-sidecar.exe')
  if (!fs.existsSync(file)) { t.skip('native artifacts supplied separately on Windows build host'); return }
  assert.equal(common.assertPeX64(file), true)
  assert.equal(common.digest(file), common.SOURCE_SHA256)
})

test('build orchestration cannot skip tests, verification or smoke and does not mutate version', () => {
  for (const unsigned of [false, true]) {
    const steps = buildSteps(unsigned)
    assert.deepEqual(steps[0], { npm: ['run', 'test:windows'] })
    assert.equal(steps.at(-2).script, 'scripts/verify-windows-package.cjs')
    assert.equal(steps.at(-1).script, 'scripts/smoke-windows-package.cjs')
    assert.deepEqual(steps.at(-1).args, unsigned ? ['--unsigned'] : [])
    assert.doesNotMatch(JSON.stringify(steps), /version:bump|npm ci|npm install/)
    const builder = steps.find(step => step.script?.includes('electron-builder'))
    assert.ok(builder.args.includes('--x64'))
    assert.equal(builder.args.includes('--config.forceCodeSigning=false'), unsigned)
    assert.equal(builder.args.includes('--config.win.signExecutable=false'), unsigned)
  }
})

test('signing hook preserves staged sidecar bytes and signs other executables', async t => {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-win-sign-'))
  const previousStage = common.STAGE_DIR
  const previousUnsigned = process.env.ZHIJUN_WINDOWS_UNSIGNED
  t.after(() => {
    common.STAGE_DIR = previousStage
    if (previousUnsigned === undefined) delete process.env.ZHIJUN_WINDOWS_UNSIGNED
    else process.env.ZHIJUN_WINDOWS_UNSIGNED = previousUnsigned
    fs.rmSync(dir, { recursive: true, force: true })
  })
  common.STAGE_DIR = dir
  process.env.ZHIJUN_WINDOWS_UNSIGNED = '0'
  const sidecar = path.join(dir, common.SIDECAR_RELATIVE)
  fs.mkdirSync(path.dirname(sidecar), { recursive: true })
  fs.writeFileSync(sidecar, 'already signed fixture')
  fs.writeFileSync(path.join(dir, 'zhijun-product.json'), JSON.stringify({ connectivity: { sidecarSha256: common.digest(sidecar) } }))
  const sign = t.mock.method(common, 'signFile', () => {})
  const verify = t.mock.method(common, 'verifySignature', () => true)
  const hook = require('../scripts/sign-windows.cjs')
  await hook({ path: sidecar })
  assert.equal(sign.mock.callCount(), 0)
  assert.equal(verify.mock.callCount(), 1)
  await hook({ path: path.join(dir, 'installer.exe') })
  assert.equal(sign.mock.callCount(), 1)
  fs.appendFileSync(sidecar, 'tampered')
  await assert.rejects(hook({ path: sidecar }), /SIDECAR_SIGNING_HASH_MISMATCH/)
  process.env.ZHIJUN_WINDOWS_UNSIGNED = '1'
  await assert.rejects(hook({ path: path.join(dir, 'installer.exe') }), /UNSIGNED_BUILD_MUST_DISABLE_SIGN_EXECUTABLE/)
})

test('Windows config passes installed builder schema and matches production files without Mac resources', async () => {
  const { getConfig, validateConfiguration } = require('app-builder-lib/out/util/config/config')
  const { DebugLogger } = require('builder-util')
  const windows = await getConfig(common.SHELL_ROOT, 'electron-builder.windows.yml', null)
  const mac = await getConfig(common.SHELL_ROOT, 'electron-builder.yml', null)
  await validateConfiguration(windows, new DebugLogger())
  assert.deepEqual(windows.files, mac.files)
  assert.equal(windows.extraResources.length, 4)
  assert.doesNotMatch(JSON.stringify(windows.extraResources), /darwin|package-resources\//)
  assert.equal(windows.afterPack, 'scripts/after-pack-windows.cjs')
  assert.equal(windows.forceCodeSigning, true)
  assert.equal(windows.win.signtoolOptions.sign, 'scripts/sign-windows.cjs')
  assert.deepEqual(windows.win.signtoolOptions.signingHashAlgorithms, ['sha256'])
  assert.equal(windows.nsis.deleteAppDataOnUninstall, false)
})
