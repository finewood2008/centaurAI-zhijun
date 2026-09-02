// MindOS 视觉截图 + 响应式回归脚本（FE-UI-022）
// 依赖：npm i -D playwright && npx playwright install chromium
// 运行：npm run screenshots
// 输出：tests/visual/shots/<route>-<viewport>.png
// 断言（任一失败则以退出码 1 结束）：
//   1) 页面导航成功（goto 不抛错）
//   2) 应用根节点与页面标题正常渲染（#app 有内容、document.title 匹配路由，标题用 waitForFunction 等待）
//   3) 无横向滚动（scrollWidth <= innerWidth）
//   4) 无未捕获异常（pageerror）与显式 console.error（排除浏览器自动的“Failed to load resource”网络资源噪声）
import { chromium } from 'playwright'
import { mkdirSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = dirname(fileURLToPath(import.meta.url))
const OUT_DIR = resolve(__dirname, 'shots')
mkdirSync(OUT_DIR, { recursive: true })

// 核心页面路由（与 src/router/index.ts 一致）：五个一级入口 + 两个隐藏路由
const ROUTES = [
  { name: 'today', path: '/', title: '今日' },
  { name: 'conversation', path: '/chat', title: '对话' },
  { name: 'ontology', path: '/me', title: '我的本体' },
  { name: 'judgments', path: '/judgments', title: '判断' },
  { name: 'data', path: '/data', title: '资料与边界' },
  { name: 'materials', path: '/materials', title: '原材料' },
  { name: 'settings', path: '/settings', title: '偏好' },
]

// 任务卡要求 390 / 768 / 1024 / 1440 四档视口
const VIEWPORTS = [
  { label: 'desktop', width: 1440, height: 900 },
  { label: 'tablet-lg', width: 1024, height: 768 },
  { label: 'tablet', width: 768, height: 1024 },
  { label: 'mobile', width: 390, height: 844 },
]

// BASE 去掉尾斜杠，避免与路由 path 拼接产生双斜杠被 catch-all 重定向回首页
const BASE = (process.env.MINDOS_BASE_URL ?? 'http://localhost:5173/mindos').replace(/\/$/, '')
const NAV_TIMEOUT = 15000
const TITLE_TIMEOUT = 6000

// 浏览器为 HTTP 500 / 连接失败自动打印的“资源加载”错误，属环境（后端未启动）噪声而非应用错误
const NETWORK_NOISE = /^Failed to load resource/i

// 自带 Chromium 不支持的系统（如 macOS 13）可设 PLAYWRIGHT_CHANNEL=chrome 使用已安装的 Google Chrome
const browser = await chromium.launch(process.env.PLAYWRIGHT_CHANNEL ? { channel: process.env.PLAYWRIGHT_CHANNEL } : {})
const failures = []

for (const { label, width, height } of VIEWPORTS) {
  const page = await browser.newPage({ viewport: { width, height } })

  // 收集页面运行时错误：pageerror（未捕获异常）与 console.error（排除网络资源噪声）
  const pageErrors = []
  page.on('pageerror', (err) => pageErrors.push(`pageerror: ${err.message}`))
  page.on('console', (msg) => {
    if (msg.type() === 'error' && !NETWORK_NOISE.test(msg.text())) {
      pageErrors.push(`console.error: ${msg.text()}`)
    }
  })

  for (const route of ROUTES) {
    const tag = `${route.name}@${label}`
    pageErrors.length = 0

    // 1) 导航必须成功：失败计入 failures 并跳过后续渲染断言
    let navError = ''
    try {
      await page.goto(`${BASE}${route.path}`, { waitUntil: 'domcontentloaded', timeout: NAV_TIMEOUT })
    } catch (e) {
      navError = e instanceof Error ? e.message : String(e)
    }
    if (navError) {
      failures.push(`${tag}: 导航失败 ${navError}`)
      console.log(`FAIL ${tag} -> 导航失败 ${navError}`)
      continue
    }

    // 2a) 标题：等待路由设置（afterEach），容忍冷编译/慢加载
    let titleOk = true
    try {
      await page.waitForFunction(
        (expected) => document.title.includes(expected),
        route.title,
        { timeout: TITLE_TIMEOUT },
      )
    } catch {
      titleOk = false
    }
    const render = await page.evaluate(() => ({
      appLen: document.querySelector('#app')?.textContent?.trim().length ?? 0,
      title: document.title,
    }))
    if (render.appLen === 0) {
      failures.push(`${tag}: 应用根节点未渲染（#app 为空）`)
    }
    if (!titleOk) {
      failures.push(`${tag}: 页面标题异常 "${render.title}"（期望包含 ${route.title}）`)
    }

    // 等待内容渲染（ErrorState/EmptyState/表格等）
    await page.waitForTimeout(1200)

    // 3) 横向溢出断言
    const overflow = await page.evaluate(() => {
      const doc = document.documentElement
      return { doc: doc.scrollWidth, w: window.innerWidth }
    })
    const noOverflow = overflow.doc <= overflow.w
    if (!noOverflow) {
      failures.push(`${tag}: 横向溢出 scrollWidth=${overflow.doc} > innerWidth=${overflow.w}`)
    }

    // 4) 运行时错误断言
    if (pageErrors.length) {
      for (const e of pageErrors) failures.push(`${tag}: ${e}`)
    }

    // 5) 认识论徽章必须带文字（PRD 11.1：不能仅靠颜色区分）
    const emptyBadges = await page.evaluate(
      () => Array.from(document.querySelectorAll('.layer-badge')).filter((el) => !(el instanceof HTMLElement) || !el.innerText.trim()).length,
    )
    if (emptyBadges > 0) {
      failures.push(`${tag}: ${emptyBadges} 个 .layer-badge 没有文字`)
    }

    const ok = !navError && render.appLen > 0 && titleOk && noOverflow && pageErrors.length === 0 && emptyBadges === 0
    const file = resolve(OUT_DIR, `${route.name}-${label}.png`)
    await page.screenshot({ path: file, fullPage: false })
    console.log(`${ok ? 'PASS' : 'FAIL'} ${tag} -> ${file}`)
  }
  await page.close()
}
await browser.close()

if (failures.length) {
  console.error('\n视觉/响应式回归失败：')
  for (const f of failures) console.error(' - ' + f)
  process.exit(1)
}
console.log('\n截图完成：', OUT_DIR)
