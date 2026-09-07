'use strict';
const assert = require('node:assert/strict');
const test = require('node:test');

test('vendored SDK correlates concurrent native responses across reversed, split and merged JSONL frames', async () => {
  const { spawnElectronSidecar } = await import('@nexusaos/connectivity-electron/process');
  // Synthetic private-pipe peer. No Consumer account, device or network access.
  const fixture = `
    const readline = require('node:readline');
    const requests = [];
    readline.createInterface({ input: process.stdin }).on('line', line => {
      requests.push(JSON.parse(line));
      if (requests.length !== 3) return;
      const responses = requests.map((r, i) => JSON.stringify({ protocol_version: 1,
        type: 'response', operation: 'request', request_id: r.request_id,
        status: 200 + i, headers: { 'content-type': 'text/plain' },
        body_base64: Buffer.from(r.request_id).toString('base64') }) + '\\n').reverse();
      process.stdout.write(responses[0].slice(0, 17));
      setImmediate(() => process.stdout.write(responses[0].slice(17) + responses[1] + responses[2]));
    });
  `;
  const native = spawnElectronSidecar({ executable: process.execPath, args: ['-e', fixture], requestTimeoutMs: 3000 });
  try {
    const order = [];
    const responses = await Promise.all([0, 1, 2].map(index => native.exchange.exchange({
      protocol_version: 1, type: 'request', operation: 'request', request_id: `parallel-${index}`,
      session_id: 'synthetic-session', request: { method: 'GET', relative_path: '/health' },
    }).then(response => { order.push(response.request_id); return response; })));
    assert.deepEqual(order, ['parallel-2', 'parallel-1', 'parallel-0']);
    assert.deepEqual(responses.map(response => response.status), [200, 201, 202]);
    assert.deepEqual(responses.map(response => Buffer.from(response.body_base64, 'base64').toString()),
      ['parallel-0', 'parallel-1', 'parallel-2']);
  } finally { await native.close(); }
});
