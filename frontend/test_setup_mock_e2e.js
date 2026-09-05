/* Electron setup-window Mock 流程端到端验收（阶段 3）。
 *
 * 前置：以开发模式启动应用并开启本地添加骨架 + 自动打开 Setup 窗口：
 *   PowerShell:
 *     $env:MINDOS_ENABLE_LOCAL_ADD_SKELETON='1'
 *     $env:MINDOS_OPEN_SETUP_ON_START='1'
 *     npx electron . --remote-debugging-port=9224
 * 然后运行：
 *     node test_setup_mock_e2e.js
 *
 * 验收路径（真实窗口内，CDP 驱动 UI 手势）：
 *   待开始 → 开始添加 → 选择设备（两候选）→ 身份验证 → 配置 Wi-Fi →
 *   双向校验 → 等待确认 → 已完成；另验收取消→重试恢复。
 */
const assert = require('node:assert/strict');

const DEBUG_PORT = 9224;
const BASE = `http://127.0.0.1:${DEBUG_PORT}`;

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function findSetupTarget() {
  const deadline = Date.now() + 20000;
  for (;;) {
    try {
      const targets = await fetch(`${BASE}/json/list`).then((response) => response.json());
      const page = targets.find((target) => target.type === 'page' && target.url.includes('/renderer/setup.html'));
      if (page) return page;
    } catch {}
    if (Date.now() > deadline) throw new Error('未找到 Setup 窗口 target（确认已带 --remote-debugging-port 启动）');
    await sleep(300);
  }
}

async function connect(page) {
  const socket = new WebSocket(page.webSocketDebuggerUrl);
  let nextId = 1;
  const pending = new Map();
  const errors = [];
  socket.addEventListener('message', (event) => {
    const message = JSON.parse(event.data);
    if (message.id && pending.has(message.id)) {
      const { resolve, reject } = pending.get(message.id);
      pending.delete(message.id);
      if (message.error) reject(new Error(message.error.message));
      else resolve(message.result);
    }
    if (message.method === 'Runtime.exceptionThrown') {
      errors.push(message.params.exceptionDetails.text || 'renderer exception');
    }
  });
  await new Promise((resolve, reject) => {
    socket.addEventListener('open', resolve, { once: true });
    socket.addEventListener('error', reject, { once: true });
  });
  function command(method, params = {}) {
    const id = nextId++;
    socket.send(JSON.stringify({ id, method, params }));
    return new Promise((resolve, reject) => pending.set(id, { resolve, reject }));
  }
  async function evaluate(expression) {
    const result = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  }
  async function waitFor(expression, label, timeoutMs = 15000) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const value = await evaluate(expression);
      if (value) return value;
      if (Date.now() > deadline) throw new Error(`等待超时：${label}`);
      await sleep(200);
    }
  }
  await command('Runtime.enable');
  return { evaluate, waitFor, errors, close: () => socket.close() };
}

async function main() {
  const page = await findSetupTarget();
  const cdp = await connect(page);

  // 1) 初始：待开始，出现「开始添加」
  await cdp.waitFor(`document.getElementById('setup-start') && !document.getElementById('setup-start').hidden`, '开始按钮');
  assert.equal(await cdp.evaluate(`document.getElementById('setup-badge').textContent`), '待开始');

  // 2) 开始添加 → 候选选择（两个 Mock 候选）
  await cdp.evaluate(`document.getElementById('setup-start').click()`);
  await cdp.waitFor(`document.querySelectorAll('#setup-candidates li').length === 2`, '两候选');
  assert.equal(await cdp.evaluate(`document.getElementById('setup-badge').textContent`), '选择设备');

  // 3) 取消 → 重试恢复（覆盖取消/权威恢复场景）
  await cdp.evaluate(`document.getElementById('setup-cancel').click()`);
  await cdp.waitFor(`document.getElementById('setup-badge').textContent === '已取消'`, '取消态');
  await cdp.evaluate(`document.getElementById('setup-resume').click()`);
  await cdp.waitFor(`document.querySelectorAll('#setup-candidates li').length === 2`, '恢复后候选');

  // 4) 选择第一个候选 → 身份验证
  await cdp.evaluate(`document.querySelectorAll('#setup-candidates li')[0].click()`);
  await cdp.waitFor(`document.getElementById('setup-badge').textContent === '身份验证'`, '身份验证');
  assert.equal(
    await cdp.evaluate(`Boolean(document.getElementById('setup-action') && !document.getElementById('setup-action').hidden)`),
    true,
    '身份验证阶段应显示推进按钮',
  );

  // 5) 继续认证 → 配置 Wi-Fi
  await cdp.evaluate(`document.getElementById('setup-action').click()`);
  await cdp.waitFor(`!document.getElementById('setup-wifi').hidden`, 'Wi-Fi 表单');
  assert.equal(await cdp.evaluate(`document.getElementById('setup-badge').textContent`), '配置 Wi-Fi');

  // 6) 提交 Wi-Fi → 双向校验 → 等待确认 → 已完成
  await cdp.evaluate(`document.getElementById('setup-wifi-ssid').value = 'Centaur-5G'`);
  await cdp.evaluate(`document.getElementById('setup-wifi-password').value = 'mock-secret'`);
  await cdp.evaluate(`document.getElementById('setup-wifi-confirm').click()`);
  await cdp.waitFor(`document.getElementById('setup-badge').textContent === '双向校验'`, '双向校验');
  await cdp.evaluate(`document.getElementById('setup-action').click()`);
  await cdp.waitFor(`document.getElementById('setup-badge').textContent === '等待确认'`, '等待确认');
  await cdp.evaluate(`document.getElementById('setup-action').click()`);
  await cdp.waitFor(`document.getElementById('setup-badge').textContent === '已完成'`, '已完成');
  assert.equal(await cdp.evaluate(`!document.getElementById('setup-success').hidden`), true, '应显示成功提示');

  if (cdp.errors.length > 0) {
    throw new Error(`renderer 异常：${cdp.errors.join(' | ')}`);
  }
  console.log('setup mock e2e OK：待开始→选择设备→取消/恢复→身份验证→Wi-Fi→双向校验→等待确认→已完成');
  cdp.close();
}

main().catch((err) => {
  console.error('setup mock e2e FAILED：', err && err.message);
  process.exit(1);
});
