'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const {
  AcceptanceError, MARKDOWN_MIN, MARKDOWN_MAX, parseArgs, validateManifest, generateMarkdown,
  sanitizeResourceEvidence, compareResourceEvidence, createReceiptWriter, executeRound, sha256,
  collectResourceEvidence,
} = require('../scripts/product-stability-core.cjs');

const available = value => ({ status: 'available', value });

function manifest(extra = {}) {
  return validateManifest({ version: 1, deviceId: 'centauros-device-0001', host: '192.168.0.7', sshUser: 'user',
    topology: 'systemd', ...extra });
}

function evidence({ bootId = '11111111-2222-3333-4444-555555555555', pid = '100', restarts = '0', oom = 'oom 0\noom_kill 0' } = {}) {
  const properties = Object.fromEntries(['MainPID', 'NRestarts', 'ActiveState', 'SubState', 'Result', 'ControlGroup',
    'CPUQuotaPerSecUSec', 'MemoryHigh', 'MemoryMax', 'MemorySwapMax', 'TasksMax', 'OOMPolicy']
    .map(key => [key, available(key === 'MainPID' ? pid : key === 'NRestarts' ? restarts : key === 'ControlGroup' ? '/system.slice/test.service' : 'ok')]));
  const files = Object.fromEntries(['memory.events', 'memory.current', 'memory.high', 'memory.max', 'memory.swap.max', 'cpu.max', 'pids.max', 'pids.current']
    .map(key => [key, available(key === 'memory.events' ? oom : '1')]));
  const unit = { status: 'available', properties, cgroup: { status: 'available', files } };
  return { schemaVersion: 1, mode: 'live', system: { bootId: available(bootId), uptimeAndIdleSeconds: available('10.0 5.0'), memTotalKiB: available(1024) },
    systemd: { 'ollama.service': unit, 'centaurai-database.service': unit, 'centauros-remote-agent.service': unit },
    docker: { status: 'available', containers: [], truncated: false },
    thermal: { status: 'available', sensors: [{ sensor: '/sys/temp', type: 'cpu', millidegreesC: available(42000) }], truncated: false },
    previousBootKernel: { status: 'available', scope: 'ignored', linesExamined: 4,
      counts: { oom: 0, thermal: 0, machineCheck: 0, watchdog: 0, gpuFault: 0, storageError: 0 } },
    secret: 'must-be-removed' };
}

test('manifest requires one explicit device and host and never supplies a fallback box', () => {
  const value = manifest();
  assert.equal(value.rounds, 10);
  assert.equal(value.deviceId, 'centauros-device-0001');
  assert.equal(value.host, '192.168.0.7');
  assert.deepEqual(value.requiredUnits, ['ollama.service', 'centaurai-database.service', 'centauros-remote-agent.service']);
  assert.throws(() => validateManifest({ ...value, fallbackHost: '192.168.1.18' }), AcceptanceError);
  assert.throws(() => validateManifest({ version: 1, host: '192.168.0.7', sshUser: 'user', topology: 'systemd' }), AcceptanceError);
  assert.throws(() => validateManifest({ version: 1, deviceId: value.deviceId, host: '-oProxyCommand=bad', sshUser: 'user', topology: 'systemd' }), AcceptanceError);
  assert.equal(parseArgs(['--manifest', '/tmp/device.json', '--rounds', '3', '--preflight']).rounds, 3);
});

test('generated Markdown is deterministic-sized, non-sensitive and unique per round', () => {
  const first = generateMarkdown('0123456789abcdef', 1);
  const second = generateMarkdown('0123456789abcdef', 2);
  assert.ok(first.bytes.length >= MARKDOWN_MIN && first.bytes.length <= MARKDOWN_MAX);
  assert.ok(second.bytes.length >= MARKDOWN_MIN && second.bytes.length <= MARKDOWN_MAX);
  assert.notEqual(first.fileName, second.fileName);
  assert.notEqual(first.sha256, second.sha256);
  assert.equal(first.sha256, sha256(first.bytes));
  assert.doesNotMatch(first.bytes.toString('utf8'), /password|token|139\d{8}/i);
});

test('resource evidence keeps its allowlist and detects reboot, PID and OOM changes', () => {
  const first = sanitizeResourceEvidence(evidence());
  assert.equal(JSON.stringify(first).includes('must-be-removed'), false);
  assert.equal(compareResourceEvidence(first, sanitizeResourceEvidence(evidence()), manifest()).passed, true);
  const changed = sanitizeResourceEvidence(evidence({ bootId: 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee', pid: '101', restarts: '1', oom: 'oom 1\noom_kill 1' }));
  const result = compareResourceEvidence(first, changed, manifest());
  assert.equal(result.passed, false);
  assert.ok(result.issues.includes('BOOT_ID_CHANGED'));
  assert.ok(result.issues.includes('UNIT_MAINPID_CHANGED:ollama.service'));
  assert.ok(result.issues.includes('CGROUP_OOM_KILL_CHANGED:centaurai-database.service'));
});

test('SSH evidence ignores user config, uses one explicit host and a fixed remote command', async () => {
  let invocation;
  const expected = evidence();
  const result = await collectResourceEvidence(manifest(), { execFile: async (...args) => {
    invocation = args; return { stdout: JSON.stringify(expected), stderr: 'private remote diagnostics' };
  } });
  assert.equal(result.system.bootId.value, '11111111-2222-3333-4444-555555555555');
  assert.equal(invocation[0], '/usr/bin/ssh');
  assert.deepEqual(invocation[1].slice(0, 2), ['-F', '/dev/null']);
  assert.equal(invocation[1].filter(value => value === 'user@192.168.0.7').length, 1);
  assert.deepEqual(invocation[1].slice(-2), ['/usr/lib/centauros/centauros-resource-evidence', '--json']);
  assert.equal(JSON.stringify(result).includes('private remote diagnostics'), false);
});

test('receipt and detached resource samples are created with mode 0600', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-stability-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'receipt.jsonl');
  const writer = await createReceiptWriter(filename, '0123456789abcdef');
  const safe = sanitizeResourceEvidence(evidence());
  const ref = await writer.resource('preflight', safe);
  await writer.append({ schemaVersion: 1, kind: 'preflight', status: 'passed', resourceSample: ref });
  await writer.close();
  assert.equal((await fs.stat(filename)).mode & 0o777, 0o600);
  const resource = path.join(directory, 'receipt.jsonl.resources', `${ref.id}.json`);
  assert.equal((await fs.stat(resource)).mode & 0o777, 0o600);
  assert.equal((await fs.readFile(filename, 'utf8')).includes('secret'), false);
});

test('one round uses production operation IDs, verifies the original hash and cancels the upload handle', async () => {
  const runId = '0123456789abcdef';
  const fixture = generateMarkdown(runId, 1);
  const uploadId = 'a'.repeat(32), blobId = 'b'.repeat(32);
  let transferred = Buffer.alloc(0), currentOperation;
  const calls = [];
  let nextJob = 1;
  const responses = operationId => {
    const materialId = 'mindos_stability_0001';
    if (operationId === 'post_api_mindos_uploads') return { materialId, status: 'queued' };
    if (operationId === 'get_api_mindos_uploads_material_id') return { materialId, status: 'available' };
    if (operationId === 'get_api_mindos_materials_material_id_analysis') return { materialId,
      summary: { status: 'ok', text: '摘要' }, tagSuggestions: { status: 'ok', items: [] },
      entities: { status: 'ok', items: [] }, relations: { status: 'ok', items: [] } };
    if (operationId === 'get_api_mindos_materials_material_id_draft_card') return { materialId, status: 'ok', cardState: 'draft',
      title: '草稿', content: '正文', revision: 'rev-1', confirmed: false };
    if (operationId === 'get_api_mindos_materials_material_id_summary') return { materialId, status: 'ok', text: '摘要' };
    if (operationId === 'get_api_mindos_materials_material_id') return { materialId, status: 'available', draftCard: { revision: 'rev-1' } };
    throw new Error(`unexpected operation ${operationId}`);
  };
  const product = { async invoke(method, input) {
    calls.push([method, input.operationId || input.id]);
    if (method === 'uploadCreate') return { id: uploadId, state: 'open', size: input.size, received: 0, nextIndex: 0 };
    if (method === 'uploadChunk') { transferred = Buffer.from(input.bytes); return { id: uploadId, state: 'open', size: fixture.bytes.length, received: transferred.length, nextIndex: 1 }; }
    if (method === 'uploadComplete') return { id: uploadId, state: 'complete', size: fixture.bytes.length, received: transferred.length, nextIndex: 1, sha256: sha256(transferred) };
    if (method === 'uploadCancel') return { id: uploadId, state: 'cancelled', size: fixture.bytes.length, received: transferred.length, nextIndex: 1 };
    if (method === 'start') { currentOperation = input.operationId; return { id: (nextJob++).toString(16).padStart(32, '0'), state: 'queued', cursor: 0 }; }
    if (method === 'poll') {
      if (currentOperation === 'get_api_mindos_materials_material_id_file') return { id: input.id, state: 'succeeded', cursor: 3, hasMore: false, events: [
        { seq: 1, kind: 'headers', status: 200, headers: { 'content-type': 'text/markdown' } },
        { seq: 2, kind: 'blob', blob: { id: blobId, size: transferred.length, sha256: sha256(transferred), contentType: 'text/markdown' } },
        { seq: 3, kind: 'end' },
      ] };
      const bytes = Buffer.from(JSON.stringify(responses(currentOperation)));
      return { id: input.id, state: 'succeeded', cursor: 3, hasMore: false, events: [
        { seq: 1, kind: 'headers', status: 200, headers: { 'content-type': 'application/json' } },
        { seq: 2, kind: 'chunk', data: new Uint8Array(bytes) }, { seq: 3, kind: 'end' },
      ] };
    }
    if (method === 'blobRead') return { id: blobId, offset: 0, size: transferred.length,
      contentType: 'text/markdown', sha256: sha256(transferred), data: new Uint8Array(transferred), hasMore: false };
    throw new Error(`unexpected method ${method}`);
  } };
  const result = await executeRound({ product, runId, round: 1, deadline: Date.now() + 5000, pollIntervalMs: 1 });
  assert.equal(result.status, 'passed');
  assert.equal(result.fixtureSha256, result.rawSha256);
  assert.equal(result.fixtureBytes, fixture.bytes.length);
  assert.deepEqual(calls.filter(([method]) => method === 'start').map(([, operation]) => operation), [
    'post_api_mindos_uploads', 'get_api_mindos_uploads_material_id',
    'get_api_mindos_materials_material_id_analysis', 'get_api_mindos_materials_material_id_draft_card',
    'get_api_mindos_materials_material_id_summary', 'get_api_mindos_materials_material_id',
    'get_api_mindos_materials_material_id_file',
  ]);
  assert.equal(calls.at(-1)[0], 'uploadCancel');
});
