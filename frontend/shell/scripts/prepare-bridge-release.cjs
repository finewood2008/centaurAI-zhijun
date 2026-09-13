'use strict';

// Builds legacy v1 bridge inputs pinned to the historical compatibility baseline.
// It never logs in to or changes a box and is not the current CentaurOS release path.
const fs = require('node:fs');
const path = require('node:path');
const crypto = require('node:crypto');
const { execFileSync } = require('node:child_process');

const root = path.resolve(__dirname, '../../..');
const baseline = JSON.parse(fs.readFileSync(path.join(__dirname, 'legacy-bridge-release-baseline.json'), 'utf8'));
const args = process.argv.slice(2);
if (args.length !== 0 && args.length !== 3) throw new Error('usage: node prepare-bridge-release.cjs [os-worktree data-engine-worktree new-output-directory]');
const osRoot = path.resolve(args[0] || path.join(root, '../.worktrees/zhijun-bridge-os'));
const engineRoot = path.resolve(args[1] || path.join(root, '../.worktrees/zhijun-bridge-data-engine-current'));
const output = path.resolve(args[2] || path.join(root, 'data/desktop/legacy-bridge-v1-release'));
const run = (cwd, file, argv, options = {}) => execFileSync(file, argv, { cwd, maxBuffer: 32 * 1024 * 1024, ...options });
const git = (cwd, argv) => run(cwd, 'git', argv);
const hash = bytes => crypto.createHash('sha256').update(bytes).digest('hex');

function validateSource(directory, source) {
  if (fs.realpathSync(directory) !== directory) throw new Error('Source must not use a symlink');
  if (git(directory, ['status', '--porcelain']).length) throw new Error('Source worktree must be clean');
  if (git(directory, ['rev-parse', 'HEAD']).toString().trim() !== source.commit) throw new Error('Source differs from the pinned integration commit');
  git(directory, ['merge-base', '--is-ancestor', source.baseCommit, source.commit]);
}

validateSource(osRoot, baseline.sources.osAgent);
validateSource(engineRoot, baseline.sources.dataEngine);
if (fs.existsSync(output)) throw new Error('Refusing to overwrite an existing bundle');
const parent = path.dirname(output);
if (fs.realpathSync(parent) !== parent || !fs.statSync(parent).isDirectory()) throw new Error('Output parent must be an existing real directory');
const staging = fs.mkdtempSync(path.join(parent, '.bridge-build-'));
const manifest = {
  schemaVersion: 1, protocol: 'mindos-agent-bridge-v1', createdAt: new Date().toISOString(),
  deployed: false, signedRelease: false, containsPrivateKeys: false,
  osAgent: baseline.sources.osAgent, dataEngine: baseline.sources.dataEngine,
  goVersion: run(osRoot, 'go', ['version']).toString().trim(), files: [],
};

function write(relative, bytes, extra = {}) {
  const target = path.join(staging, relative);
  fs.mkdirSync(path.dirname(target), { recursive: true, mode: 0o700 });
  fs.writeFileSync(target, bytes, { flag: 'wx', mode: 0o600 });
  manifest.files.push({ path: relative, sha256: hash(bytes), bytes: bytes.length, ...extra });
}

try {
  for (const [name, directory, source] of [
    ['os-agent', osRoot, baseline.sources.osAgent], ['data-engine', engineRoot, baseline.sources.dataEngine],
  ]) {
    write(`${name}.patch`, git(directory, ['diff', '--binary', source.baseCommit, source.commit]));
  }
  const engineFiles = [
    'backend/server.py', 'backend/mindos/agent_bridge.py', 'backend/mindos/bridge_materials.py',
    'backend/mindos/api_contracts.py', 'backend/mindos/stores/connectivity_store.py', 'backend/mindos/uploads.py',
  ];
  for (const relative of engineFiles) {
    let before = null;
    const exists = git(engineRoot, ['ls-tree', '--name-only', baseline.sources.dataEngine.baseCommit, '--', relative]).length;
    if (exists) before = hash(git(engineRoot, ['show', `${baseline.sources.dataEngine.baseCommit}:${relative}`]));
    write(`data-engine/${relative}`, git(engineRoot, ['show', `${baseline.sources.dataEngine.commit}:${relative}`]), { baseSha256: before });
  }
  write('agent/remote-agent-applications.yaml', git(osRoot, ['show', `${baseline.sources.osAgent.commit}:manifests/remote-agent-applications.yaml`]));
  for (const arch of ['amd64', 'arm64']) {
    const relative = `agent/centauros-remote-agent-linux-${arch}`;
    const target = path.join(staging, relative);
    const flags = `-s -w -buildid= -X main.version=${baseline.sources.osAgent.commit.slice(0, 12)}-mindos-bridge-v1`;
    run(path.join(osRoot, 'remote-agent'), 'go', ['build', '-trimpath', '-ldflags', flags, '-o', target, './cmd/centauros-remote-agent'],
      { env: { ...process.env, CGO_ENABLED: '0', GOOS: 'linux', GOARCH: arch }, stdio: ['ignore', 'pipe', 'pipe'] });
    const bytes = fs.readFileSync(target);
    manifest.files.push({ path: relative, sha256: hash(bytes), bytes: bytes.length, platform: `linux-${arch}` });
  }
  write('README.md', Buffer.from('# Legacy v1 bridge inputs\n\nHistorical compatibility bundle only. It is not deployed, not signed, and not the current CentaurOS release path. No private keys are included. Do not use it to configure an NPU runtime or a current zhijun.workspace deployment.\n'));
  // Source changes while building must never produce a bundle claiming a clean pin.
  validateSource(osRoot, baseline.sources.osAgent);
  validateSource(engineRoot, baseline.sources.dataEngine);
  fs.writeFileSync(path.join(staging, 'manifest.json'), JSON.stringify(manifest, null, 2) + '\n', { flag: 'wx', mode: 0o600 });
  fs.renameSync(staging, output);
  console.log(JSON.stringify({ output, files: manifest.files.length, deployed: false }));
} catch (error) {
  fs.rmSync(staging, { recursive: true, force: true });
  throw error;
}
