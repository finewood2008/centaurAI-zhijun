import assert from 'node:assert/strict'
import { mkdirSync } from 'node:fs'
import { chromium } from 'playwright'

const base = 'http://127.0.0.1:8772'
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } })
const page = await context.newPage()
const errors = []
page.on('pageerror', e => errors.push(e.message))
await page.route('**/*', route => new URL(route.request().url()).hostname === '127.0.0.1' ? route.continue() : route.abort())
const get = async path => (await context.request.get(base + path)).json()
const info = await get('/__fixture')
const cid = info.conversationId
const path = `/api/mindos/conversations/${cid}/routing`
const state = await get(path)
await context.request.put(base + path, { data: { mode: 'online', acknowledge: true, freshContext: true, expectedRevision: state.mode.revision, serviceId: state.service.id } })
const dir = '../../data/diagnostics/quiet-routing'
mkdirSync(dir, { recursive: true })
try {
  await page.goto(`${base}/mindos/c/${cid}`, { waitUntil: 'networkidle' })
  const composer = page.locator('.zj-page__composer textarea')
  const settings = page.getByRole('dialog', { name: '模型与授权', exact: true })
  const openSettings = () => page.getByRole('region', { name: '对话处理方式' }).getByRole('button', { name: '在线理解', exact: true }).click()
  await openSettings()
  await settings.getByRole('switch', { name: '记住资料受限时的处理方式' }).click()
  await settings.getByRole('radio', { name: /跳过受限资料/ }).check()
  await settings.getByRole('button', { name: '保存并开启', exact: true }).click()
  await settings.getByText('已记住处理方式，刷新或重启后仍有效。没有增加任何资料授权。').waitFor()
  assert.equal((await get(path)).defaultAuthorization.enabled, false)
  await settings.getByRole('button', { name: '关闭模型与授权' }).click()
  for (const question of ['星桥项目为什么迟迟不想推进？', '接着聊聊星桥项目的工作安排']) {
    await composer.fill(question)
    await composer.press('Enter')
    await page.getByRole('button', { name: '停止', exact: true }).waitFor({ state: 'hidden' })
    await page.getByTestId('routing-handling-notice').last().waitFor()
    assert.equal(await page.locator('.route-consent[open]').count(), 0)
  }
  let sent = await get('/__fixture')
  assert.equal(sent.onlineRequests.length, 2)
  assert.ok(!JSON.stringify(sent.onlineRequests).includes('星桥项目只是工作安排'))
  await page.reload({ waitUntil: 'networkidle' })
  await openSettings()
  assert.equal(await settings.getByRole('switch', { name: '记住资料受限时的处理方式' }).getAttribute('aria-checked'), 'true')
  await settings.getByRole('button', { name: '修改方式', exact: true }).click()
  await settings.getByRole('radio', { name: /本轮改用本地模型/ }).check()
  await settings.getByRole('button', { name: '保存并开启', exact: true }).click()
  await settings.getByText('已开启 · 本轮改用本地模型', { exact: false }).waitFor()
  await settings.getByRole('button', { name: '关闭模型与授权' }).click()
  await composer.fill('星桥项目这件事怎样理解？')
  await composer.press('Enter')
  await page.getByText('已按你保存的默认方式在本地处理，本轮内容未发给在线模型', { exact: true }).waitFor()
  await page.getByRole('button', { name: '停止', exact: true }).waitFor({ state: 'hidden' })
  sent = await get('/__fixture')
  assert.equal(sent.onlineRequests.length, 2)
  assert.equal(sent.localRequests, 1)
  await page.setViewportSize({ width: 390, height: 844 })
  await openSettings()
  await page.screenshot({ path: `${dir}/settings-mobile.png`, fullPage: true })
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1), true)
  await settings.getByRole('switch', { name: '记住资料受限时的处理方式' }).click()
  await settings.getByText('已关闭固定处理方式；需要新的资料授权时再询问。').waitFor()
  await settings.getByRole('button', { name: '关闭模型与授权' }).click()
  await composer.fill('星桥项目为什么不代表我的个人追求？')
  await composer.press('Enter')
  const consent = page.getByRole('dialog', { name: '这次要让在线模型使用哪些内容？', exact: true })
  await consent.waitFor()
  await page.screenshot({ path: `${dir}/necessary-consent-mobile.png`, fullPage: true })
  await consent.getByRole('button', { name: '取消', exact: true }).click()
  assert.equal(await composer.inputValue(), '星桥项目为什么不代表我的个人追求？')
  assert.equal((await get('/__fixture')).onlineRequests.length, 2)
  assert.deepEqual(errors, [])
  console.log('quiet routing E2E: remembered omit, repeated chat without modal, payload privacy, refresh, explicit local default, off restores necessary consent, cancel preserves input, mobile passed')
} catch (error) {
  await page.screenshot({ path: `${dir}/failure.png`, fullPage: true })
  console.log((await page.locator('body').innerText()).slice(-6000))
  throw error
} finally { await browser.close() }
