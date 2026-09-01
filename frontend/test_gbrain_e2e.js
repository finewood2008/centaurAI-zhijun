/* Electron unified Wiki/GBrain smoke test. Run with --remote-debugging-port=9224. */
const assert = require('node:assert/strict');

async function main() {
  const targets = await fetch('http://127.0.0.1:9224/json/list').then((response) => response.json());
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
    const result = await command('Runtime.evaluate', { expression, awaitPromise: true, returnByValue: true });
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text);
    return result.result.value;
  }

  await command('Runtime.enable');
  await command('Page.enable');
  await command('Page.reload', { ignoreCache: true });
  await evaluate(`new Promise((resolve, reject) => {
    const started = Date.now();
    const timer = setInterval(() => {
      if (!document.getElementById('app').classList.contains('hidden')) { clearInterval(timer); resolve(true); }
      else if (Date.now() - started > 20000) { clearInterval(timer); reject(new Error('app startup timeout')); }
    }, 100);
  })`);
  await evaluate(`(async () => {
    document.querySelector('[data-view="wiki"]').click();
    await new Promise(resolve => setTimeout(resolve, 4200));
  })()`);

  const wiki = await evaluate(`({
    visible: !document.getElementById('wiki-panel').classList.contains('hidden'),
    logoSource: document.querySelector('.brand-mark').getAttribute('src'),
    logoWidth: document.querySelector('.brand-mark').naturalWidth,
    sidebarLogoSource: document.querySelector('.brand-icon').getAttribute('src'),
    sidebarLogoWidth: document.querySelector('.brand-icon').naturalWidth,
    duplicatePage: Boolean(document.getElementById('gbrain-page')),
    subsectionTabs: document.querySelectorAll('[data-wiki-section]').length,
    sourceLabel: document.querySelector('.wiki-section-title').textContent,
    ready: document.getElementById('wiki-engine-runtime').classList.contains('ready'),
    runtime: document.getElementById('wiki-engine-status-text').textContent,
    pages: document.querySelectorAll('#wiki-page-list .wiki-page-item').length,
  })`);
  assert.equal(wiki.visible, true);
  assert.match(wiki.logoSource, /assets\/logo-wordmark\.svg$/);
  assert.equal(wiki.logoWidth, 700);
  assert.match(wiki.sidebarLogoSource, /assets\/logo-wordmark\.svg$/);
  assert.equal(wiki.sidebarLogoWidth, 700);
  assert.equal(wiki.duplicatePage, false);
  assert.equal(wiki.subsectionTabs, 0);
  assert.match(wiki.sourceLabel, /唯一内容源/);
  assert.equal(wiki.ready, true);
  assert.match(wiki.runtime, /就绪/);
  assert(wiki.pages > 0);

  await evaluate(`(async () => {
    const input = document.getElementById('wiki-search-input');
    input.value = '半人马AI 产品定位';
    document.getElementById('wiki-search-btn').click();
    await new Promise(resolve => setTimeout(resolve, 3200));
  })()`);
  const search = await evaluate(`({
    results: document.querySelectorAll('#wiki-page-list .wiki-page-item').length,
    text: document.getElementById('wiki-page-list').textContent,
  })`);
  assert(search.results > 0);
  assert.match(search.text, /相关度/);

  await evaluate(`(async () => {
    document.querySelector('#wiki-page-list .wiki-page-item').click();
    await new Promise(resolve => setTimeout(resolve, 2800));
  })()`);
  const reader = await evaluate(`({
    title: document.getElementById('wiki-title').textContent,
    bodyLength: document.getElementById('wiki-editor-text').value.length,
    graph: Boolean(document.querySelector('#wiki-graph svg')),
    graphNodes: document.querySelectorAll('#wiki-graph .wiki-node').length,
  })`);
  assert(reader.title.length > 2);
  assert(reader.bodyLength > 50);
  assert.equal(reader.graph, true);
  assert(reader.graphNodes > 1);

  await evaluate(`(async () => {
    document.querySelector('[data-view="settings"]').click();
    await new Promise(resolve => setTimeout(resolve, 1800));
    document.querySelector('[data-settings-section="general"]').click();
    await new Promise(resolve => setTimeout(resolve, 100));
  })()`);
  const settings = await evaluate(`({
    visible: !document.getElementById('settings-panel').classList.contains('hidden'),
    activeTab: document.querySelector('[data-view="settings"]').classList.contains('active'),
    insideMain: document.getElementById('settings-panel').parentElement.tagName === 'MAIN',
    position: getComputedStyle(document.getElementById('settings-panel')).position,
    overlay: Boolean(document.getElementById('settings-overlay')),
    cards: document.querySelectorAll('#settings-panel .settings-section').length,
    visibleCards: document.querySelectorAll('#settings-panel .settings-section:not(.hidden)').length,
    navItems: document.querySelectorAll('#settings-panel [data-settings-section]').length,
    activeSection: document.querySelector('#settings-panel [data-settings-section].active').dataset.settingsSection,
    contentTitle: document.getElementById('settings-content-title').textContent,
    ready: document.getElementById('setting-wiki-engine-runtime').classList.contains('ready'),
    status: document.getElementById('setting-wiki-index-status').textContent,
    model: document.getElementById('setting-wiki-index-model').textContent,
    engine: document.getElementById('setting-wiki-index-engine').textContent,
    pages: document.getElementById('setting-wiki-index-pages').textContent,
    vectors: document.getElementById('setting-wiki-index-vectors').textContent,
    message: document.getElementById('setting-wiki-index-msg').textContent,
    reconcile: Boolean(document.getElementById('setting-wiki-index-reconcile')),
  })`);
  assert.equal(settings.visible, true);
  assert.equal(settings.activeTab, true);
  assert.equal(settings.insideMain, true);
  assert.notEqual(settings.position, 'fixed');
  assert.equal(settings.overlay, false);
  assert(settings.cards >= 10);
  assert.equal(settings.visibleCards, 3);
  assert.equal(settings.navItems, 4);
  assert.equal(settings.activeSection, 'general');
  assert.match(settings.contentTitle, /设备与服务/);
  assert.equal(settings.ready, true);
  assert.match(settings.status, /正常/);
  assert.match(settings.model, /bge-m3.*1024/);
  assert.match(settings.engine, /PGLite/);
  assert(Number(settings.pages) > 0);
  const vectorCounts = settings.vectors.split('/').map((part) => Number(part.trim().replace(/,/g, '')));
  assert(vectorCounts[0] > 0);
  assert.equal(vectorCounts[0], vectorCounts[1]);
  assert.match(settings.message, /Wiki 是唯一内容源/);
  assert.equal(settings.reconcile, true);

  await evaluate(`(async () => {
    document.querySelector('[data-settings-section="intelligence"]').click();
    await new Promise(resolve => setTimeout(resolve, 200));
  })()`);
  const intelligenceSettings = await evaluate(`({
    activeSection: document.querySelector('#settings-panel [data-settings-section].active').dataset.settingsSection,
    contentTitle: document.getElementById('settings-content-title').textContent,
    visibleCards: document.querySelectorAll('#settings-panel .settings-section:not(.hidden)').length,
    wikiIndexVisible: !document.getElementById('setting-wiki-index-reconcile').closest('.settings-section').classList.contains('hidden'),
    legacyCloudControls: document.querySelectorAll('#setting-llm-base, #setting-llm-model, #setting-llm-key, #setting-llm-save').length,
    mentionsCloudVendor: document.getElementById('settings-panel').textContent.includes(['Deep', 'Seek'].join('')),
    organizerReady: document.getElementById('setting-wiki-organizer-runtime').classList.contains('ready'),
    organizerStatus: document.getElementById('setting-wiki-organizer-status').textContent,
    organizerModel: document.getElementById('setting-wiki-organizer-model').textContent,
    organizerProvider: document.getElementById('setting-wiki-organizer-provider').textContent,
    organizerMemory: document.getElementById('setting-wiki-organizer-memory').textContent,
    organizerConcurrency: document.getElementById('setting-wiki-organizer-concurrency').textContent,
    organizerMessage: document.getElementById('setting-wiki-organizer-msg').textContent,
  })`);
  assert.equal(intelligenceSettings.activeSection, 'intelligence');
  assert.match(intelligenceSettings.contentTitle, /本地智能能力/);
  assert.equal(intelligenceSettings.visibleCards, 4);
  assert.equal(intelligenceSettings.wikiIndexVisible, true);
  assert.equal(intelligenceSettings.legacyCloudControls, 0);
  assert.equal(intelligenceSettings.mentionsCloudVendor, false);
  assert.equal(intelligenceSettings.organizerReady, true);
  assert.match(intelligenceSettings.organizerStatus, /已就绪/);
  assert.match(intelligenceSettings.organizerModel, /qwen3:1\.7b/);
  assert.match(intelligenceSettings.organizerProvider, /Ollama.*本机/);
  assert.match(intelligenceSettings.organizerMemory, /按需加载.*用后卸载/);
  assert.match(intelligenceSettings.organizerConcurrency, /最多 1 个模型.*1 个任务/);
  assert.match(intelligenceSettings.organizerMessage, /释放模型内存/);
  assert.match(intelligenceSettings.organizerMessage, /不会发送到云端/);
  assert.deepEqual(errors, []);
  socket.close();
  console.log(JSON.stringify({ ok: true, wiki, search, reader, settings, intelligenceSettings }));
}

main().catch((error) => {
  console.error(error.stack || error);
  process.exitCode = 1;
});
