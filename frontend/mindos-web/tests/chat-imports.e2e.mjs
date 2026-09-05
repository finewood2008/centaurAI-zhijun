// Live loopback smoke test: creates clearly labelled synthetic demo data, uses
// local inference only, leaves the demo available for manual inspection.
// Run: CHAT_IMPORTS_E2E_LIVE=1 node tests/chat-imports.e2e.mjs
import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

if (process.env.CHAT_IMPORTS_E2E_LIVE !== '1') throw new Error('Set CHAT_IMPORTS_E2E_LIVE=1 to create a local synthetic demo')
const origin = process.env.MINDOS_BASE_URL || 'http://127.0.0.1:5173'
assert.equal(new URL(origin).hostname, '127.0.0.1', 'Live test must stay on loopback')
const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 960 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', error => errors.push(error.message))
// Never load remote websites through this browser during the test.
await page.route('**/*', route => {
  const url = new URL(route.request().url())
  return ['127.0.0.1', 'localhost'].includes(url.hostname) ? route.continue() : route.abort()
})
const headers = { 'X-Requested-By': 'centaur-vdb' }
const apiPath = path => `${origin}/api/mindos${path}`
let conversationId
try {
  let filename
  if (process.env.CHAT_IMPORTS_TEST_CONVERSATION) {
    conversationId = process.env.CHAT_IMPORTS_TEST_CONVERSATION
    const listing = await context.request.get(apiPath(`/conversations/${conversationId}/imports`), { headers })
    filename = (await listing.json()).items[0].files[0].name
    await page.goto(`${origin}/mindos/c/${conversationId}`, { waitUntil: 'networkidle' })
  } else {
  const created = await context.request.post(apiPath('/conversations'), { headers, data: { title: '文件导入验收（合成演示数据）', mode: 'chat' } })
  assert.equal(created.status(), 200, await created.text())
  conversationId = (await created.json()).id
  await page.goto(`${origin}/mindos/c/${conversationId}`, { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: '添加文件', exact: true }).waitFor()
  await page.getByRole('button', { name: '添加文件', exact: true }).click()
  assert.equal(await page.getByRole('button', { name: '选择已有资料', exact: true }).count(), 1)
  filename = `知君文件导入验收-${Date.now()}.txt`
  const content = '这是一份用于软件测试的合成演示数据，不是真实个人信息。\n项目：星桥阅读计划。\n项目预算：42 万元。\n项目负责人：林小舟（虚构人物）。\n第一阶段：整理 120 份文档；第二阶段：进行 3 场内部演示。\n风险：样本文档格式不统一，需要人工检查。'
  await page.getByLabel('上传聊天文件', { exact: true }).setInputFiles({ name: filename, mimeType: 'text/plain', buffer: Buffer.from(content) })
  await page.getByLabel('待发送文件').waitFor()
  assert.equal(await page.getByRole('button', { name: '发送', exact: true }).isEnabled(), true, 'A file-only message can be sent')
  await page.getByLabel('输入消息', { exact: true }).fill('请简短回答：这份文件的预算和负责人分别是什么？')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  await page.locator('.import-batch').waitFor()
  await page.getByRole('button', { name: '确认文件处理方式', exact: true }).waitFor({ timeout: 60000 })
  await page.getByRole('button', { name: '确认文件处理方式', exact: true }).click()
  await page.getByRole('button', { name: '仅用本地模型读取', exact: true }).click()
  console.log(JSON.stringify({ phase: 'uploaded_and_local_consent', conversationId, filename }))
  }

  // The wait stays inside the test process; the calling agent can report progress.
  await page.waitForFunction(() => [...document.querySelectorAll('.zj-msg--assistant')].some(e => e.textContent.includes('42') && e.textContent.includes('林小舟')), undefined, { timeout: 180000 })
  const detailResponse = await context.request.get(apiPath(`/conversations/${conversationId}`), { headers })
  const detail = await detailResponse.json()
  const reply = detail.messages.find(m => m.role === 'assistant')
  assert.equal(reply.external, false)
  assert.equal(reply.provider, 'ollama')
  assert.ok(reply.provenance.materials.length)
  await page.reload({ waitUntil: 'networkidle' })
  await page.locator('.import-file__name').first().click()
  await page.locator('dialog[open] pre').waitFor()
  assert.match(await page.locator('dialog[open] pre').textContent(), /42 万元/)
  await page.getByRole('button', { name: '关闭文件预览' }).click()
  await page.getByRole('button', { name: '添加文件', exact: true }).click()
  await page.getByRole('button', { name: '选择已有资料', exact: true }).click()
  await page.getByRole('dialog', { name: '选择已有资料' }).waitFor()
  await page.getByLabel('搜索已有资料').fill(filename)
  assert.match(await page.locator('.library-list').textContent(), new RegExp(filename.replaceAll('.', '\\.')))
  await page.getByRole('button', { name: '关闭', exact: true }).click()
  // Paste and drag are staged only; they must not immediately upload.
  await page.evaluate(() => {
    const data = new DataTransfer()
    data.items.add(new File(['synthetic image'], '粘贴截图.png', { type: 'image/png' }))
    document.querySelector('textarea').dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true }))
  })
  assert.match(await page.getByLabel('待发送文件').textContent(), /粘贴截图/)
  await page.getByRole('button', { name: '移除 粘贴截图.png' }).click()
  await page.evaluate(() => {
    const data = new DataTransfer()
    data.items.add(new File(['drop test'], '拖拽测试.txt', { type: 'text/plain' }))
    document.querySelector('.zj-page').dispatchEvent(new DragEvent('drop', { dataTransfer: data, bubbles: true }))
  })
  assert.match(await page.getByLabel('待发送文件').textContent(), /拖拽测试/)
  await page.getByRole('button', { name: '移除 拖拽测试.txt' }).click()
  mkdirSync('../../data/diagnostics/chat-imports', { recursive: true })
  await page.screenshot({ path: '../../data/diagnostics/chat-imports/desktop.png' })
  await page.setViewportSize({ width: 390, height: 844 })
  await page.keyboard.press('Escape')
  await page.waitForFunction(() => document.querySelector('.ws-sidebar').getBoundingClientRect().right <= 1)
  await page.screenshot({ path: '../../data/diagnostics/chat-imports/mobile.png', animations: 'disabled' })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true, 'No horizontal overflow')
  assert.equal(await page.getByRole('button', { name: '发送', exact: true }).evaluate(el => el.getBoundingClientRect().bottom <= window.innerHeight), true, 'Send button stays in the mobile viewport')
  assert.deepEqual(errors, [])
  console.log(JSON.stringify({ phase: 'passed', conversationId, provider: reply.provider, reply: reply.content }))
} finally {
  await browser.close()
}
