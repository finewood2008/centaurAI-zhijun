// Real Vue component with isolated management service; no account/box writes.
import assert from 'node:assert/strict'
import { readFile, mkdir } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc'
import { build } from 'esbuild'
import { chromium } from 'playwright'
import { expect } from 'playwright/test'

const root = fileURLToPath(new URL('../', import.meta.url)), styles = []
const bundle = await build({
  stdin: { contents: `import {createApp} from 'vue'; import Panel from './src/components/settings/ExternalAgentsPanel.vue'; window.__app=createApp(Panel);window.__app.mount('#app');`, resolveDir: root },
  bundle: true, write: false, format: 'iife', platform: 'browser',
  define: { __VUE_OPTIONS_API__: 'true', __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false' },
  plugins: [{ name: 'external-agent-fixture', setup(plugin) {
    plugin.onResolve({ filter: /^@\// }, args => args.path === '@/services/api' ? { path: args.path, namespace: 'mock' }
      : { path: resolve(root, 'src', args.path.slice(2)) + (args.path.endsWith('.vue') ? '' : '.ts') })
    plugin.onLoad({ filter: /.*/, namespace: 'mock' }, () => ({ contents: 'export const api=window.__api;export class ApiError extends Error{}', loader: 'js' }))
    plugin.onLoad({ filter: /\.vue$/ }, async args => {
      const { descriptor } = parse(await readFile(args.path, 'utf8'), { filename: args.path }), id = 'data-v-external'
      const compiled = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' })
      for (const style of descriptor.styles) styles.push(compileStyle({ source: style.content, filename: args.path, id, scoped: style.scoped }).code)
      return { contents: `${compiled.content}\n__component.__scopeId=${JSON.stringify(id)};export default __component;`, loader: 'ts', resolveDir: dirname(args.path) }
    })
  } }],
})
await mkdir('/private/tmp/zhijun-external-agents', { recursive: true })
const browser = await chromium.launch({ headless: true, channel: 'chrome' })
try {
  for (const width of [1440, 390]) {
    const page = await browser.newPage({ viewport: { width, height: 1000 } })
    const errors = []
    page.on('pageerror', error => errors.push(error.message))
    await page.route('**/*', route => route.abort())
    await page.setContent(`<style>*{box-sizing:border-box}body{margin:0;padding:20px;background:#f7f5ef;color:#29362d;font:15px system-ui}#app{max-width:1000px;margin:auto}${styles.join('\n')}</style><div id="app"></div>`)
    await page.evaluate(() => {
      window.__writes = []
      window.__state = { available: true, enabled: true, endpoint: 'https://box.example.com/mcp', grants: [{
        id: 'gr-test', agentId: 'workbuddy', agentName: 'WorkBuddy', revision: 1, state: 'active',
        sections: ['ways'], materialIds: ['doc-a'], excludedClaimIds: [], acknowledgedLegacyIds: [], days: 30,
        disclosureAccepted: true, createdAt: Date.now()/1000, expiresAt: Date.now()/1000+86400,
      }] }
      window.__api = {
        externalAgents: async () => structuredClone(window.__state),
        externalAgentPreview: async () => ({ personal: [{id:'old',content:'合成偏好：先给结论',section:'ways',nature:'self_declared',requiresLegacyConfirmation:true}],materials:[{id:'doc-a',title:'合成项目资料',version:1}] }),
        externalAgentAudit: async () => ({items:[]}),
        setExternalAgentsEnabled: async enabled => { window.__writes.push('enabled');window.__state.enabled=enabled },
        updateExternalGrant: async (id, body) => { const sent=JSON.parse(JSON.stringify(body));window.__writes.push(sent);Object.assign(window.__state.grants[0],sent,{revision:2}) },
        setExternalGrantState: async (id, revision, state) => { window.__writes.push(state);Object.assign(window.__state.grants[0],{state,revision:revision+1}) },
      }
    })
    await page.addScriptTag({ content: bundle.outputFiles[0].text })
    await expect(page.getByRole('heading', {name:'外部 Agent'})).toBeVisible()
    await expect(page.getByText('WorkBuddy', {exact:true})).toBeVisible()
    await page.getByRole('button',{name:'调整范围'}).click()
    await page.getByRole('button',{name:'取消',exact:true}).click()
    assert.deepEqual(await page.evaluate(() => window.__writes), [])
    await page.getByRole('button',{name:'调整范围'}).click()
    assert.equal(await page.getByRole('checkbox',{name:'我是谁',exact:true}).isChecked(),false)
    await page.getByRole('checkbox',{name:'我是谁',exact:true}).check()
    await page.getByRole('checkbox',{name:/将当前预览/}).check()
    await page.getByRole('checkbox',{name:/我理解内容/}).check()
    await page.screenshot({path:`/private/tmp/zhijun-external-agents/settings-${width}.png`,fullPage:true})
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
    await page.getByRole('button',{name:'保存范围'}).click()
    await expect(page.getByText('授权范围已更新，期限不会自动延长。')).toBeVisible()
    const writes=await page.evaluate(() => window.__writes)
    assert.deepEqual(writes[0].acknowledgedLegacyIds,['old'])
    assert.equal(writes[0].expectedRevision,1)
    await page.getByRole('button',{name:'暂停',exact:true}).click()
    await expect(page.getByText('已暂停',{exact:true})).toBeVisible()
    await page.getByRole('button',{name:'撤销授权',exact:true}).click()
    await expect(page.getByText('已撤销',{exact:true})).toBeVisible()
    assert.equal(await page.getByRole('button',{name:'调整范围'}).count(),0)
    await page.evaluate(() => window.__app.unmount())
    assert.equal(await page.getByText('WorkBuddy',{exact:true}).count(),0)
    assert.deepEqual(errors,[])
    await page.close()
  }
  console.log('External Agent management: desktop/narrow scope editing, cancel, pause, revoke and layout passed.')
} finally { await browser.close() }
