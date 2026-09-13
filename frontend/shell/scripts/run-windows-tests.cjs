'use strict';
const path = require('node:path');
const { spawnSync } = require('node:child_process');
// Explicit portable suite: legacy POSIX filesystem/shebang tests still run in
// the unchanged Mac/Linux suite. Never depend on cmd.exe expanding a glob.
const names = [
  'app-icon',
  'business-bridge', 'consumer', 'materials', 'native-save', 'native-response-order',
  'product', 'product-quota-integration', 'read-scheduler', 'reconnect', 'request-latency', 'runtime',
  'provisioning-broker', 'provisioning-formal-preload', 'provisioning-renderer', 'provisioning-window',
  'microphone-permission', 'connection-budget-config', 'sdk-fallback', 'connection-errors',
  'windows-build', 'windows-content', 'windows-file-security', 'windows-verification',
];
function run() {
  const result = spawnSync(process.execPath, ['--test', '--test-concurrency=2', '--test-timeout=120000',
    ...names.map(name => path.join(__dirname, '..', 'tests', `${name}.test.cjs`))],
  { cwd: path.resolve(__dirname, '..'), stdio: 'inherit', shell: false });
  if (result.error || result.status !== 0) throw new Error('WINDOWS_TEST_SUITE_FAILED');
}
if (require.main === module) { try { run(); } catch (error) { console.error(error.message); process.exitCode = 1; } }
module.exports = { names, run };
