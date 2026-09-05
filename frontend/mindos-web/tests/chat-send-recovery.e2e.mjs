// Run against `python -m tests.chat_send_fixture` (8775) after building the UI.
// All normal chat/preview/candidate requests use the real isolated backend.
import assert from 'node:assert/strict'
import { mkdtemp } from 'node:fs/promises'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { chromium } from 'playwright'

const base = 'http://127.0.0.1:8775'
assert.equal((await (await fetch(base + '/api/health')).json()).version, 'chat-send-fixture', 'STOP: only the disposable chat-send fixture is allowed')
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 1050 } })
const page = await context.newPage(), errors = [], forbidden = [], requests = [], failures = []
const dir = await mkdtemp(join(tmpdir(), 'zhijun-chat-send-e2e-'))
const info = async () => (await context.request.get(base + '/__fixture')).json()
const initial = await info(), cases = initial.cases
const faults = new Map()
page.on('pageerror', error => errors.push(error.message))
page.on('response', async response => {
  if (response.status() >= 400 && response.url().endsWith('/routing/preview')) {
    const body = await response.json().catch(() => ({}))
    failures.push({ url: response.url(), code: body.detail?.code || body.code })
  }
})
await context.route('**/*', async route => {
  const request = route.request(), url = new URL(request.url())
  if (url.origin !== base) { forbidden.push(url.origin); return route.abort() }
  if (request.method() === 'POST') {
    const match = url.pathname.match(/^\/api\/mindos\/conversations\/([^/]+)\/(routing\/preview|messages)$/)
    if (match) {
      requests.push({ cid: match[1], kind: match[2], body: request.postDataJSON() })
      const key = match[1] + ':' + match[2], fault = faults.get(key)
      if (fault) {
        faults.delete(key)
        return route.fulfill({ status: fault.status, contentType: 'application/json', body: JSON.stringify({ detail: { code: fault.code, detail: fault.message } }) })
      }
    }
  }
  return route.continue()
})
const composer = page.locator('.zj-composer textarea').first()
const messages = async cid => (await (await context.request.get(base + '/api/mindos/conversations/' + cid)).json()).messages
const subset = (cid, kind) => requests.filter(request => request.cid === cid && (!kind || request.kind === kind))
const enter = async cid => {
  await page.goto(base + '/mindos/c/' + cid, { waitUntil: 'networkidle' })
  await composer.waitFor()
}
async function waitPair(cid, before, text) {
  let stored = [], deadline = Date.now() + 20000
  while (Date.now() < deadline) {
    stored = await messages(cid)
    if (stored.length === before + 2 && stored.at(-1).status === 'complete' && stored.at(-1).content.includes('合成完整回复')) break
    await new Promise(resolve => setTimeout(resolve, 100))
  }
  assert.equal(stored.length, before + 2, 'one send persists exactly one user/assistant pair')
  assert.equal(stored.at(-2).content, text)
  assert.equal(stored.at(-1).role, 'assistant')
  await page.locator('[data-message-id="' + stored.at(-1).id + '"]').waitFor()
  await page.getByRole('button', { name: '停止', exact: true }).waitFor({ state: 'hidden' })
  return stored
}
async function chooseCandidate() {
  const help = page.getByTestId('reply-assistance').last()
  await help.getByRole('button', { name: '帮我开个头', exact: true }).click()
  const candidate = help.locator('.reply-help__options button').first()
  await candidate.waitFor()
  const text = await candidate.innerText()
  await candidate.click()
  return text
}

try {
  // The actual empty-state entry fills only; the first send creates a new conversation.
  await page.goto(base + '/mindos/chat', { waitUntil: 'networkidle' })
  await page.getByRole('button', { name: /准备一次重要沟通/ }).click()
  assert.equal(await composer.inputValue(), '我想准备一次重要沟通：')
  assert.equal(requests.length, 0, 'starter never sends or previews by itself')
  const starter = '我想准备一次重要沟通：希望先和合伙人明确下一步目标。'
  await composer.fill(starter)
  await composer.press('Enter')
  await page.waitForURL(/\/mindos\/c\/conv_/)
  const starterId = page.url().split('/').at(-1)
  await waitPair(starterId, 0, starter)
  assert.ok(subset(starterId, 'routing/preview').length >= 1, 'fresh conversation uses real preview including legacy-summary retrieval')

  await enter(cases.short)
  const shortBefore = (await messages(cases.short)).length
  await composer.fill('先谈目标。')
  await composer.press('Enter')
  await waitPair(cases.short, shortBefore, '先谈目标。')

  await enter(cases.assisted)
  const assistedBefore = (await messages(cases.assisted)).length
  const chosen = await chooseCandidate()
  assert.equal((await messages(cases.assisted)).length, assistedBefore, 'candidate generation and selection do not create messages')
  await composer.fill(chosen + ' 我想先从这里开始。')
  const assistedText = await composer.inputValue()
  await composer.press('Enter')
  const assisted = await waitPair(cases.assisted, assistedBefore, assistedText)
  assert.equal(assisted.at(-2).meta.replyAssistance.kind, 'assisted', 'editing must retain assisted provenance')
  assert.ok(assisted.at(-2).meta.replyAssistance.selections.length)

  await enter(cases.refresh)
  faults.set(cases.refresh + ':messages', { status: 409, code: 'ROUTE_CHANGED', message: '合成预览已过期，请重新核对' })
  const retryText = '合成一次路由过期，重新核对后仍只发送这条。'
  await composer.fill(retryText)
  await composer.press('Enter')
  await waitPair(cases.refresh, 0, retryText)
  assert.equal(subset(cases.refresh, 'routing/preview').length, 2, 'one dispatch conflict rebuilds the preview exactly once')
  const sends = subset(cases.refresh, 'messages')
  assert.equal(sends.length, 2)
  assert.equal(sends[0].body.requestId, sends[1].body.requestId, 'retry retains the idempotency identity')

  await enter(cases.failure)
  faults.set(cases.failure + ':routing/preview', { status: 500, code: 'INTERNAL_ERROR', message: '合成服务器内部错误' })
  const failedText = '这段输入应在服务器错误后完整保留。'
  await composer.fill(failedText)
  await composer.press('Enter')
  await page.getByText('合成服务器内部错误', { exact: false }).waitFor()
  assert.equal(await composer.inputValue(), failedText)
  assert.equal(subset(cases.failure, 'routing/preview').length, 1, '500 is not retried as a stale preview')
  assert.equal(subset(cases.failure, 'messages').length, 0)
  assert.equal((await messages(cases.failure)).length, 0)

  await enter(cases.source)
  const sourceBefore = (await messages(cases.source)).length
  await composer.fill('我已经写下的合成原文。')
  await chooseCandidate()
  const protectedText = await composer.inputValue()
  const changed = await context.request.post(base + '/__fixture/change-source')
  assert.equal(changed.ok(), true)
  await composer.press('Enter')
  await page.getByText('来源版本已变化', { exact: false }).first().waitFor()
  assert.equal(await composer.inputValue(), protectedText)
  assert.equal(subset(cases.source, 'routing/preview').length, 1, 'a real source change never auto-retries or omits source restrictions')
  assert.equal(subset(cases.source, 'messages').length, 0)
  assert.equal((await messages(cases.source)).length, sourceBefore)
  assert.ok(subset(cases.source, 'routing/preview')[0].body.replyAssistance.selections.length)
  assert.ok(failures.some(item => item.url.includes(cases.source) && item.code === 'SOURCE_CHANGED'), 'real changed ancestor—not a mocked error—must block sending')
  await page.setViewportSize({ width: 390, height: 844 })
  const closeNavigation = page.getByRole('button', { name: '关闭导航', exact: true })
  if (!(await closeNavigation.isVisible())) await page.getByRole('button', { name: '打开导航菜单', exact: true }).click()
  await closeNavigation.click()
  await page.waitForFunction(() => document.querySelector('.ws-sidebar').getBoundingClientRect().right <= 1)
  await page.waitForFunction(() => !document.querySelector('.ws-toast'), undefined, { timeout: 10000 })
  const narrow = await page.locator('.zj-composer').evaluate(element => {
    const textarea = element.querySelector('textarea').getBoundingClientRect()
    const notice = element.querySelector('.zj-composer__recovery').getBoundingClientRect()
    const buttons = [...element.querySelectorAll('button')].filter(button => button.getBoundingClientRect().width > 0).map(button => button.getBoundingClientRect())
    const unobscured = target => {
      const box = target.getBoundingClientRect()
      return [.2, .5, .8].every(x => target.contains(document.elementFromPoint(box.x + box.width * x, box.y + box.height / 2)))
    }
    return { overflow: document.documentElement.scrollWidth > innerWidth + 1,
      inputVisible: textarea.x >= 0 && textarea.right <= innerWidth + 1 && textarea.bottom <= innerHeight + 1,
      noticeBeforeInput: notice.bottom <= textarea.top + 1,
      buttonsContained: buttons.every(box => box.x >= 0 && box.right <= innerWidth + 1 && box.bottom <= innerHeight + 1),
      inputUnobscured: unobscured(element.querySelector('textarea')),
      sendUnobscured: unobscured(element.querySelector('.zj-composer__send')),
      noticeUnobscured: unobscured(element.querySelector('.zj-composer__recovery')) }
  })
  assert.deepEqual(narrow, { overflow: false, inputVisible: true, noticeBeforeInput: true, buttonsContained: true, inputUnobscured: true, sendUnobscured: true, noticeUnobscured: true }, '390px source recovery remains readable and its actual hit targets are not covered')
  await page.screenshot({ path: join(dir, 'source-change-mobile.png'), fullPage: true })
  await page.getByRole('button', { name: '撤销填入', exact: true }).click()
  assert.equal(await composer.inputValue(), '我已经写下的合成原文。', 'rollback restores the candidate undo operation without erasing earlier input')

  const final = await info()
  assert.equal(final.localRequests, initial.localRequests, 'never silently switch to the local model')
  assert.deepEqual(errors, [])
  assert.deepEqual(forbidden, [])
  await page.screenshot({ path: join(dir, 'source-change-retained-input.png'), fullPage: true })
  console.log('Chat send E2E passed: empty starter; old summary null; cited short reply; edited assisted reply; bounded ROUTE_CHANGED recovery; 500 and SOURCE_CHANGED preserve text/provenance; undo restores original; no live transport. ' + dir)
} catch (error) {
  await page.screenshot({ path: join(dir, 'failure.png'), fullPage: true })
  console.error('Synthetic chat-send failure screenshot: ' + dir)
  console.error((await page.locator('body').innerText()).slice(-6000))
  throw error
} finally { await browser.close() }
