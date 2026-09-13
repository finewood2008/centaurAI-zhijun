'use strict';
const fs = require('node:fs');
const path = require('node:path');
const os = require('node:os');
const { spawnSync, execFileSync } = require('node:child_process');
const common = require('./windows-release-common.cjs');
const { verifyWindowsApp } = require('./verify-windows-package.cjs');

async function main(argv = process.argv.slice(2)) {
  common.assertBuildHost();
  const unsigned = common.parseUnsigned(argv);
  const source = path.join(common.releaseDir(unsigned), 'win-unpacked');
  await verifyWindowsApp(source, { unsigned });
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'zhijun-win-smoke-'));
  const target = path.join(temporary, 'relocated-app');
  let application, child;
  try {
    fs.cpSync(source, target, { recursive: true, errorOnExist: true });
    await verifyWindowsApp(target, { unsigned });
    const sidecar = spawnSync(path.join(target, 'resources', common.SIDECAR_RELATIVE), ['--help'], {
      windowsHide: true, encoding: 'utf8', timeout: 10000, maxBuffer: 65536,
    });
    if (sidecar.error || sidecar.status !== 0 || !`${sidecar.stdout}${sidecar.stderr}`.includes('direct-connect-timeout-ms')) throw new Error('SIDECAR_SMOKE_FAILED');
    const { _electron: electron } = require('../../mindos-web/node_modules/playwright');
    const env = { ...process.env, ZHIJUN_DESKTOP_USER_DATA: path.join(temporary, 'profile'), ZHIJUN_SHELL_NOGPU: '1' };
    delete env.ELECTRON_RUN_AS_NODE;
    delete env.ZHIJUN_DESKTOP_CONFIG;
    delete env.ZHIJUN_DESKTOP_MODE;
    application = await electron.launch({ executablePath: path.join(target, '知君.exe'), env, timeout: 30000 });
    child = application.process();
    const page = await application.firstWindow();
    await page.getByTestId('sign-in').waitFor({ timeout: 20000 });
    const state = await page.evaluate(async () => ({ snapshot: await window.zhijunDesktop.getSnapshot(),
      node: typeof window.require, process: typeof window.process }));
    if (state.snapshot?.data?.environment !== 'production' || state.node !== 'undefined' || state.process !== 'undefined'
        || page.url() !== 'zhijun://desktop/desktop.html#/') throw new Error('PACKAGED_RUNTIME_SMOKE_FAILED');
    const security = await application.evaluate(({ BrowserWindow, safeStorage }) => {
      const pref = BrowserWindow.getAllWindows()[0].webContents.getLastWebPreferences();
      const value = 'synthetic-windows-storage-smoke';
      return { sandbox: pref.sandbox, isolated: pref.contextIsolation, node: pref.nodeIntegration, web: pref.webSecurity,
        storage: safeStorage.isEncryptionAvailable() && safeStorage.decryptString(safeStorage.encryptString(value)) === value };
    });
    if (!security.sandbox || !security.isolated || security.node || !security.web || !security.storage) throw new Error('WINDOWS_SECURITY_SMOKE_FAILED');
    const report = { ok: true, platform: 'win32', arch: 'x64', signed: !unsigned, relocated: true, productionConfigLoaded: true,
      sidecarStarts: true, safeStorageRoundTrip: true, liveBoxTested: false, installerInstallTested: false };
    fs.writeFileSync(path.join(common.releaseDir(unsigned), 'smoke-windows.json'), `${JSON.stringify(report, null, 2)}\n`);
    console.log(JSON.stringify(report));
    return report;
  } finally {
    try {
      if (application) {
        try { await application.close(); } finally {
          if (child && child.exitCode === null && child.signalCode === null) {
            execFileSync(path.join(process.env.SystemRoot || 'C:\\Windows', 'System32/taskkill.exe'), ['/PID', String(child.pid), '/T', '/F'], { stdio: 'pipe', timeout: 10000 });
          }
        }
      }
    } finally { fs.rmSync(temporary, { recursive: true, force: true, maxRetries: 4, retryDelay: 250 }); }
  }
}
if (require.main === module) main().catch(error => { console.error(/^[A-Z0-9_]+$/.test(error.message) ? error.message : 'WINDOWS_PACKAGE_SMOKE_FAILED'); process.exitCode = 1; });
module.exports = { main };
