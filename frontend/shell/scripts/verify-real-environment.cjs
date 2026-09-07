'use strict';
// Anonymous reachability checks only: no password, access token, or business body.
const net = require('node:net');
const { consumerBaseUrl } = require('../config/zhijun-product.example.json');
const { gatewayHost } = require('../config/zhijun-connectivity.json');
async function httpsCheck(url, consumer = false) {
  try {
    const response = await fetch(url, { redirect: 'error', signal: AbortSignal.timeout(8000), headers: { Accept: 'application/json' } });
    if (!consumer) { await response.body?.cancel(); return { httpStatus: response.status, passed: response.status === 200 }; }
    const reader = response.body?.getReader();
    let total = 0; const chunks = [];
    if (reader) {
      try {
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          total += value.length;
          if (total > 16384) throw new Error('RESPONSE_TOO_LARGE');
          chunks.push(value);
        }
      } finally { await reader.cancel(); }
    }
    let code;
    try { code = JSON.parse(Buffer.concat(chunks).toString('utf8')).code; } catch { /* Status-only rejection can be valid. */ }
    return { httpStatus: response.status, businessCode: Number.isInteger(code) ? code : null,
      passed: response.status === 401 || (response.status === 200 && code === 401), check: 'anonymous-devices-request-denied' };
  } catch { return { passed: false, code: 'HTTPS_CHECK_FAILED' }; }
}
async function tcpCheck(host) {
  return new Promise(resolve => {
    const socket = net.createConnection({ host, port: 22 });
    let done = false;
    function finish(reachable) { if (!done) { done = true; socket.destroy(); resolve({ host, port: 22, reachable }); } }
    socket.setTimeout(3000);
    socket.once('connect', () => finish(true));
    socket.once('error', () => finish(false));
    socket.once('timeout', () => finish(false));
  });
}
(async () => {
  const [consumer, gatewayLive, gatewayReady, homeDevice, officeDevice] = await Promise.all([
    httpsCheck(`${consumerBaseUrl}/app-api/devices`, true), httpsCheck(`https://${gatewayHost}/health/live`),
    httpsCheck(`https://${gatewayHost}/health/ready`), tcpCheck('192.168.1.18'), tcpCheck('192.168.31.248'),
  ]);
  console.log(JSON.stringify({ checkedAt: new Date().toISOString(), result: 'partial',
    scope: 'anonymous-network-preflight', realLoginValidated: false, realDeviceValidated: false,
    consumer, gatewayLive, gatewayReady, homeDevice, officeDevice }, null, 2));
  if (![consumer, gatewayLive, gatewayReady].every(check => check.passed)) process.exitCode = 1;
})();
