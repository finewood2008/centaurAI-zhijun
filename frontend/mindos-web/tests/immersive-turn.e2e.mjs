// 沉浸壳走完一轮：built UI + disposable real API（backend/tests/immersive_fixture.py，8778）；没有真实账号、盒子或模型。
// 步骤：/chat?shell=immersive → 今天的会话（改写路由）与日头 → 发送 → 「继续前，确认资料使用」 → 分段浮现（至少两步）→ 停止消失
//       → 点状态行展开 .zj-prov → 留印（种好的产出）→ 昨天的块懒加载 / 留印 / 接着说 / 回到今天 → 上滑加载更早（视口顶部不动）
//       → ?say= 预填 → 390 宽截图。
// 运行：cd backend && .venv/bin/python -m tests.immersive_fixture（先 npm run build）
//       PLAYWRIGHT_CHANNEL=chrome node --experimental-strip-types tests/immersive-turn.e2e.mjs
import assert from 'node:assert/strict'
import { mkdir } from 'node:fs/promises'
import { chromium } from 'playwright'
import { expect } from 'playwright/test'
import { dayLabel } from '../src/immersive/dayStream.ts'

const base = 'http://127.0.0.1:8778'
assert.equal((await (await fetch(base + '/api/health')).json()).version, 'immersive-fixture')
const info = async () => (await (await fetch(base + '/__fixture')).json())
const initial = await info()
assert.equal(initial.synthetic, true)
const list = await (await fetch(base + '/api/mindos/conversations?status=all&limit=200')).json()
const yesterdayRow = list.items.find(item => item.id === initial.cases.yesterday)
assert.ok(yesterdayRow?.outcomes?.working >= 1, 'fixture precondition: yesterday conversation carries a working claim in its outcomes brief')
assert.ok(list.total > 30, `fixture precondition: more than one page of conversations (got ${list.total})`)

const browser = await chromium.launch({ headless: true, channel: process.env.PLAYWRIGHT_CHANNEL || 'chrome' })
const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } })
const errors = []
page.on('pageerror', error => errors.push(error.message))
await page.route('**/*', route => new URL(route.request().url()).origin === base ? route.continue() : route.abort())
const screenshotDir = '/private/tmp/zhijun-immersive-screenshots'
await mkdir(screenshotDir, { recursive: true })

const input = page.getByRole('textbox', { name: '输入消息', exact: true })
const stream = page.getByTestId('day-stream')
const labels = page.getByTestId('day-label')
const activeArea = page.locator('.zj-daystream__active')
const urlOf = id => new RegExp('/mindos/c/' + id + '(\\?.*)?$')
async function stopped() { await expect(page.getByRole('button', { name: '停止', exact: true })).toHaveCount(0) }

try {
  // 1. /chat → 今天的会话；没有导航，只有印坞；日头：今天 / 昨天 / 上周
  await page.goto(base + '/mindos/chat?shell=immersive')
  await expect(input).toBeVisible()
  await expect(page).toHaveURL(urlOf(initial.cases.today))
  await expect(page.getByRole('toolbar', { name: '印' })).toBeVisible()
  await expect(page.locator('.ws-sidebar')).toHaveCount(0)
  await expect(stream).toHaveAttribute('role', 'log')
  const now = new Date()
  await expect(labels.filter({ hasText: '今天 · ' })).toHaveCount(1)
  await expect(labels.filter({ hasText: '昨天 · ' })).toHaveCount(1)
  await expect(labels.filter({ hasText: dayLabel(new Date(initial.backdated.lastweek), now) })).toHaveCount(1)
  const dayKeys = await page.locator('section.zj-day[data-day]').evaluateAll(nodes => nodes.map(node => node.dataset.day))
  assert.deepEqual(dayKeys, [...dayKeys].sort(), 'days run oldest to newest, today last')
  assert.equal(dayKeys.at(-1), dayLabelKey(now))
  await expect(page.locator(`.zj-block[data-conversation-id="${initial.cases.today}"]`)).toHaveCount(0, 'the active conversation is not also a history block')
  await expect(page.locator(`.zj-block[data-conversation-id="${initial.cases.yesterday}"]`)).toHaveCount(1)
  const labelsBefore = await labels.count()
  console.log('day headers on first page:', labelsBefore)

  // 2. 发送 → 确认资料使用 → 分段浮现 → 停止消失
  const onlineBefore = (await info()).onlineRequests
  await input.fill('合成：今天先把目标说清楚。')
  await page.getByRole('button', { name: '发送', exact: true }).click()
  const setup = page.getByRole('dialog', { name: '继续前，确认资料使用', exact: true })
  await expect(setup).toBeVisible()
  await expect(setup).not.toContainText(/openai|synthetic|本地模型|在线模型/i)
  await setup.getByRole('checkbox').check()
  await setup.getByRole('button', { name: '确认并继续' }).click()
  const lastBubble = activeArea.locator('.zj-stream-turn--assistant').last().locator('.zj-msg__body')
  const seenLengths = new Set()
  const startedAt = Date.now()
  const replyHead = initial.paragraphs[0].slice(0, 5)
  while (Date.now() - startedAt < 8000) {
    const text = (await lastBubble.innerText().catch(() => '')).trim()
    // 只记这一轮的回复；刚点确认时 last() 还可能是上一条知君消息
    if (text.startsWith(replyHead)) seenLengths.add(text.length)
    if (text.includes(initial.paragraphs[2])) break
    await page.waitForTimeout(20)
  }
  assert.ok(seenLengths.size >= 2, `expected at least two reveal steps, saw lengths ${[...seenLengths].join(', ')}`)
  console.log('reveal steps (text lengths):', [...seenLengths].join(' → '))
  for (const paragraph of initial.paragraphs) await expect(lastBubble).toContainText(paragraph)
  await stopped()
  await expect(activeArea.getByRole('button', { name: '复制', exact: true }).last()).toBeVisible()
  await expect(page.getByTestId('routing-local-mode')).toHaveCount(0)
  await expect(page.getByRole('button', { name: /模型与授权|改用本地/ })).toHaveCount(0)
  assert.equal((await info()).onlineRequests, onlineBefore + 1)

  // 3. 状态行：一行极淡的字，点开才见回答依据；没有服务商与模型名
  const statusLine = activeArea.getByTestId('status-line').last()
  await expect(statusLine).toHaveText(/参考了你记下的|没有参考记下的内容|看了你记下的/)
  await expect(statusLine).not.toContainText(/在线|本机|openai|synthetic/i)
  await expect(page.getByRole('button', { name: '回答依据', exact: true })).toHaveCount(0)
  await statusLine.click()
  await expect(activeArea.locator('.zj-prov').last()).toBeVisible()
  await expect(activeArea.locator('.zj-prov').last()).not.toContainText(/openai|synthetic|在线处理|本地处理|提示约/i)

  // 4. 留印：这段对话留下的（种好的待确认理解）
  const activeSeal = activeArea.getByRole('button', { name: '这段对话留下的' })
  await expect(activeSeal).toBeVisible()
  await activeSeal.click()
  // 待确认理解在卡上是一句「等你点头 N 条」（去收件箱），不逐条列正文
  await expect(activeArea.getByTestId('outcomes-card')).toContainText(/等你点头 1 条|合成理解：我在准备与合伙人的沟通/)
  await page.screenshot({ path: screenshotDir + '/01-turn-desktop.png' })

  // 5. 昨天的块：进入视口才读；留印来自列表摘要；接着说 → 回复 chip → 回到今天
  const yesterdayBlock = page.locator(`.zj-block[data-conversation-id="${initial.cases.yesterday}"]`)
  await yesterdayBlock.scrollIntoViewIfNeeded()
  await expect(yesterdayBlock).toHaveClass(/is-loaded/)
  await expect(yesterdayBlock).toContainText('昨天我在想要不要接那个项目')
  await yesterdayBlock.getByRole('button', { name: '这段对话留下的' }).click()
  await expect(yesterdayBlock.getByTestId('outcomes-card')).toContainText(/等你点头 1 条|合成理解：我更在意能不能照顾好现有客户/)
  await page.screenshot({ path: screenshotDir + '/03-yesterday-block-desktop.png' })
  await yesterdayBlock.getByRole('button', { name: '接着说', exact: true }).click()
  await expect(page).toHaveURL(urlOf(initial.cases.yesterday))
  const chip = page.getByTestId('replying-chip')
  await expect(chip).toContainText('回复：对「合成：昨天聊过的事」的对话')
  await expect(page.locator('section.zj-day[data-day]').filter({ has: activeArea }).getByTestId('day-label')).toContainText('昨天 · ')
  await expect(page.locator(`.zj-block[data-conversation-id="${initial.cases.yesterday}"]`)).toHaveCount(0)
  await chip.getByRole('button', { name: '回到今天' }).click()
  await expect(page).toHaveURL(urlOf(initial.cases.today))
  await expect(chip).toHaveCount(0)
  await expect(page.locator('section.zj-day[data-day]').filter({ has: activeArea }).getByTestId('day-label')).toContainText('今天 · ')

  // 6a. 读更早的一页时视口顶部的元素不动（滚动锚定）
  const anchor = labels.filter({ hasText: '昨天 · ' }).first()
  await anchor.evaluate(el => el.scrollIntoView({ block: 'start' }))
  await page.waitForTimeout(150)
  const anchorTopBefore = await anchor.evaluate(el => el.getBoundingClientRect().top)
  const scrollTopBefore = await stream.evaluate(el => el.scrollTop)
  assert.ok(scrollTopBefore > 0, 'the stream is scrolled away from the top before loading older days')
  await page.evaluate(() => document.querySelector('.zj-daystream__more button')?.click())
  await expect.poll(() => labels.count()).toBeGreaterThan(labelsBefore)
  await page.waitForTimeout(150)
  const anchorTopAfter = await anchor.evaluate(el => el.getBoundingClientRect().top)
  assert.ok(Math.abs(anchorTopAfter - anchorTopBefore) <= 2, `viewport top moved by ${anchorTopAfter - anchorTopBefore}px while loading older days`)
  const labelsAfterFirstLoad = await labels.count()
  console.log('day headers after first older page:', labelsAfterFirstLoad)

  // 6b. 上滑到顶再读一页（scrollTop < 400 触发）；顶部没有浏览器锚定，靠 scrollHeight 差补偿把位置留住
  await stream.evaluate(el => { el.scrollTop = 0 })
  await expect.poll(() => labels.count(), { timeout: 10000 }).toBeGreaterThan(labelsAfterFirstLoad)
  const oldestLabel = dayLabel(new Date(initial.backdated.oldest), now)
  await expect(labels.filter({ hasText: oldestLabel })).toHaveCount(1)
  await expect.poll(() => stream.evaluate(el => el.scrollTop)).toBeGreaterThan(0)
  console.log('day headers after scrolling to the top:', await labels.count())

  // 7. ?say= 预填：/chat 改写到今天的会话后话头仍在输入框
  await page.goto(base + '/mindos/chat?say=' + encodeURIComponent('合成：带着话头来'))
  await expect(page).toHaveURL(urlOf(initial.cases.today))
  await expect(input).toHaveValue('合成：带着话头来')

  // 8. 今天的信：盒端给了来信就必须是今天这一节的第一条，状态行只有两种措辞，下面一句内联建议 = nextAction
  const home = await (await fetch(base + '/api/mindos/zhijun/home')).json().catch(() => null)
  const letter = page.getByTestId('letter-bubble')
  if (home?.brief?.headline) {
    await expect(letter).toBeVisible()
    await expect(letter).toContainText(home.brief.headline)
    await expect(letter.getByTestId('letter-status')).toHaveText(/^依据 \d+ 条 · 今日已送达$|^我在重新整理$/)
    await expect(letter).not.toContainText(/openai|synthetic|template/i)
    const todaySection = page.locator('section.zj-day[data-day]').filter({ has: letter })
    await expect(todaySection.getByTestId('day-label')).toContainText('今天 · ')
    await expect(todaySection.locator('.zj-letter-bubble, .zj-block, .zj-daystream__active').first()).toHaveClass(/zj-letter-bubble/)
    if (home.nextAction?.title) await expect(letter.getByTestId('letter-suggestion')).toContainText(home.nextAction.title)
    console.log('letter bubble present:', home.brief.status)
  } else {
    await expect(letter).toHaveCount(0)
    console.log('letter bubble absent (no home brief for this fixture state)')
  }

  // 9. 手机宽度：不横向滚动
  await page.setViewportSize({ width: 390, height: 844 })
  await expect.poll(() => page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true)
  await expect(input).toBeVisible()
  await page.screenshot({ path: screenshotDir + '/02-turn-mobile.png' })

  const final = await info()
  assert.ok(!JSON.stringify(final.payloads).includes('PROTECTED_TODAY_HISTORY'), 'protected local history never leaves the box')
  assert.equal(final.localRequests, 0)
  assert.deepEqual(errors, [])
  console.log('PASS immersive turn: today redirect, day headers, consent, paced reveal, status line, 留 seals, block continue, anchored older loading, say prefill, mobile')
} finally {
  await browser.close()
}

function dayLabelKey(date) {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, '0')}-${String(date.getDate()).padStart(2, '0')}`
}
