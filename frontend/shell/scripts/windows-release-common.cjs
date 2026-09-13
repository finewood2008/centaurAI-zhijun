'use strict'
const fs = require('node:fs')
const path = require('node:path')
const crypto = require('node:crypto')
const { execFileSync } = require('node:child_process')

const SHELL_ROOT = path.resolve(__dirname, '..')
const WORKSPACE_ROOT = path.resolve(SHELL_ROOT, '../..')
const STAGE_DIR = path.join(SHELL_ROOT, 'package-resources-windows')
const SIDECAR_RELATIVE = 'connectivity-sidecar/windows-amd64/nexusaos-connectivity-sidecar.exe'
const SOURCE_SHA256 = 'e86f840d671f24c3a0fb3b892dc20ddbc227fac1a6d3c3252298c330e47a8705'
const ELECTRON_VERSION = '37.10.3'
const digest = file => crypto.createHash('sha256').update(fs.readFileSync(file)).digest('hex')
const releaseDir = (unsigned = false) => path.join(SHELL_ROOT, unsigned ? 'release-windows-unsigned' : 'release-windows')
const artifactStem = (unsigned = false) => `Zhijun-${unsigned ? 'UNSIGNED-' : ''}${require('../package.json').version}-win-x64`

function parseUnsigned(argv = process.argv.slice(2)) {
  if (argv.length === 0) return false
  if (argv.length === 1 && argv[0] === '--unsigned') return true
  throw new Error('WINDOWS_RELEASE_ARGUMENT_INVALID')
}

function assertBuildHost(platform = process.platform, arch = process.arch, version = process.versions.node) {
  if (platform !== 'win32' || arch !== 'x64') throw new Error('WINDOWS_X64_BUILD_HOST_REQUIRED')
  if (!/^22\./.test(version)) throw new Error('NODE_22_REQUIRED')
}

function assertPeX64(file) {
  const stat = fs.lstatSync(file)
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('PE_FILE_INVALID')
  const data = fs.readFileSync(file)
  if (data.length < 64 || data.toString('ascii', 0, 2) !== 'MZ') throw new Error('PE_X64_REQUIRED')
  const offset = data.readUInt32LE(0x3c)
  if (offset < 64 || offset + 26 > data.length || data.readUInt32LE(offset) !== 0x4550
      || data.readUInt16LE(offset + 4) !== 0x8664 || data.readUInt16LE(offset + 24) !== 0x20b) {
    throw new Error('PE_X64_REQUIRED')
  }
  return true
}

function signingSettings(env = process.env) {
  const tool = env.ZHIJUN_WINDOWS_SIGNTOOL_PATH
  const thumbprint = env.ZHIJUN_WINDOWS_CERT_SHA1
  if (typeof tool !== 'string' || !path.isAbsolute(tool) || !/signtool\.exe$/i.test(tool)
      || typeof thumbprint !== 'string' || !/^[a-fA-F0-9]{40}$/.test(thumbprint)) {
    throw new Error('WINDOWS_SIGNING_CONFIGURATION_REQUIRED')
  }
  const stat = fs.lstatSync(tool)
  if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('WINDOWS_SIGNTOOL_INVALID')
  return { tool, thumbprint: thumbprint.toUpperCase() }
}

function verifySignature(file) {
  const { tool, thumbprint } = signingSettings()
  try {
    execFileSync(tool, ['verify', '/pa', '/all', '/tw', file], { stdio: 'pipe', timeout: 60_000 })
    // Pass paths as environment values, never interpolate executable text into PowerShell.
    const command = "$s=Get-AuthenticodeSignature -LiteralPath $env:ZHIJUN_VERIFY_FILE; if ($s.Status -ne 'Valid' -or $null -eq $s.SignerCertificate -or $s.SignerCertificate.Thumbprint -ne $env:ZHIJUN_VERIFY_CERT -or $null -eq $s.TimeStamperCertificate) { exit 1 }"
    const powershell = path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/WindowsPowerShell/v1.0/powershell.exe')
    execFileSync(powershell, ['-NoLogo', '-NoProfile', '-NonInteractive', '-Command', command], {
      stdio: 'pipe', timeout: 60_000,
      env: { ...process.env, ZHIJUN_VERIFY_FILE: path.resolve(file), ZHIJUN_VERIFY_CERT: thumbprint },
    })
  } catch { throw new Error('WINDOWS_SIGNATURE_INVALID') }
  return true
}

function signFile(file) {
  const { tool, thumbprint } = signingSettings()
  try {
    execFileSync(tool, ['sign', '/sha1', thumbprint, '/s', 'My', '/tr', 'http://timestamp.digicert.com',
      '/td', 'SHA256', '/fd', 'SHA256', file], { stdio: 'pipe', timeout: 120_000 })
  } catch { throw new Error('WINDOWS_SIGNING_FAILED') }
  verifySignature(file)
}

module.exports = { SHELL_ROOT, WORKSPACE_ROOT, STAGE_DIR, SIDECAR_RELATIVE, SOURCE_SHA256, ELECTRON_VERSION,
  releaseDir, artifactStem, parseUnsigned, assertBuildHost, assertPeX64, digest, signingSettings, signFile, verifySignature }
