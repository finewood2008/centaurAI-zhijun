'use strict';

const { spawn } = require('node:child_process');
const path = require('node:path');

const env = { ...process.env };
delete env.ELECTRON_RUN_AS_NODE;
const child = spawn(require('electron'), [path.join(__dirname, 'run-product-stability.cjs'), ...process.argv.slice(2)], {
  env, stdio: 'inherit',
});
child.once('error', () => {
  process.stderr.write('{"status":"failed","errorCode":"ELECTRON_START_FAILED","stage":"startup"}\n');
  process.exitCode = 1;
});
child.once('exit', code => { process.exitCode = code ?? 1; });
for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => child.kill(signal));

