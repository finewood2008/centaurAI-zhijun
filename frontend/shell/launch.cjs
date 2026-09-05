'use strict'
const { spawn } = require('node:child_process')
const args = process.argv.slice(2)
if (args.some(arg => arg !== '--simulation')) {
  console.error('用法：npm start，或 npm run start:simulation')
  process.exit(2)
}
const env = { ...process.env }
delete env.ELECTRON_RUN_AS_NODE
if (args.includes('--simulation')) env.ZHIJUN_DESKTOP_MODE = 'simulation'
const child = spawn(require('electron'), [__dirname], { env, stdio: 'inherit' })
child.on('error', () => { console.error('Electron 启动失败，请在 frontend/shell 执行 npm ci。'); process.exitCode = 1 })
child.on('exit', code => { process.exitCode = code ?? 1 })
for (const signal of ['SIGINT', 'SIGTERM']) process.on(signal, () => child.kill(signal))
