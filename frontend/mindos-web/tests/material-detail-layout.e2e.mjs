// Real MaterialDetailPage, compiled CSS and synthetic API responses; no box access.
// Run: node tests/material-detail-layout.e2e.mjs (no app server or live box needed).
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
const record = {
 materialId:'synthetic-1', fileName:'原材料详情-产品需求说明书.pdf', fileType:'document', status:'available',
 versionNumber:2, createdAt:'2026-09-14T10:00:00+08:00', metadata:{fileSize:204800,modifiedAt:'2026-09-14T10:00:00+08:00'},
 text:'解析后的原始材料内容。'.repeat(8), textLabel:'解析正文', previewUrl:'/controlled/file', tags:['产品','待评审'],
 folderPath:'产品资料', transcript:[], sensitiveScan:{state:'completed',completedFields:3,totalFields:3},
 contentParts:[{partId:'p1',partType:'text',ordinal:1,text:'第一部分：功能描述。',location:{}},{partId:'p2',partType:'table',ordinal:2,text:'表格',location:{page:1},rows:[['字段','内容'],['备注','超长内容'.repeat(35)]]}],
 embeddedImages:[], draftCard:{title:'需求要点',content:'草稿由用户确认后形成知识卡片。',status:'ok',confirmed:false,revision:'r1'},
};
const mocks = {
 '@/services/api': `export const api={
  getMaterialDetail:async()=>(${JSON.stringify(record)}),
  listMaterialVersions:async()=>({items:[${JSON.stringify(record)}]}),
  getMaterialRelated:async()=>({items:[{id:'r1',title:'相关产品资料',sourceType:'material',snippet:'关联信息'.repeat(20),reasons:['语义相关'],scoreBand:'high'}],note:''}),
  saveMaterialDraftCard:async(_id,payload)=>({...payload,status:'ok',confirmed:false,revision:'r2'}),
 };`,
 '@/shared/productScope':'export const isDesktopProduct=()=>true;',
 '@/services/productFiles':"export const productPreview=async()=>{window.__previews++;return 'blob:controlled'};export const releaseProductPreview=()=>{};export const saveProductResource=async()=>{};",
 '@/components/PdfPreview.vue':"import {h} from 'vue';export default {props:['src'],render:()=>h('div',{class:'fixture-pdf'},'受控 PDF 原件')};",
 '@/components/ui/ProductImage.vue':"import {h} from 'vue';export default {render:()=>h('div','受控图片')};",
 '@/components/RedactionPanel.vue':"import {h} from 'vue';export default {render:()=>h('div','禁止挂载旧隐私面板')};",
 '@/components/lifecycle/LifecycleDangerPanel.vue':"import {h} from 'vue';export default {render:()=>h('button','移至回收站')};",
 '@/composables/useToast':'export const useToast=()=>()=>{};',
 'vue-router':"export const useRoute=()=>({params:{materialId:'synthetic-1'},query:{}});export const useRouter=()=>({push:async()=>{}});export const onBeforeRouteLeave=()=>{};",
}
const styles = []
const bundle = await build({
  stdin: { contents: "import {createApp} from 'vue'; import MaterialDetail from './src/pages/MaterialDetailPage.vue'; window.__previews=0;window.__app=createApp(MaterialDetail);window.__app.mount('#app');", resolveDir: root },
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
const screenshots = await mkdtemp(resolve(tmpdir(), 'zhijun-material-detail-layout-'))
const errors = [], outgoing = []
try {
  for (const width of [1440, 1200, 1199, 1101, 1100, 768, 760, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 820 } })
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => { outgoing.push(route.request().url()); return route.abort() })
    await page.setContent(`<style>${[...baseStyles, ...styles, shellCss].join('\n')}</style><div class="ws-app"><aside class="fixture-sidebar"></aside><main class="ws-app__main"><header class="fixture-header"></header><section class="ws-app__content"><div id="app"></div></section></main></div>`)
    await page.addScriptTag({ content: bundle.outputFiles[0].text })
    await page.locator('.material-overview').waitFor()
    assert.equal(await page.locator('.material-overview > div').count(),8)
    assert.equal(await page.locator('.material-overview').innerText().then(text=>text.includes('敏感识别已完成')),true)
    assert.equal(await page.getByText('禁止挂载旧隐私面板').count(),0)
    assert.equal(await page.evaluate(()=>window.__previews),0,'PDF is not fetched before explicit opening')
    const overflow=await page.evaluate(()=>{
      const content=document.querySelector('.ws-app__content')
      return document.documentElement.scrollWidth>innerWidth || content.scrollWidth>content.clientWidth+1
    })
    assert.equal(overflow,false,`${width}px page remains contained`)
    const draft=page.locator('.draft-card-panel textarea')
    await draft.fill('已修改但尚未保存的知识卡片正文')
    await page.getByRole('button',{name:'刷新处理状态',exact:true}).isDisabled().then(value=>assert.equal(value,true))
    await page.getByRole('button',{name:'保存草稿',exact:true}).click()
    await page.getByRole('button',{name:'刷新处理状态',exact:true}).isEnabled().then(value=>assert.equal(value,true))
    await page.getByRole('button',{name:'查看 PDF 原件',exact:true}).click()
    await page.locator('.fixture-pdf').waitFor()
    assert.equal(await page.evaluate(()=>window.__previews),1)
    await page.getByRole('button',{name:'关闭 PDF 预览',exact:true}).click()
    await page.getByRole('button',{name:'上传新版本',exact:true}).click()
    await page.getByRole('dialog',{name:'上传新版本',exact:true}).waitFor()
    await page.getByRole('button',{name:'取消',exact:true}).click()
    if(width===1200 || width===390) {
      await page.screenshot({path:resolve(screenshots,`material-detail-bottom-${width}.png`),fullPage:true})
      await page.evaluate(()=>document.querySelector('.ws-app__content').scrollTo(0,0))
      await page.screenshot({path:resolve(screenshots,`material-detail-${width}.png`),fullPage:true})
    }
    console.log(`PASS MaterialDetailPage ${width}px: overview, scoped preview, draft protection, version dialog, no horizontal overflow`)
    await page.evaluate(() => window.__app.unmount())
    await page.close()
  }
  assert.deepEqual(errors, [], 'no browser runtime errors')
  assert.deepEqual(outgoing, [], 'regression never calls a live endpoint')
  console.log(`Screenshots: ${screenshots}`)
} finally { await browser.close() }
