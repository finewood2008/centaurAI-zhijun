'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const crypto = require('node:crypto');
const { createProductionAdapter } = require('../production/adapter.cjs');
const { createSdkRuntime } = require('../production/sdk-runtime.cjs');
const config = { consumerBaseUrl: 'https://consumer.example.test', connectivity: {
  applicationId: 'synthetic.zhijun', purpose: 'materials.read', requestedScopes: ['remote.p2p'], profile: 'SOVEREIGN_DIRECT_ONLY',
  gatewayHost: 'gateway.example.test', iceHost: 'ice.example.test' } };
const consumer = { current: async () => ({ accountId: 'account-synthetic', clientId: 'client-synthetic' }),
  signIn: async () => ({ accountId: 'account-synthetic' }), listDevices: async () => [], signOut: async () => {}, dispose: async () => {} };

test('missing trusted business bridge rejects before spawning SDK, even with complete connectivity config', async () => {
  let spawned = 0;
  const adapter = await createProductionAdapter({ config, consumer, runtimeFactory: async () => { spawned++; } });
  await assert.rejects(adapter.connect({ accountId: 'account-synthetic', deviceId: 'device-synthetic' }), { code: 'BUSINESS_BRIDGE_REQUIRED' });
  assert.equal(spawned, 0); await adapter.dispose();
});

test('bridge rejects wrong subject, maps only approved requests and closes the owned runtime once', async () => {
  let closed = 0; const requests = [];
  const adapter = await createProductionAdapter({ config, consumer,
    runtimeFactory: async () => ({ connect: async () => ({}), close: async () => { closed++; } }),
    bridge: { authorize: async ({ subject }) => ({ ...subject, request: async value => { requests.push(value); return { status: 200 }; } }) } });
  await assert.rejects(adapter.connect({ accountId: 'wrong-account', deviceId: 'device-synthetic' }), { code: 'AUTHENTICATION_REQUIRED' });
  const session = await adapter.connect({ accountId: 'account-synthetic', deviceId: 'device-synthetic' });
  assert.equal((await session.authorize()).deviceId, 'device-synthetic');
  await session.request({ method: 'GET', path: '/api/mindos/materials?limit=20&offset=0', headers: { Accept: 'application/json' } });
  assert.deepEqual(requests[0].headers, { Accept: ['application/json'] });
  assert.equal(requests[0].relative_path, '/api/mindos/materials?limit=20&offset=0');
  await session.close(); await session.close(); assert.equal(closed, 1); await adapter.dispose();
});

test('real SDK facade/admin/native protocol executes against a synthetic private-pipe sidecar', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-sdk-fixture-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const executable = path.join(directory, 'synthetic-sidecar');
  // Fixture is deliberately not a real transport; no network and no credentials.
  const code = `#!${process.execPath}\nconst readline = require('node:readline');\nreadline.createInterface({input:process.stdin}).on('line',line=>{\nconst m=JSON.parse(line); const r={protocol_version:m.protocol_version,type:'response',operation:m.operation,request_id:m.request_id};\nif(m.operation==='connect')r.session_id='session-synthetic';\nif(m.operation==='request'){r.status=200;r.headers={'content-type':'application/json'};r.body_base64=Buffer.from('{"synthetic":true}').toString('base64');}\nprocess.stdout.write(JSON.stringify(r)+'\\n');\n});\n`;
  await fs.writeFile(executable, code, { mode: 0o700 });
  let issued = 0;
  const runtime = await createSdkRuntime({ config: { ...config.connectivity, sidecarPath: executable,
    sidecarSha256: crypto.createHash('sha256').update(code).digest('hex') }, consumer: {
    createSession: async (deviceId, body) => {
      issued++; assert.equal(deviceId, 'device-synthetic'); assert.equal(body.applicationId, 'synthetic.zhijun');
      return { ...body, sessionId: 'session-synthetic', streamId: 'stream-synthetic', gatewayUrl: 'wss://gateway.example.test',
        token: 'synthetic-ticket', transportPolicy: 'DIRECT_ONLY', connectBefore: '2099-01-01T00:00:00Z',
        expiresAt: '2099-01-01T01:00:00Z', iceServers: [{ urls: ['stun:ice.example.test'] }] };
    },
  } });
  t.after(() => runtime.close());
  const session = await runtime.connect('device-synthetic');
  const result = await session.request({ method: 'GET', relative_path: '/api/mindos/materials?limit=20&offset=0', headers: { Accept: ['application/json'] } });
  assert.equal(result.status, 200); assert.equal(Buffer.from(result.body).toString(), '{"synthetic":true}'); assert.equal(issued, 1);
  await session.close(); await runtime.close();
  await assert.rejects(createSdkRuntime({ config: { ...config.connectivity, sidecarPath: executable, sidecarSha256: '0'.repeat(64) }, consumer }), { code: 'CONFIGURATION_REQUIRED' });
});

test('sign-out closes business authorization and native resources, including a late bridge result', async () => {
  let businessClosed = 0; let nativeClosed = 0; let finish;
  const bridgeResult = new Promise(resolve => { finish = resolve; });
  const adapter = await createProductionAdapter({ config, consumer,
    runtimeFactory: async () => ({ connect: async () => ({}), close: async () => { nativeClosed++; } }),
    bridge: { authorize: () => bridgeResult } });
  const session = await adapter.connect({ accountId: 'account-synthetic', deviceId: 'device-synthetic' });
  const authorization = session.authorize();
  await adapter.signOut();
  finish({ accountId: 'account-synthetic', deviceId: 'device-synthetic', request: async () => ({}), close: async () => { businessClosed++; } });
  await assert.rejects(authorization, { code: 'STALE_GENERATION' });
  await session.close(); assert.equal(nativeClosed, 1); assert.equal(businessClosed, 1); await adapter.dispose();
});

test('sign-out while reading the current identity prevents a late native connection from starting', async () => {
  let finishCurrent; let spawned = 0;
  const adapter = await createProductionAdapter({ config,
    consumer: { ...consumer, current: () => new Promise(resolve => { finishCurrent = resolve; }) },
    bridge: { authorize: async () => ({}) },
    runtimeFactory: async () => { spawned++; return { connect: async () => ({}), close: async () => {} }; } });
  const connecting = adapter.connect({ accountId: 'account-synthetic', deviceId: 'device-synthetic' });
  await adapter.signOut();
  finishCurrent({ accountId: 'account-synthetic', clientId: 'client-synthetic' });
  await assert.rejects(connecting, { code: 'STALE_GENERATION' });
  assert.equal(spawned, 0);
  await adapter.dispose();
});
