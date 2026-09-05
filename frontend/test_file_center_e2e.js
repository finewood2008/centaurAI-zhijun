/* Electron file-center smoke test. Run the app with --remote-debugging-port=9223 first. */
const assert = require('node:assert/strict');

async function main() {
  const targets = await fetch('http://127.0.0.1:9223/json/list').then((response) => response.json());
  const page = targets.find((target) => target.type === 'page' && target.url.includes('/renderer/index.html'));
  assert(page, 'Electron renderer target was not found');

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
    const result = await command('Runtime.evaluate', {
      expression,
      awaitPromise: true,
      returnByValue: true,
    });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  }

  await command('Runtime.enable');
  await command('Page.enable');
  await command('Page.reload', { ignoreCache: true });
  await new Promise((resolve) => setTimeout(resolve, 500));
  await evaluate(`(async () => {
    document.querySelector('[data-view="files"]').click();
    await new Promise(resolve => setTimeout(resolve, 900));
  })()`);
  const initial = await evaluate(`({
    count: document.getElementById('fc-count').textContent,
    cards: document.querySelectorAll('#fc-grid .fc-card').length,
    statuses: document.querySelectorAll('#fc-grid .fc-status').length,
    page: document.getElementById('fc-page-info').textContent,
    toolbar: Boolean(document.getElementById('fc-trash-btn') && document.getElementById('fc-save-view')),
  })`);
  const total = Number(initial.count);
  assert(total > 0);
  assert.equal(initial.cards, Math.min(total, 60));
  assert.equal(initial.statuses, initial.cards);
  assert.match(initial.page, new RegExp(`共 ${total} 个`));
  assert.equal(initial.toolbar, true);

  await evaluate(`(async () => {
    const input = document.getElementById('fc-search');
    input.value = '半人马AI工作站介绍';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise(resolve => setTimeout(resolve, 700));
  })()`);
  const filtered = await evaluate(`({
    count: document.getElementById('fc-count').textContent,
    cards: document.querySelectorAll('#fc-grid .fc-card').length,
    text: document.getElementById('fc-grid').textContent,
  })`);
  assert.equal(filtered.count, '1');
  assert.equal(filtered.cards, 1);
  assert.match(filtered.text, /半人马AI工作站介绍\.pdf/);
  assert.match(filtered.text, /已索引/);

  await evaluate(`(async () => {
    const input = document.getElementById('fc-search');
    input.value = '';
    input.dispatchEvent(new Event('input', { bubbles: true }));
    await new Promise(resolve => setTimeout(resolve, 700));
    document.getElementById('fc-batch-btn').click();
    document.getElementById('fc-select-all').click();
  })()`);
  const batch = await evaluate(`({
    selected: document.getElementById('fc-selected-count').textContent,
    reindexDisabled: document.getElementById('fc-batch-reindex').disabled,
  })`);
  assert.match(batch.selected, new RegExp(`已选 ${initial.cards} 个`));
  assert.equal(batch.reindexDisabled, false);

  await evaluate(`(async () => {
    document.getElementById('fc-batch-btn').click();
    document.getElementById('fc-trash-btn').click();
    await new Promise(resolve => setTimeout(resolve, 250));
  })()`);
  assert.equal(await evaluate(`!document.getElementById('fc-maint-modal').classList.contains('hidden')`), true);
  await evaluate(`document.getElementById('fc-maint-close').click()`);

  assert.deepEqual(errors, []);
  socket.close();
  console.log(JSON.stringify({ ok: true, initial, filtered, batch }));
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
