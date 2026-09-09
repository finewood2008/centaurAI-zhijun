'use strict';

const crypto = require('node:crypto');
const fs = require('node:fs/promises');
const fsConstants = require('node:fs').constants;
const path = require('node:path');
const { execFile: execFileCallback } = require('node:child_process');
const { promisify } = require('node:util');
const P = require('../runtime/product-policy.cjs');

const execFile = promisify(execFileCallback);
const RESOURCE_COMMAND = '/usr/lib/centauros/centauros-resource-evidence';
const RESOURCE_LIMIT = 65536;
const MARKDOWN_MIN = 12 * 1024;
const MARKDOWN_MAX = 16 * 1024;
const MARKDOWN_TARGET = 14 * 1024;
const TERMINAL = new Set(P.TERMINAL);
const UNIT_NAMES = new Set(['ollama.service', 'centaurai-database.service', 'centauros-remote-agent.service']);
const SYSTEMD_PROPERTIES = [
  'MainPID', 'NRestarts', 'ActiveState', 'SubState', 'Result', 'ControlGroup',
  'CPUQuotaPerSecUSec', 'MemoryHigh', 'MemoryMax', 'MemorySwapMax', 'TasksMax', 'OOMPolicy',
];
const CGROUP_FILES = [
  'memory.events', 'memory.current', 'memory.high', 'memory.max', 'memory.swap.max',
  'cpu.max', 'pids.max', 'pids.current',
];
const DOCKER_RESOURCES = ['Memory', 'MemorySwap', 'NanoCpus', 'CpuQuota', 'CpuPeriod', 'PidsLimit'];
const KERNEL_COUNTERS = ['oom', 'thermal', 'machineCheck', 'watchdog', 'gpuFault', 'storageError'];
const OLLAMA_BACKENDS = new Set(['cpu_avx2', 'cpu', 'rocm', 'vulkan', 'cuda', 'metal']);

class AcceptanceError extends Error {
  constructor(code, stage, meta = {}) {
    super(code);
    this.name = 'AcceptanceError';
    this.code = code;
    this.stage = stage;
    Object.assign(this, meta);
  }
}

const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value);
const sha256 = bytes => crypto.createHash('sha256').update(bytes).digest('hex');
const nowIso = () => new Date().toISOString();
const durationMs = started => Math.max(0, Math.round(Number(process.hrtime.bigint() - started) / 1e6));
const safeId = value => typeof value === 'string' && /^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$/.test(value);
const safeHost = value => typeof value === 'string' && value.length <= 253
  && (/^(?:25[0-5]|2[0-4]\d|1?\d?\d)(?:\.(?:25[0-5]|2[0-4]\d|1?\d?\d)){3}$/.test(value)
    || /^(?=.{1,253}$)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$/.test(value));
const safeUser = value => typeof value === 'string' && /^[a-z_][a-z0-9_-]{0,31}$/.test(value);

function parseArgs(argv) {
  const result = { plan: false, preflight: false, allowSavedLogin: false };
  const values = new Set(['--manifest', '--config', '--user-data', '--output', '--rounds']);
  for (let index = 0; index < argv.length; index++) {
    const arg = argv[index];
    if (values.has(arg)) {
      if (index + 1 >= argv.length || argv[index + 1].startsWith('--')) throw new AcceptanceError('INVALID_ARGUMENT', 'arguments');
      const key = { '--manifest': 'manifestPath', '--config': 'configPath', '--user-data': 'userDataPath', '--output': 'outputPath', '--rounds': 'rounds' }[arg];
      if (Object.hasOwn(result, key)) throw new AcceptanceError('INVALID_ARGUMENT', 'arguments');
      result[key] = argv[++index];
    } else if (arg === '--plan') result.plan = true;
    else if (arg === '--preflight') result.preflight = true;
    else if (arg === '--allow-saved-login') result.allowSavedLogin = true;
    else throw new AcceptanceError('INVALID_ARGUMENT', 'arguments');
  }
  if (!result.manifestPath) throw new AcceptanceError('MANIFEST_REQUIRED', 'arguments');
  if (result.plan && result.preflight) throw new AcceptanceError('INVALID_ARGUMENT', 'arguments');
  if (result.rounds !== undefined && (!/^[1-9]\d?$/.test(result.rounds) || Number(result.rounds) > 50)) {
    throw new AcceptanceError('INVALID_ARGUMENT', 'arguments');
  }
  if (result.rounds !== undefined) result.rounds = Number(result.rounds);
  for (const key of ['manifestPath', 'configPath', 'userDataPath', 'outputPath']) {
    if (result[key] !== undefined && (!path.isAbsolute(result[key]) || /[\r\n\0]/.test(result[key]))) {
      throw new AcceptanceError('ABSOLUTE_PATH_REQUIRED', 'arguments');
    }
  }
  return result;
}

function validateManifest(value) {
  const allowed = ['version', 'deviceId', 'host', 'sshUser', 'sshPort', 'topology', 'requiredContainers', 'rounds', 'expectedOllamaBackend'];
  if (!plain(value) || value.version !== 1 || Object.keys(value).some(key => !allowed.includes(key))
      || !safeId(value.deviceId) || !safeHost(value.host) || !safeUser(value.sshUser)
      || !['systemd', 'hybrid'].includes(value.topology) || !OLLAMA_BACKENDS.has(value.expectedOllamaBackend)) {
    throw new AcceptanceError('INVALID_MANIFEST', 'manifest');
  }
  const sshPort = value.sshPort ?? 22;
  const rounds = value.rounds ?? 10;
  const requiredContainers = value.requiredContainers ?? [];
  if (!Number.isSafeInteger(sshPort) || sshPort < 1 || sshPort > 65535
      || !Number.isSafeInteger(rounds) || rounds < 1 || rounds > 50
      || !Array.isArray(requiredContainers) || requiredContainers.length > 8
      || requiredContainers.some(item => typeof item !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$/.test(item))
      || new Set(requiredContainers).size !== requiredContainers.length
      || (value.topology === 'hybrid' && requiredContainers.length === 0)
      || (value.topology === 'systemd' && requiredContainers.length !== 0)) {
    throw new AcceptanceError('INVALID_MANIFEST', 'manifest');
  }
  const requiredUnits = value.topology === 'systemd'
    ? ['ollama.service', 'centaurai-database.service', 'centauros-remote-agent.service']
    : ['ollama.service', 'centauros-remote-agent.service'];
  return Object.freeze({ version: 1, deviceId: value.deviceId, host: value.host, sshUser: value.sshUser,
    sshPort, topology: value.topology, requiredContainers: Object.freeze([...requiredContainers]),
    requiredUnits: Object.freeze(requiredUnits), expectedOllamaBackend: value.expectedOllamaBackend, rounds });
}

async function readJsonFile(filename, maximum = 16384) {
  let file;
  try {
    file = await fs.open(filename, fsConstants.O_RDONLY | fsConstants.O_NOFOLLOW);
    const stat = await file.stat();
    if (!stat.isFile() || stat.size > maximum || (stat.mode & 0o022)) throw new Error('unsafe');
    const bytes = Buffer.alloc(maximum + 1);
    const { bytesRead } = await file.read(bytes, 0, bytes.length, 0);
    if (bytesRead > maximum) throw new Error('large');
    return JSON.parse(bytes.subarray(0, bytesRead).toString('utf8'));
  } catch {
    throw new AcceptanceError('INVALID_MANIFEST', 'manifest');
  } finally { await file?.close(); }
}

async function loadManifest(filename) {
  if (!path.isAbsolute(filename)) throw new AcceptanceError('ABSOLUTE_PATH_REQUIRED', 'manifest');
  return validateManifest(await readJsonFile(filename));
}

function generateMarkdown(runId, round) {
  if (!/^[a-f0-9]{16}$/.test(runId) || !Number.isSafeInteger(round) || round < 1 || round > 50) {
    throw new AcceptanceError('INVALID_FIXTURE_INPUT', 'fixture');
  }
  const heading = `# 知君稳定性验收材料 ${runId}-${String(round).padStart(2, '0')}\n\n`;
  const paragraph = [
    '这是一份自动生成的非敏感稳定性测试材料，用于验证上传、解析、摘要、实体、关系、标签、知识卡片草稿与原文读取链路。',
    '材料只描述验收过程，不包含账号、密码、访问令牌、真实人物、真实业务资料或设备配置。',
    '每一轮使用唯一文件名和唯一标记，正文规模保持稳定，以便比较模型负载、服务存活和资源变化。',
  ].join('') + '\n\n';
  let text = heading;
  let section = 1;
  while (Buffer.byteLength(text + `## 检查段落 ${section}\n\n${paragraph}`, 'utf8') <= MARKDOWN_TARGET) {
    text += `## 检查段落 ${section++}\n\n${paragraph}`;
  }
  while (Buffer.byteLength(text, 'utf8') < MARKDOWN_TARGET) text += '验';
  let bytes = Buffer.from(text, 'utf8');
  while (bytes.length > MARKDOWN_TARGET) {
    text = text.slice(0, -1);
    bytes = Buffer.from(text, 'utf8');
  }
  if (bytes.length < MARKDOWN_MIN || bytes.length > MARKDOWN_MAX) throw new AcceptanceError('FIXTURE_SIZE_INVALID', 'fixture');
  return Object.freeze({ fileName: `zhijun-stability-${runId}-${String(round).padStart(2, '0')}.md`,
    contentType: 'text/markdown', bytes, sha256: sha256(bytes) });
}

function availability(value, transform = item => item) {
  if (!plain(value) || !['available', 'unavailable'].includes(value.status)) return { status: 'unavailable', reason: 'invalid_evidence' };
  if (value.status === 'unavailable') return { status: 'unavailable', reason: typeof value.reason === 'string' ? value.reason.slice(0, 128) : 'unavailable' };
  try { return { status: 'available', value: transform(value.value) }; }
  catch { return { status: 'unavailable', reason: 'invalid_value' }; }
}

function sanitizeResourceEvidence(value) {
  if (!plain(value) || value.schemaVersion !== 1) throw new AcceptanceError('RESOURCE_EVIDENCE_INVALID', 'resource-sample');
  const text = item => {
    if (typeof item !== 'string' && typeof item !== 'number') throw new Error('invalid');
    return String(item).slice(0, 512);
  };
  const integer = item => { if (!Number.isSafeInteger(item)) throw new Error('invalid'); return item; };
  const system = {};
  for (const key of ['bootId', 'uptimeAndIdleSeconds']) system[key] = availability(value.system?.[key], text);
  system.memTotalKiB = availability(value.system?.memTotalKiB, integer);
  const systemd = {};
  for (const unit of UNIT_NAMES) {
    const source = value.systemd?.[unit];
    if (!plain(source) || !['available', 'unavailable'].includes(source.status)) {
      systemd[unit] = { status: 'unavailable', reason: 'invalid_evidence' }; continue;
    }
    const properties = {};
    for (const key of SYSTEMD_PROPERTIES) properties[key] = availability(source.properties?.[key], text);
    const files = {};
    for (const key of CGROUP_FILES) files[key] = availability(source.cgroup?.files?.[key], text);
    systemd[unit] = { status: source.status, properties, cgroup: { status: source.cgroup?.status === 'available' ? 'available' : 'unavailable', files } };
  }
  let docker;
  if (!plain(value.docker) || !['available', 'unavailable'].includes(value.docker.status) || !Array.isArray(value.docker.containers)) {
    docker = { status: 'unavailable', reason: 'invalid_evidence', containers: [], truncated: false };
  } else {
    docker = { status: value.docker.status, containers: value.docker.containers.slice(0, 16).map(item => ({
      id: typeof item?.id === 'string' ? item.id.slice(0, 128) : '', name: typeof item?.name === 'string' ? item.name.slice(0, 128) : '',
      state: typeof item?.state === 'string' ? item.state.slice(0, 32) : 'unknown',
      pid: Number.isSafeInteger(item?.pid) && item.pid >= 0 ? item.pid : null,
      oomKilled: typeof item?.oomKilled === 'boolean' ? item.oomKilled : null,
      restartCount: Number.isSafeInteger(item?.restartCount) ? item.restartCount : null,
      resources: Object.fromEntries(DOCKER_RESOURCES.map(key => [key, Number.isSafeInteger(item?.resources?.[key]) ? item.resources[key] : null])),
    })), truncated: value.docker.truncated === true };
    if (value.docker.status === 'unavailable') docker.reason = typeof value.docker.reason === 'string' ? value.docker.reason.slice(0, 128) : 'unavailable';
  }
  const thermal = { status: value.thermal?.status === 'available' ? 'available' : 'unavailable',
    truncated: value.thermal?.truncated === true, sensors: Array.isArray(value.thermal?.sensors) ? value.thermal.sensors.slice(0, 128).map(item => ({
      sensor: typeof item?.sensor === 'string' ? item.sensor.slice(0, 256) : '',
      type: typeof item?.type === 'string' ? item.type.slice(0, 128) : '',
      millidegreesC: availability(item?.millidegreesC, integer),
    })) : [] };
  const previousBootKernel = value.previousBootKernel?.status === 'available'
    ? { status: 'available', scope: 'previous_boot_last_2000_kernel_lines',
      linesExamined: Number.isSafeInteger(value.previousBootKernel.linesExamined) ? value.previousBootKernel.linesExamined : 0,
      counts: Object.fromEntries(KERNEL_COUNTERS.map(key => [key, Number.isSafeInteger(value.previousBootKernel.counts?.[key]) ? value.previousBootKernel.counts[key] : 0])) }
    : { status: 'unavailable', reason: typeof value.previousBootKernel?.reason === 'string' ? value.previousBootKernel.reason.slice(0, 128) : 'unavailable' };
  let ollamaCompute;
  if (value.ollamaCompute?.status === 'available' && OLLAMA_BACKENDS.has(value.ollamaCompute.backend)) {
    ollamaCompute = { status: 'available', scope: typeof value.ollamaCompute.scope === 'string'
      ? value.ollamaCompute.scope.slice(0, 128) : 'current_boot_bounded_journal',
    linesExamined: Number.isSafeInteger(value.ollamaCompute.linesExamined) ? value.ollamaCompute.linesExamined : 0,
    backend: value.ollamaCompute.backend };
  } else {
    ollamaCompute = { status: 'unavailable', reason: value.ollamaCompute?.status === 'unavailable'
      && typeof value.ollamaCompute.reason === 'string' ? value.ollamaCompute.reason.slice(0, 128) : 'invalid_backend_evidence' };
  }
  return { schemaVersion: 1, mode: value.mode === 'live' ? 'live' : 'fixture', system, systemd, docker, thermal, previousBootKernel, ollamaCompute };
}

function evidenceBootId(evidence) {
  const item = evidence.system?.bootId;
  return item?.status === 'available' && /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/i.test(item.value) ? item.value.toLowerCase() : null;
}

function propertyValue(evidence, unit, key) {
  const value = evidence.systemd?.[unit]?.properties?.[key];
  return value?.status === 'available' ? value.value : null;
}

function parseMemoryEvents(value) {
  if (typeof value !== 'string') return null;
  const result = {};
  for (const line of value.trim().split('\n')) {
    const match = line.match(/^([a-z_]+) ([0-9]+)$/);
    if (!match) return null;
    result[match[1]] = Number(match[2]);
  }
  return result;
}

function compareResourceEvidence(before, after, manifest) {
  const issues = [];
  const firstBoot = evidenceBootId(before), lastBoot = evidenceBootId(after);
  if (!firstBoot || !lastBoot) issues.push('BOOT_ID_UNAVAILABLE');
  else if (firstBoot !== lastBoot) issues.push('BOOT_ID_CHANGED');
  for (const [phase, evidence] of [['before', before], ['after', after]]) {
    if (evidence.ollamaCompute?.status !== 'available') issues.push(`OLLAMA_BACKEND_UNAVAILABLE:${phase}`);
    else if (evidence.ollamaCompute.backend !== manifest.expectedOllamaBackend) issues.push(`OLLAMA_BACKEND_UNEXPECTED:${phase}`);
  }
  for (const unit of manifest.requiredUnits) {
    if (!UNIT_NAMES.has(unit) || before.systemd?.[unit]?.status !== 'available' || after.systemd?.[unit]?.status !== 'available') {
      issues.push(`UNIT_EVIDENCE_UNAVAILABLE:${unit}`); continue;
    }
    for (const key of ['MainPID', 'NRestarts']) {
      const first = propertyValue(before, unit, key), last = propertyValue(after, unit, key);
      if (first === null || last === null) issues.push(`UNIT_${key.toUpperCase()}_UNAVAILABLE:${unit}`);
      else if (first !== last || (key === 'MainPID' && first === '0')) issues.push(`UNIT_${key.toUpperCase()}_CHANGED:${unit}`);
    }
    const beforeEvents = parseMemoryEvents(before.systemd[unit].cgroup?.files?.['memory.events']?.value);
    const afterEvents = parseMemoryEvents(after.systemd[unit].cgroup?.files?.['memory.events']?.value);
    if (!beforeEvents || !afterEvents) issues.push(`CGROUP_OOM_EVIDENCE_UNAVAILABLE:${unit}`);
    else for (const key of ['oom', 'oom_kill']) if ((afterEvents[key] ?? 0) !== (beforeEvents[key] ?? 0)) issues.push(`CGROUP_${key.toUpperCase()}_CHANGED:${unit}`);
  }
  const containers = evidence => new Map((evidence.docker?.containers || []).map(item => [item.name.replace(/^\//, ''), item]));
  const firstContainers = containers(before), lastContainers = containers(after);
  for (const name of manifest.requiredContainers) {
    const first = firstContainers.get(name), last = lastContainers.get(name);
    if (!first || !last) { issues.push(`CONTAINER_EVIDENCE_UNAVAILABLE:${name}`); continue; }
    if (!first.id || first.id !== last.id) issues.push(`CONTAINER_ID_CHANGED:${name}`);
    if (!Number.isSafeInteger(first.pid) || !Number.isSafeInteger(last.pid) || first.pid <= 0 || last.pid <= 0) issues.push(`CONTAINER_PID_UNAVAILABLE:${name}`);
    else if (first.pid !== last.pid) issues.push(`CONTAINER_PID_CHANGED:${name}`);
    if (first.restartCount === null || last.restartCount === null) issues.push(`CONTAINER_RESTART_UNAVAILABLE:${name}`);
    else if (first.restartCount !== last.restartCount) issues.push(`CONTAINER_RESTART_CHANGED:${name}`);
    if (first.oomKilled !== false || last.oomKilled !== false) issues.push(`CONTAINER_OOM:${name}`);
    if (first.state !== 'running' || last.state !== 'running') issues.push(`CONTAINER_NOT_RUNNING:${name}`);
  }
  return { passed: issues.length === 0, issues, bootId: lastBoot };
}

async function collectResourceEvidence(manifest, options = {}) {
  const runner = options.execFile || execFile;
  let stdout;
  try {
    ({ stdout } = await runner('/usr/bin/ssh', [
      '-F', '/dev/null', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=8', '-o', 'ConnectionAttempts=1', '-o', 'LogLevel=ERROR',
      '-p', String(manifest.sshPort), `${manifest.sshUser}@${manifest.host}`, RESOURCE_COMMAND, '--json',
    ], { timeout: 20000, maxBuffer: RESOURCE_LIMIT, encoding: 'utf8' }));
  } catch {
    throw new AcceptanceError('RESOURCE_EVIDENCE_UNAVAILABLE', 'resource-sample');
  }
  if (Buffer.byteLength(stdout || '', 'utf8') > RESOURCE_LIMIT) throw new AcceptanceError('RESOURCE_EVIDENCE_INVALID', 'resource-sample');
  let parsed;
  try { parsed = JSON.parse(stdout); } catch { throw new AcceptanceError('RESOURCE_EVIDENCE_INVALID', 'resource-sample'); }
  return sanitizeResourceEvidence(parsed);
}

async function createReceiptWriter(filename, runId) {
  const parent = path.dirname(filename);
  await fs.mkdir(parent, { recursive: true, mode: 0o700 });
  const resources = path.join(parent, `${path.basename(filename)}.resources`);
  await fs.mkdir(resources, { recursive: false, mode: 0o700 });
  const handle = await fs.open(filename, 'wx', 0o600);
  await handle.chmod(0o600);
  let sampleIndex = 0;
  return Object.freeze({
    filename,
    async append(value) {
      const bytes = Buffer.from(`${JSON.stringify(value)}\n`);
      await handle.write(bytes); await handle.sync();
    },
    async resource(phase, evidence) {
      const id = `${runId}-${String(++sampleIndex).padStart(3, '0')}-${phase}`;
      const bytes = Buffer.from(`${JSON.stringify(evidence)}\n`);
      const samplePath = path.join(resources, `${id}.json`);
      const sample = await fs.open(samplePath, 'wx', 0o600);
      try { await sample.write(bytes); await sample.sync(); await sample.chmod(0o600); } finally { await sample.close(); }
      return { id, sha256: sha256(bytes), bootId: evidenceBootId(evidence), status: 'captured' };
    },
    async close() { await handle.close(); },
  });
}

function operationRequest(operationId, params = {}, query = {}, body = null) {
  return { version: 1, requestId: crypto.randomUUID(), operationId, params, query, body };
}

async function invokeWithDeadline(product, method, input, deadline, guard = work => work, uncertainWrite = false) {
  if (Date.now() >= deadline) throw new AcceptanceError('STAGE_TIMEOUT', 'operation');
  let timer;
  try {
    const work = guard(Promise.resolve().then(() => product.invoke(method, input)));
    return await Promise.race([work, new Promise((_, reject) => {
      timer = setTimeout(() => reject(new AcceptanceError(uncertainWrite ? 'WRITE_OUTCOME_UNKNOWN' : 'STAGE_TIMEOUT', 'operation')),
        Math.max(1, deadline - Date.now()));
      timer.unref?.();
    })]);
  } finally { clearTimeout(timer); }
}

async function executeOperation(product, request, deadline, guard = work => work) {
  const definition = P.operationRequest(request);
  const startedAt = process.hrtime.bigint();
  let job;
  let cursor = 0;
  let status;
  let contentType;
  let ended = false;
  let blob;
  const chunks = [];
  try {
    job = await invokeWithDeadline(product, 'start', definition.value, deadline, guard, definition.operation.mutating);
    while (!ended) {
      if (Date.now() >= deadline) throw new AcceptanceError('STAGE_TIMEOUT', request.operationId);
      const page = await invokeWithDeadline(product, 'poll', { id: job.id, after: cursor, waitMs: Math.min(P.LIMITS.wait, 1000) }, deadline, guard);
      if (page.id !== job.id || page.cursor < cursor) throw new AcceptanceError('OPERATION_CONTRACT_MISMATCH', request.operationId);
      for (const event of page.events) {
        if (event.seq !== cursor + 1 || ended) throw new AcceptanceError('OPERATION_CONTRACT_MISMATCH', request.operationId);
        cursor = event.seq;
        if (event.kind === 'headers') {
          if (status !== undefined) throw new AcceptanceError('OPERATION_CONTRACT_MISMATCH', request.operationId);
          status = event.status; contentType = event.headers['content-type'];
        } else if (event.kind === 'chunk') chunks.push(Buffer.from(event.data));
        else if (event.kind === 'blob') {
          if (blob) throw new AcceptanceError('OPERATION_CONTRACT_MISMATCH', request.operationId);
          blob = event.blob;
        } else if (event.kind === 'error') throw new AcceptanceError(event.code || 'REMOTE_OPERATION_FAILED', request.operationId);
        else if (event.kind === 'end') ended = true;
      }
      if (TERMINAL.has(page.state) && !page.hasMore && !ended) throw new AcceptanceError('OPERATION_INCOMPLETE', request.operationId);
      if (!page.events.length) await guard(new Promise(resolve => setTimeout(resolve, 25)));
    }
    if (!Number.isInteger(status) || status < 200 || status >= 300) throw new AcceptanceError(`REMOTE_HTTP_${status || 0}`, request.operationId, { httpStatus: status });
    if (definition.operation.response === 'bytes') {
      if (!blob || chunks.length) throw new AcceptanceError('BLOB_RESPONSE_REQUIRED', request.operationId);
      return { jobId: job.id, blob, status, durationMs: durationMs(startedAt) };
    }
    if (!/^application\/json(?:\s*;|$)/i.test(contentType || '')) throw new AcceptanceError('OPERATION_CONTRACT_MISMATCH', request.operationId);
    let data;
    try { data = JSON.parse(Buffer.concat(chunks).toString('utf8')); }
    catch { throw new AcceptanceError('OPERATION_CONTRACT_MISMATCH', request.operationId); }
    return { jobId: job.id, data, status, durationMs: durationMs(startedAt) };
  } catch (error) {
    if (job && !ended) void product.invoke('cancel', { id: job.id, requestId: request.requestId }).catch(() => {});
    throw error;
  }
}

function assertMaterialId(value, stage) {
  if (!plain(value) || typeof value.materialId !== 'string' || !/^[^\u0000-\u001f\u007f]{1,256}$/.test(value.materialId)) {
    throw new AcceptanceError('MATERIAL_CONTRACT_MISMATCH', stage);
  }
  return value.materialId;
}

function analysisState(value) {
  const fields = ['summary', 'tagSuggestions', 'entities', 'relations'];
  if (!plain(value) || fields.some(key => !plain(value[key]) || typeof value[key].status !== 'string')) return 'invalid';
  const statuses = fields.map(key => value[key].status);
  if (statuses.includes('failed') || statuses.includes('unavailable') || statuses.includes('empty')) return 'failed';
  return statuses.every(status => status === 'ok') ? 'complete' : 'pending';
}

function draftState(value) {
  if (!plain(value) || typeof value.status !== 'string') return 'invalid';
  if (['failed', 'unavailable', 'empty'].includes(value.status)) return 'failed';
  if (value.status !== 'ok') return 'pending';
  return value.cardState === 'draft' && value.confirmed === false && typeof value.title === 'string' && value.title.trim()
    && typeof value.content === 'string' && value.content.trim() && typeof value.revision === 'string' && value.revision
    ? 'complete' : 'invalid';
}

async function executeRound({ product, runId, round, deadline, guard = work => work, pollIntervalMs = 5000 }) {
  const fixture = generateMarkdown(runId, round);
  const stages = {};
  let uploadId;
  try {
    let started = process.hrtime.bigint();
    const created = await invokeWithDeadline(product, 'uploadCreate', { requestId: crypto.randomUUID(), fileName: fixture.fileName,
      contentType: fixture.contentType, size: fixture.bytes.length }, deadline, guard, true);
    uploadId = created.id;
    if (created.state !== 'open' || created.received !== 0 || created.nextIndex !== 0 || created.size !== fixture.bytes.length) {
      throw new AcceptanceError('UPLOAD_CONTRACT_MISMATCH', 'upload-transfer');
    }
    const chunk = await invokeWithDeadline(product, 'uploadChunk', { id: uploadId, index: 0, bytes: new Uint8Array(fixture.bytes) }, deadline, guard, true);
    if (chunk.received !== fixture.bytes.length || chunk.nextIndex !== 1) throw new AcceptanceError('UPLOAD_CONTRACT_MISMATCH', 'upload-transfer');
    const complete = await invokeWithDeadline(product, 'uploadComplete', { id: uploadId }, deadline, guard, true);
    if (complete.state !== 'complete' || complete.sha256 !== fixture.sha256) throw new AcceptanceError('UPLOAD_HASH_MISMATCH', 'upload-transfer');
    stages.uploadTransferMs = durationMs(started);

    const upload = await executeOperation(product, operationRequest('post_api_mindos_uploads', {}, {}, {
      kind: 'multipart', fields: {}, files: [{ field: 'file', uploadId }],
    }), deadline, guard);
    stages.uploadRequestMs = upload.durationMs;
    const materialId = assertMaterialId(upload.data, 'upload-request');

    started = process.hrtime.bigint();
    let uploadStatusJobId;
    for (;;) {
      const status = await executeOperation(product, operationRequest('get_api_mindos_uploads_material_id', { materialId }), deadline, guard);
      uploadStatusJobId = status.jobId;
      assertMaterialId(status.data, 'upload-status');
      if (status.data.status === 'available') break;
      if (status.data.status === 'failed') throw new AcceptanceError(status.data.errorCode || 'MATERIAL_PROCESSING_FAILED', 'upload-status');
      if (!['uploaded', 'queued', 'processing'].includes(status.data.status)) throw new AcceptanceError('MATERIAL_CONTRACT_MISMATCH', 'upload-status');
      if (Date.now() + pollIntervalMs >= deadline) throw new AcceptanceError('ROUND_TIMEOUT', 'upload-status');
      await guard(new Promise(resolve => setTimeout(resolve, pollIntervalMs)));
    }
    stages.uploadReadyMs = durationMs(started);

    started = process.hrtime.bigint();
    let analysis;
    let draft;
    for (;;) {
      analysis = await executeOperation(product, operationRequest('get_api_mindos_materials_material_id_analysis', { materialId }), deadline, guard);
      draft = await executeOperation(product, operationRequest('get_api_mindos_materials_material_id_draft_card', { materialId }), deadline, guard);
      const a = analysisState(analysis.data), d = draftState(draft.data);
      if (a === 'invalid' || d === 'invalid') throw new AcceptanceError('DERIVED_CONTRACT_MISMATCH', 'derived');
      if (a === 'failed') throw new AcceptanceError('DERIVED_ANALYSIS_FAILED', 'derived');
      if (d === 'failed') throw new AcceptanceError(draft.data.errorCode || 'DRAFT_GENERATION_FAILED', 'derived');
      if (a === 'complete' && d === 'complete') break;
      if (Date.now() + pollIntervalMs >= deadline) throw new AcceptanceError('ROUND_TIMEOUT', 'derived');
      await guard(new Promise(resolve => setTimeout(resolve, pollIntervalMs)));
    }
    stages.derivedReadyMs = durationMs(started);

    const summary = await executeOperation(product, operationRequest('get_api_mindos_materials_material_id_summary', { materialId }), deadline, guard);
    if (summary.data?.materialId !== materialId || summary.data?.status !== 'ok' || typeof summary.data.text !== 'string' || !summary.data.text.trim()) {
      throw new AcceptanceError('SUMMARY_CONTRACT_MISMATCH', 'summary');
    }
    stages.summaryMs = summary.durationMs;
    const detail = await executeOperation(product, operationRequest('get_api_mindos_materials_material_id', { materialId }), deadline, guard);
    if (detail.data?.materialId !== materialId || detail.data?.status !== 'available'
        || detail.data?.draftCard?.revision !== draft.data.revision) throw new AcceptanceError('DETAIL_CONTRACT_MISMATCH', 'detail');
    stages.detailMs = detail.durationMs;

    started = process.hrtime.bigint();
    const raw = await executeOperation(product, operationRequest('get_api_mindos_materials_material_id_file', { materialId }), deadline, guard);
    if (raw.blob.size !== fixture.bytes.length || raw.blob.sha256 !== fixture.sha256) throw new AcceptanceError('RAW_FILE_DESCRIPTOR_MISMATCH', 'raw-range');
    const range = await invokeWithDeadline(product, 'blobRead', { id: raw.blob.id, offset: 0, limit: Math.min(65536, raw.blob.size) }, deadline, guard);
    const rangeBytes = Buffer.from(range.data);
    if (range.offset !== 0 || range.size !== fixture.bytes.length || rangeBytes.length !== fixture.bytes.length || range.hasMore
        || sha256(rangeBytes) !== fixture.sha256) throw new AcceptanceError('RAW_FILE_HASH_MISMATCH', 'raw-range');
    stages.rawRangeMs = durationMs(started);

    return { status: 'passed', round, materialId, uploadId, fixtureSha256: fixture.sha256, fixtureBytes: fixture.bytes.length,
      rawSha256: sha256(rangeBytes), rawBytes: rangeBytes.length,
      operationIds: { upload: upload.jobId, uploadStatus: uploadStatusJobId, analysis: analysis.jobId, draft: draft.jobId,
        summary: summary.jobId, detail: detail.jobId, raw: raw.jobId }, durationsMs: stages };
  } finally {
    if (uploadId) await guard(product.invoke('uploadCancel', { id: uploadId })).catch(() => {});
  }
}

function safePlan(manifest, rounds) {
  return { schemaVersion: 1, mode: 'plan', deviceId: manifest.deviceId, host: manifest.host,
    topology: manifest.topology, expectedOllamaBackend: manifest.expectedOllamaBackend, rounds,
    fixtureBytes: { minimum: MARKDOWN_MIN, target: MARKDOWN_TARGET, maximum: MARKDOWN_MAX },
    operations: ['post_api_mindos_uploads', 'get_api_mindos_uploads_material_id',
      'get_api_mindos_materials_material_id_analysis', 'get_api_mindos_materials_material_id_draft_card',
      'get_api_mindos_materials_material_id_summary', 'get_api_mindos_materials_material_id',
      'get_api_mindos_materials_material_id_file'],
    transport: 'authenticated-connectivity-sdk-direct-only', resourceCommand: RESOURCE_COMMAND,
    disconnectPolicy: 'fail-without-device-fallback' };
}

module.exports = {
  AcceptanceError, RESOURCE_COMMAND, MARKDOWN_MIN, MARKDOWN_MAX, MARKDOWN_TARGET,
  parseArgs, validateManifest, loadManifest, generateMarkdown, sanitizeResourceEvidence,
  evidenceBootId, compareResourceEvidence, collectResourceEvidence, createReceiptWriter,
  operationRequest, executeOperation, executeRound, safePlan, sha256,
};
