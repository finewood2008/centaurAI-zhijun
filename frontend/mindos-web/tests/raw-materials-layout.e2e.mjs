// Real RawMaterialsPage and UI components, compiled scoped CSS, synthetic records only.
// Run: node tests/raw-materials-layout.e2e.mjs (no app server or live box needed).
import assert from 'node:assert/strict'
import { readFile, mkdtemp } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { createHash } from 'node:crypto'
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc'
import { build } from 'esbuild'
import { chromium } from 'playwright'

const root = fileURLToPath(new URL('../', import.meta.url))
const names = [
  '低空航空平台产品需求说明书.pdf',
  `${'这是非常长的中文文件名用于验证表格不会被撑宽'.repeat(8)}.pdf`,
  `${'SyntheticUnbrokenMaterialFileName'.repeat(12)}.md`,
]
const records = names.map((fileName, index) => ({
  materialId: `synthetic-${index}`, fileName, fileType: 'document',
  status: index === 2 ? 'queued' : 'available', errorCode: index === 2 ? 'service_interrupted' : null,
  createdAt: '2026-09-12T23:52:00+08:00', folderId: index === 1 ? 1 : null,
  knowledgeCard: { state: index === 1 ? 'index_failed' : 'draft' },
  sensitiveScan: index === 1 ? { state: 'completed', completedFields: 12, totalFields: 12, retryable: false } : null,
}))
const mocks = {
  '@/services/api': `const recycled=new Set();export const api={
    listMaterials:async(params)=>{window.__lastMaterialQuery=params;return {items:${JSON.stringify(records)}.filter(row=>!recycled.has(row.materialId)),total:121-recycled.size}},
    listFolderNodes:async()=>({items:[{id:1,parentId:null,name:'超长文件夹名称用于验证列宽和省略号',subtreeMaterialCount:1}]}),
    moveMaterial:async(...args)=>{window.__moves.push(args);},
    getMaterialDeletionImpact:async(id)=>({confirmToken:'synthetic-token',expectedRevision:7,blockingDependencies:[],cleanupSummary:{vectors:2,derivedRecords:3}}),
    recycleMaterial:async(id,payload)=>{window.__recycle={id,payload};recycled.add(id)},
  };`,
  '@/composables/useToast': 'export const useToast=()=>()=>{};',
  'vue-router': "import {h} from 'vue'; export const onBeforeRouteLeave=()=>{}; export const useRoute=()=>({query:{}}); export const useRouter=()=>({push:async target=>{window.__routes.push(target)}}); export const RouterLink={props:['to'],setup:(props,{slots})=>()=>h('a',{href:props.to,onClick:e=>{e.preventDefault();window.__routes.push(props.to)}},slots.default?.())};",
}
const styles = []
const bundle = await build({
  stdin: { contents: "import {createApp} from 'vue'; import RawMaterials from './src/pages/RawMaterialsPage.vue'; window.__routes=[];window.__moves=[];window.__app=createApp(RawMaterials);window.__app.mount('#app');", resolveDir: root },
  bundle: true, write: false, format: 'iife', platform: 'browser',
  define: { __VUE_OPTIONS_API__: 'true', __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false' },
  plugins: [{ name: 'real-sfc-synthetic-api', setup(plugin) {
    plugin.onResolve({ filter: /^(?:vue-router|@\/)/ }, args => {
      if (Object.hasOwn(mocks, args.path)) return { path: args.path, namespace: 'mock' }
      const absolute = resolve(root, 'src', args.path.slice(2))
      return { path: absolute.endsWith('.vue') ? absolute : `${absolute}.ts` }
    })
    plugin.onLoad({ filter: /.*/, namespace: 'mock' }, args => ({ contents: mocks[args.path], loader: 'ts', resolveDir: root }))
    plugin.onLoad({ filter: /\.vue$/ }, async args => {
      const { descriptor } = parse(await readFile(args.path, 'utf8'), { filename: args.path })
      const id = `data-v-${createHash('sha256').update(args.path).digest('hex').slice(0, 8)}`
      const compiled = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' })
      for (const style of descriptor.styles) {
        const result = compileStyle({ source: style.content, filename: args.path, id, scoped: style.scoped })
        assert.deepEqual(result.errors, [], `scoped CSS must compile: ${args.path}`)
        styles.push(result.code)
      }
      return { contents: `${compiled.content}\n__component.__scopeId=${JSON.stringify(id)};export default __component;`, loader: 'ts', resolveDir: dirname(args.path) }
    })
  } }],
})
const baseStyles = await Promise.all(['tokens.css', 'base.css', 'main.css'].map(file => readFile(resolve(root, 'src/styles', file), 'utf8')))
const shellCss = '.fixture-sidebar{width:240px;flex-shrink:0}.fixture-header{height:64px;flex-shrink:0}@media(max-width:1199px){.fixture-sidebar{width:64px}}@media(max-width:767px){.fixture-sidebar{display:none}}'
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
const screenshots = await mkdtemp(resolve(tmpdir(), 'zhijun-materials-layout-'))
const errors = [], outgoing = []
try {
  for (const width of [1440, 1200, 1199, 1101, 1100, 768, 760, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 820 } })
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => { outgoing.push(route.request().url()); return route.abort() })
    await page.setContent(`<style>${[...baseStyles, ...styles, shellCss].join('\n')}</style><div class="ws-app"><aside class="fixture-sidebar"></aside><main class="ws-app__main"><header class="fixture-header"></header><section class="ws-app__content"><div id="app"></div></section></main></div>`)
    await page.addScriptTag({ content: bundle.outputFiles[0].text })
    await page.locator('.ws-table__grid tbody tr').nth(2).waitFor()
    await page.getByRole('navigation', { name: '原材料分页' }).waitFor()
    if (width === 1440) {
      assert.equal(await page.getByRole('button', { name: '上一页', exact: true }).isDisabled(), true)
      await page.getByRole('button', { name: '下一页', exact: true }).click()
      await page.getByText('共 121 项，第 2 / 3 页', { exact: true }).waitFor()
      assert.equal(await page.evaluate(() => window.__lastMaterialQuery.offset), 50)
      await page.getByRole('button', { name: '上一页', exact: true }).click()
      await page.getByText('共 121 项，第 1 / 3 页', { exact: true }).waitFor()
    }
    const dimensions = await page.evaluate(() => {
      const scroll = document.querySelector('.ws-table__scroll'), box = scroll.getBoundingClientRect()
      const content = document.querySelector('.ws-app__content')
      const controls = [...document.querySelectorAll('.ws-table__actions button')]
      return {
        tableClient: scroll.clientWidth, tableScroll: scroll.scrollWidth,
        overflowElements: [...scroll.querySelectorAll('*')].filter(el => el.getBoundingClientRect().right > box.right + 1).map(el => ({ class: el.className, right: el.getBoundingClientRect().right - box.right, opacity: getComputedStyle(el).opacity })),
        pageOverflow: document.documentElement.scrollWidth > innerWidth || content.scrollWidth > content.clientWidth,
        hiddenControls: controls.filter(button => { const r = button.getBoundingClientRect(); return r.left < box.left - 1 || r.right > box.right + 1 || r.width < 25 || r.height < 25 }).map(button => button.getAttribute('aria-label')),
        titleEllipsis: [...document.querySelectorAll('.ws-table__name')].map(cell => { const button = cell.querySelector('.ws-material-name'); return { title: cell.title, overflow: getComputedStyle(button).textOverflow, truncated: button.scrollWidth > button.clientWidth } }),
      }
    })
    assert.equal(dimensions.pageOverflow, false, `${width}px: page must not scroll horizontally`)
    assert.deepEqual(dimensions.titleEllipsis.map(cell => cell.title), names, 'full file names remain available via title')
    assert.ok(dimensions.titleEllipsis.slice(1).every(cell => cell.overflow === 'ellipsis' && cell.truncated), 'long Chinese and unbroken Latin names must truncate instead of widening table')
    if (width >= 760) {
      assert.ok(dimensions.tableScroll <= dimensions.tableClient + 1, `${width}px: no initial table scroll: ${JSON.stringify(dimensions)}`)
      assert.deepEqual(dimensions.hiddenControls, [], `${width}px: all row actions visible without horizontal scrolling`)
      await page.getByRole('button', { name: '移动文件夹', exact: true }).first().hover()
      assert.equal(await page.locator('.ws-table__scroll').evaluate(el => el.scrollWidth <= el.clientWidth + 1), true, 'action tooltip must not create a horizontal scrollbar on hover')
      await page.getByRole('button', { name: '查看详情', exact: true }).first().click()
      assert.equal(await page.evaluate(() => window.__routes.length), 1, 'view button remains clickable')
      await page.getByRole('button', { name: '移动文件夹', exact: true }).first().click()
      await page.getByRole('dialog', { name: '移动资料', exact: true }).waitFor()
      assert.equal(await page.evaluate(() => window.__routes.length), 1, 'move button must not trigger row navigation')
      await page.getByRole('button', { name: '取消', exact: true }).click()
    } else {
      assert.ok(dimensions.tableScroll > dimensions.tableClient, 'narrow phones retain local table scrolling instead of clipping actions')
    }
    if (width === 1200 || width === 1440 || width === 760) {
      await page.mouse.move(0, 0)
      await page.screenshot({ path: resolve(screenshots, `materials-${width}.png`), fullPage: true })
    }
    assert.equal(await page.getByRole('button', { name: '移至回收站', exact: true }).count(), 2, 'queued row has no recycle action')
    if (width === 760 || width === 390) {
      const navigations = await page.evaluate(() => window.__routes.length)
      await page.getByRole('button', { name: '移至回收站', exact: true }).first().click()
      const dialog = page.getByRole('dialog', { name: '移至回收站影响确认', exact: true })
      await dialog.waitFor()
      await dialog.getByRole('button', { name: '确认移至回收站', exact: true }).waitFor()
      assert.equal(await dialog.getByRole('button', { name: /永久清除|彻底删除/ }).count(), 0)
      assert.equal(await page.evaluate(() => window.__recycle), undefined, 'opening the preview never deletes')
      assert.equal(await page.evaluate(() => window.__routes.length), navigations, 'recycle action must not navigate to detail')
      assert.equal(await dialog.evaluate(el => el.getBoundingClientRect().right <= innerWidth && el.scrollWidth <= el.clientWidth), true, 'preview stays within narrow viewport')
      await page.keyboard.press('Escape')
      await dialog.waitFor({ state: 'hidden' })
      await page.getByRole('button', { name: '移至回收站', exact: true }).first().click()
      await dialog.getByRole('button', { name: '确认移至回收站', exact: true }).click()
      await dialog.waitFor({ state: 'hidden' })
      assert.deepEqual(await page.evaluate(() => window.__recycle), { id: 'synthetic-0', payload: { confirmToken: 'synthetic-token', expectedRevision: 7, dependencyActions: [] } })
      assert.equal(await page.locator('.ws-table__grid tbody tr').count(), 2)
      await page.getByRole('link', { name: '返回资料与边界', exact: true }).click()
      assert.equal(await page.evaluate(() => window.__routes.at(-1)), '/data')
    }
    console.log(`PASS RawMaterialsPage ${width}px: table ${dimensions.tableScroll}/${dimensions.tableClient}px, page contained, ${width >= 760 ? 'actions visible and clickable' : 'phone table scroll retained'}`)
    await page.evaluate(() => window.__app.unmount())
    await page.close()
  }
  assert.deepEqual(errors, [], 'no browser runtime errors')
  assert.deepEqual(outgoing, [], 'regression never calls a live endpoint')
  console.log(`Screenshots: ${screenshots}`)
} finally { await browser.close() }
