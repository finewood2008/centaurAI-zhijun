// Actual TodayPage template + styles, synthetic data and inert child components.
// No app server, live records, browser profile, or model requests are used.
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { parse, compileScript } from '@vue/compiler-sfc'
import { build } from 'esbuild'
import { chromium } from 'playwright'

const root = fileURLToPath(new URL('../', import.meta.url))
const source = await readFile(new URL('../src/pages/TodayPage.vue', import.meta.url), 'utf8')
const { descriptor } = parse(source)
const component = compileScript(descriptor, { id: 'today-layout', inlineTemplate: true }).content
const synthetic = {
  state: 'active',
  brief: {
    status: 'ready', headline: '在繁忙的工作中，也为自己的方向留一点空间',
    message: '这是合成的来信内容。你正在准备一次重要的沟通，希望既能清楚地表达边界，也给双方保留进一步协商的空间。',
    sourceRefs: [
      { id: 'claim:one', sourceType: 'claim', label: '你确认过的理解', title: '我希望重大决定能够兼顾团队长期发展和自己当前最在意的责任' },
      { id: 'claim:two', sourceType: 'claim', label: '参考', title: 'SyntheticUnbrokenSourceTitle'.repeat(5) },
      { id: 'decision:one', sourceType: 'decision', label: '之前的判断', title: '准备下一次与合伙人的职责及授权沟通' },
    ],
  },
  nextAction: { kind: 'chat', title: '接着聊聊如何把复杂约束变成一次清楚、具体又可以调整的沟通' },
  map: { nodes: [], relationshipDays: 12 }, timeline: [],
}
const modules = {
  'today-component': component,
  '@/services/api': `export const getZhijunHome=async()=>(${JSON.stringify(synthetic)}); export const createConversation=async()=>({id:'synthetic'}); export const updateOnboarding=async()=>{};`,
  '@/composables/useToast': 'export const useToast=()=>()=>{};',
  '@/shared/labels': "export const greetingLine=()=> '合成日期';",
  'vue-router': 'export const useRouter=()=>({push:async()=>{}});',
}
const bundle = await build({
  stdin: { contents: "import {createApp} from 'vue'; import Today from 'today-component'; createApp(Today).mount('#app');", resolveDir: root },
  bundle: true, write: false, format: 'iife', platform: 'browser',
  define: { __VUE_OPTIONS_API__: 'true', __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false' },
  plugins: [{ name: 'synthetic-home', setup(plugin) {
    plugin.onResolve({ filter: /^(?:today-component|vue-router|@\/)/ }, args => ({ path: args.path, namespace: 'synthetic' }))
    plugin.onLoad({ filter: /.*/, namespace: 'synthetic' }, args => ({
      contents: modules[args.path] ?? "import {h} from 'vue'; export default {render(){return h('section', {class:'fixture-child'}, '合成地图/辅助区域')}};",
      loader: 'ts', resolveDir: root,
    }))
  } }],
})
const baseCss = await readFile(new URL('../src/styles/base.css', import.meta.url), 'utf8')
const css = descriptor.styles.map(style => style.content).join('\n')
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const page = await browser.newPage()
const errors = [], outgoing = []
page.on('pageerror', error => errors.push(error.message))
await page.route('**/*', route => { outgoing.push(route.request().url()); return route.abort() })
try {
  for (const width of [1440, 1060, 820, 390, 320]) {
    await page.setViewportSize({ width, height: 1000 })
    await page.setContent(`<style>${baseCss}\n${css}\nbody{margin:0}.layout{margin-left:76px;padding:24px}.fixture-child{min-width:0}@media(max-width:767px){.layout{margin-left:0;padding:16px}}</style><div class="layout"><div id="app"></div></div>`)
    await page.addScriptTag({ content: bundle.outputFiles[0].text })
    await page.locator('.zj-letter__sources button').first().waitFor()
    const dimensions = await page.evaluate(() => {
      const letter = document.querySelector('.zj-letter'), box = letter.getBoundingClientRect(), style = getComputedStyle(letter)
      const right = box.right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth)
      const left = box.left + parseFloat(style.paddingLeft) + parseFloat(style.borderLeftWidth)
      const elements = [...letter.children, ...letter.querySelectorAll('.zj-letter__sources button, .zj-letter__action span, .zj-letter__action svg')]
      return {
        right, left, columns: style.gridTemplateColumns,
        overflow: elements.filter(el => { const r = el.getBoundingClientRect(); return r.right > right + 1 || r.left < left - 1 || el.scrollWidth > el.clientWidth + 1 })
          .map(el => ({ class: el.className, tag: el.tagName, width: el.getBoundingClientRect().width, scroll: el.scrollWidth, client: el.clientWidth })),
        pageOverflow: document.documentElement.scrollWidth > innerWidth,
        wrapping: getComputedStyle(letter.querySelector('.zj-letter__sources button')).whiteSpace,
      }
    })
    assert.deepEqual(dimensions.overflow, [], `${width}px: children must stay in letter content box: ${JSON.stringify(dimensions)}`)
    assert.equal(dimensions.pageOverflow, false, `${width}px: no page overflow`)
    assert.equal(dimensions.wrapping, 'normal', 'source titles can wrap rather than truncate')
    console.log(`PASS TodayPage ${width}px: letter column ${dimensions.columns}, source chips and CTA contained`)
  }
  assert.deepEqual(errors, [])
  assert.deepEqual(outgoing, [], 'this regression must never access a live endpoint')
} finally { await browser.close() }
