'use strict'
const fs = require('node:fs')
const path = require('node:path')
const { spawnSync } = require('node:child_process')
const common = require('./windows-release-common.cjs')

function npmCli(env = process.env) {
  const candidates = [env.npm_execpath, path.join(path.dirname(process.execPath), 'node_modules/npm/bin/npm-cli.js')]
  const result = candidates.find(value => typeof value === 'string' && path.basename(value) === 'npm-cli.js'
    && fs.existsSync(value) && fs.statSync(value).isFile())
  if (!result) throw new Error('NPM_CLI_NOT_FOUND_RUN_VIA_NPM')
  return result
}

function buildSteps(unsigned = false) {
  const flag = unsigned ? ['--unsigned'] : []
  return [
    { npm: ['run', 'test:windows'] },
    { npm: ['--prefix', '../mindos-web', 'run', 'test:desktop'] },
    { npm: ['run', 'test:e2e'] },
    { npm: ['run', 'build:desktop'] },
    { script: 'scripts/prepare-windows-package.cjs', args: flag },
    { script: 'node_modules/electron-builder/out/cli/cli.js', args: ['--config', 'electron-builder.windows.yml',
      '--win', 'nsis', 'zip', '--x64', '--publish', 'never', ...(unsigned ? [
        '--config.forceCodeSigning=false', '--config.win.signExecutable=false',
        '--config.directories.output=release-windows-unsigned',
        '--config.artifactName=Zhijun-UNSIGNED-${version}-win-${arch}.${ext}',
      ] : [])] },
    { script: 'scripts/verify-windows-package.cjs', args: flag },
    { script: 'scripts/smoke-windows-package.cjs', args: flag },
  ]
}

function archivePreviousArtifacts(unsigned) {
  const directory = common.releaseDir(unsigned);
  const targets = [`${common.artifactStem(unsigned)}.exe`, `${common.artifactStem(unsigned)}.zip`,
    'verification-windows.json', 'smoke-windows.json'];
  const existing = targets.filter(name => fs.existsSync(path.join(directory, name)));
  for (const name of existing) {
    const stat = fs.lstatSync(path.join(directory, name));
    if (!stat.isFile() || stat.isSymbolicLink()) throw new Error('PREVIOUS_ARTIFACT_INVALID');
  }
  if (!existing.length) return;
  const backup = fs.mkdtempSync(path.join(directory, 'previous-build-'));
  for (const name of existing) fs.renameSync(path.join(directory, name), path.join(backup, name));
  console.log('Previous installer/archive/reports retained in a previous-build directory.');
}

function buildWindowsPackage(unsigned = false) {
  common.assertBuildHost()
  if (!unsigned) common.signingSettings()
  common.assertPeX64(path.join(common.SHELL_ROOT, 'node_modules/electron/dist/electron.exe'))
  const npm = npmCli()
  const env = { ...process.env, ZHIJUN_WINDOWS_UNSIGNED: unsigned ? '1' : '0', CSC_IDENTITY_AUTO_DISCOVERY: 'false' }
  // Only the explicit Windows certificate-store signing hook is supported. Do not
  // accidentally import an unrelated Mac/PFX credential from a developer's shell.
  for (const key of ['CSC_LINK', 'WIN_CSC_LINK', 'CSC_KEY_PASSWORD', 'WIN_CSC_KEY_PASSWORD']) delete env[key]
  for (const step of buildSteps(unsigned)) {
    if (step.script?.includes('electron-builder')) archivePreviousArtifacts(unsigned);
    const args = step.npm ? [npm, ...step.npm] : [path.join(common.SHELL_ROOT, step.script), ...step.args]
    const result = spawnSync(process.execPath, args, { cwd: common.SHELL_ROOT, env, stdio: 'inherit', shell: false })
    if (result.error || result.status !== 0) throw new Error('WINDOWS_PACKAGE_STEP_FAILED')
  }
  console.log(unsigned ? 'UNSIGNED Windows build verified for internal testing only; do not distribute to customers.'
    : 'Signed Windows installation artifacts built, verified, and smoke-tested.')
}

if (require.main === module) {
  try { buildWindowsPackage(common.parseUnsigned()) }
  catch (error) { console.error(error.message); process.exitCode = 1 }
}
module.exports = { npmCli, buildSteps, buildWindowsPackage, archivePreviousArtifacts }
