// Mount the real Composer, styles and Vue events with synthetic voice services.
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile, mkdtemp } from 'node:fs/promises'
import { dirname, resolve } from 'node:path'
import { tmpdir } from 'node:os'
import { fileURLToPath } from 'node:url'
import { parse, compileScript, compileStyle } from '@vue/compiler-sfc'
import { build } from 'esbuild'
import { chromium } from 'playwright'
import { REPLY_CONTROLS, replaceReply, undoReply, mergeReplyDrafts } from '../src/shared/replyAssistance.ts'

test('control replacement undo preserves the overwritten draft and later typing, including failure merging', () => {
  const prior = '原有手写内容'
  const origin = { messageId: 'reply-2', selections: [], control: 'pause' }
  const result = replaceReply(prior, origin)
  assert.equal(result.text, REPLY_CONTROLS.pause)
  assert.deepEqual(result.origin, origin)
  assert.equal(undoReply(result.text + '补充', result), prior + '补充')
  assert.equal(undoReply('改过的操作', result), null)
  const merged = mergeReplyDrafts({ text: '等待时写下的内容' }, { text: result.text, origin, undo: result })
  assert.equal(undoReply(merged.text, merged.undo), '等待时写下的内容\n' + prior)
})

test('Composer pointer, keyboard, replacement and voice toolbar regressions in desktop and narrow layouts', { timeout: 60000 }, async () => {
  const root = fileURLToPath(new URL('../', import.meta.url)), styles = []
  const mocks = {
    '@/shared/productScope': `export const isDesktopProduct=()=>window.__desktop; export const createProductSessionStorage=()=>({getItem:k=>window.__drafts[k]??null,setItem:(k,v)=>window.__drafts[k]=v,removeItem:k=>delete window.__drafts[k]});`,
    '@/composables/useReplyRecovery': 'export const replyNeedsRecovery=()=>null;',
    '@/composables/useToast': 'export const useToast=()=>message=>window.__toasts.push(message);',
    '@/shared/speech': `export const speechSupported=()=>true; export const mergeTranscript=(...parts)=>parts.filter(Boolean).join(' '); export const splitResults=results=>results; export const createRecognizer=()=>window.__recognizer={start(){window.__starts++},stop(){this.onend?.()}};`,
    '@/services/voiceRecording': `export const createVoiceRecording=callbacks=>window.__voice={start(){window.__starts++;callbacks.onState('recording')},finish(){callbacks.onState('transcribing')},complete(){callbacks.onText('盒子转写内容');callbacks.onState('idle')},cancel(){callbacks.onState('idle')},dispose(){}};`,
  }
  const bundle = await build({
    stdin: { contents: `import {createApp,h,reactive} from 'vue'; import Composer from './src/components/conversation/Composer.vue'; window.__props=reactive({conversationId:'fixture',streaming:false,retrievalOnly:true,allowDeliberate:true});window.__app=createApp({render(){return h(Composer,{...window.__props,ref:c=>window.__composer=c,onSend:(...args)=>window.__sent.push(args),onStop:()=>window.__stops++})}});window.__app.mount('#app');`, resolveDir: root },
    bundle: true, write: false, format: 'iife', platform: 'browser',
    define: { __VUE_OPTIONS_API__: 'true', __VUE_PROD_DEVTOOLS__: 'false', __VUE_PROD_HYDRATION_MISMATCH_DETAILS__: 'false' },
    plugins: [{ name: 'composer-fixture', setup(plugin) {
      plugin.onResolve({ filter: /^@\// }, args => {
        if (Object.hasOwn(mocks, args.path)) return { path: args.path, namespace: 'mock' }
        const absolute = resolve(root, 'src', args.path.slice(2))
        return { path: absolute.endsWith('.vue') ? absolute : `${absolute}.ts` }
      })
      plugin.onLoad({ filter: /.*/, namespace: 'mock' }, args => ({ contents: mocks[args.path], loader: 'ts', resolveDir: root }))
      plugin.onLoad({ filter: /\.vue$/ }, async args => {
        const { descriptor } = parse(await readFile(args.path, 'utf8'), { filename: args.path })
        const id = `data-v-${styles.length}`
        const compiled = compileScript(descriptor, { id, inlineTemplate: true, genDefaultAs: '__component' })
        for (const style of descriptor.styles) {
          const result = compileStyle({ source: style.content, filename: args.path, id, scoped: style.scoped })
          assert.deepEqual(result.errors, [])
          styles.push(result.code)
        }
        return { contents: `${compiled.content}\n__component.__scopeId=${JSON.stringify(id)};export default __component;`, loader: 'ts', resolveDir: dirname(args.path) }
      })
    } }],
  })
  const browser = await chromium.launch({ headless: true, channel: 'chrome' })
  const screenshots = await mkdtemp(resolve(tmpdir(), 'zhijun-composer-'))
  const errors = [], outgoing = []
  try {
    for (const desktop of [false, true]) for (const width of [1280, 390]) {
      const page = await browser.newPage({ viewport: { width, height: 820 } })
      page.on('pageerror', e => errors.push(e.message))
      await page.route('**/*', route => { outgoing.push(route.request().url()); return route.abort() })
      await page.setContent(`<style>*{box-sizing:border-box}body{margin:16px}#app{margin-top:260px}${styles.join('\n')}</style><div id="app"></div><div id="blank" style="height:100px">外部空白</div>`)
      await page.evaluate(desktop => Object.assign(window, { __desktop: desktop, __drafts: {}, __toasts: [], __sent: [], __starts: 0, __stops: 0 }), desktop)
      await page.addScriptTag({ content: bundle.outputFiles[0].text })
      const field = page.getByRole('textbox', { name: '输入消息' })
      const add = page.getByRole('button', { name: '使用资料', exact: true })
      const menu = page.locator('.zj-composer__add-menu')
      await field.fill('保留输入')
      await add.click(); assert.equal(await menu.count(), 1)
      const buttonBox = await add.boundingBox(), menuBox = await menu.boundingBox()
      await page.mouse.move(buttonBox.x + 12, buttonBox.y - 4)
      assert.equal(await menu.count(), 1, 'the gap between trigger and popup remains in the combined hit region')
      await page.mouse.move(menuBox.x + 15, menuBox.y + 15, { steps: 8 })
      assert.equal(await menu.count(), 1)
      await menu.click(); assert.equal(await menu.count(), 1, 'clicking the explanation stays inside the combined region')
      await page.mouse.move(buttonBox.x + 12, buttonBox.y + 8, { steps: 8 })
      assert.equal(await menu.count(), 1)
      await add.click(); assert.equal(await menu.count(), 0)
      for (let i = 0; i < 3; i++) {
        await add.click(); await page.mouse.move(width - 5, 5)
        assert.equal(await menu.count(), 0, 'leaving closes without delayed stale callbacks')
      }
      await add.focus(); await page.keyboard.press('Enter')
      assert.equal(await menu.count(), 1)
      await page.keyboard.press('Escape')
      assert.equal(await menu.count(), 0); assert.equal(await add.evaluate(el => el === document.activeElement), true)
      await add.press('Space'); assert.equal(await menu.count(), 1)
      await page.locator('#blank').dispatchEvent('pointerdown')
      assert.equal(await menu.count(), 0); assert.equal(await add.evaluate(el => el === document.activeElement), true)
      assert.equal(await field.inputValue(), '保留输入')

      const select = control => page.evaluate(control => window.__composer.insertReply('忽略调用者的非标准文字', { messageId: 'reply-' + control, selections: [], control }), control)
      await select('rephrase'); assert.equal(await field.inputValue(), REPLY_CONTROLS.rephrase)
      await select('pause'); assert.equal(await field.inputValue(), REPLY_CONTROLS.pause)
      const draft = () => page.evaluate(() => JSON.parse(window.__drafts['zhijun.reply-input.fixture']))
      const pauseDraft = await draft()
      assert.deepEqual(pauseDraft.origin, { messageId: 'reply-pause', selections: [], control: 'pause' })
      await select('pause'); assert.deepEqual(await draft(), pauseDraft, 'same control does not replace the useful undo with a no-op')
      await page.getByRole('button', { name: '撤销填入' }).click()
      assert.equal(await field.inputValue(), REPLY_CONTROLS.rephrase)
      assert.equal((await draft()).origin.control, 'rephrase')
      await select('pause'); await select('rephrase')
      assert.equal(await field.inputValue(), REPLY_CONTROLS.rephrase)
      await field.fill('手写内容'); await select('pause')
      assert.equal(await field.inputValue(), REPLY_CONTROLS.pause)
      await page.getByRole('button', { name: '撤销填入' }).click()
      assert.equal(await field.inputValue(), '手写内容')
      await page.evaluate(() => window.__composer.replaceText('新情景'))
      assert.equal(await field.inputValue(), '新情景'); assert.equal((await draft()).origin, undefined); assert.equal((await draft()).undo, undefined)
      assert.equal(await field.evaluate(el => el === document.activeElement), true)
      await page.evaluate(() => {
        const origin = { messageId: 'candidate-question', selections: [{ batchId: 'batch', candidateId: 'one' }] }
        window.__composer.insertReply('候选一', origin)
        window.__composer.insertReply('候选二', { ...origin, selections: [{ batchId: 'batch', candidateId: 'two' }] })
      })
      assert.equal(await field.inputValue(), '新情景\n候选一\n候选二')
      assert.equal((await draft()).origin.selections.length, 2)
      assert.equal(await page.evaluate(() => window.__sent.length), 0, 'no insertion sends automatically')

      await field.fill('长文本用于滚动\n'.repeat(200))
      const voice = page.locator('.zj-composer__voice'), send = page.getByRole('button', { name: '发送', exact: true })
      const fieldBox = await field.boundingBox(), voiceBox = await voice.boundingBox(), sendBox = await send.boundingBox()
      assert.ok(voiceBox.y >= fieldBox.y + fieldBox.height)
      assert.ok(voiceBox.x + voiceBox.width <= sendBox.x)
      assert.equal(await field.evaluate(el => getComputedStyle(el).resize), 'vertical')
      assert.equal(await field.evaluate(el => el.scrollHeight > el.clientHeight), true)
      await field.evaluate(el => { el.scrollTop = 100 })
      assert.ok(await field.evaluate(el => el.scrollTop) > 0)
      await page.mouse.move(fieldBox.x + fieldBox.width - 3, fieldBox.y + fieldBox.height - 3)
      await page.mouse.down(); await page.mouse.move(fieldBox.x + fieldBox.width - 3, fieldBox.y + fieldBox.height + 55, { steps: 6 }); await page.mouse.up()
      assert.ok((await field.boundingBox()).height > fieldBox.height, 'native resize handle remains draggable')
      assert.equal(await page.evaluate(() => window.__starts), 0, 'scroll and resize never start voice')
      assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth), true)
      await field.fill('录音前草稿'); await voice.click()
      assert.equal(await voice.getAttribute('aria-pressed'), 'true'); assert.equal(await page.evaluate(() => window.__starts), 1)
      if (desktop) {
        assert.equal(await send.isDisabled(), true)
        await voice.click(); assert.equal(await send.isDisabled(), true)
        await page.evaluate(() => window.__voice.complete())
        assert.equal(await field.inputValue(), '录音前草稿\n盒子转写内容')
      } else {
        await page.evaluate(() => window.__recognizer.onresult({ results: { finalText: '浏览器转写', interimText: '' } }))
        await voice.click(); assert.equal(await field.inputValue(), '录音前草稿 浏览器转写')
      }
      assert.equal(await voice.getAttribute('aria-pressed'), 'false')
      assert.equal(await page.evaluate(() => window.__sent.length), 0)
      await page.evaluate(() => { window.__props.streaming = true })
      assert.equal(await voice.isDisabled(), true)
      await page.getByRole('button', { name: '停止', exact: true }).click()
      assert.equal(await page.evaluate(() => window.__stops), 1)
      await page.evaluate(() => { window.__props.streaming = false; window.__props.disabled = true })
      assert.equal(await voice.isDisabled(), true); assert.equal(await send.isDisabled(), true)
      await page.evaluate(() => { window.__props.disabled = false })
      await page.screenshot({ path: resolve(screenshots, `composer-${desktop ? 'desktop' : 'web'}-${width}.png`), fullPage: true })
      await page.evaluate(() => { window.__props.conversationId = null })
      await page.evaluate(() => {
        window.__composer.replaceText('空白页正在写的草稿')
        window.__composer.insertReply('辅助候选', { messageId: 'landing-question', selections: [{ batchId: 'landing-batch', candidateId: 'option' }] })
      })
      const landingDraft = await page.evaluate(() => JSON.parse(window.__drafts['zhijun.reply-input.__new_conversation__']))
      await page.evaluate(() => {
        window.__composer.adoptLandingDraft('new-matter-chat')
        window.__props.conversationId = 'new-matter-chat'
      })
      assert.equal(await field.inputValue(), landingDraft.text, 'lazy conversation creation keeps visible landing input')
      assert.deepEqual(await page.evaluate(() => JSON.parse(window.__drafts['zhijun.reply-input.new-matter-chat'])), landingDraft, 'adoption retains exact sources and undo')
      await page.getByRole('button', { name: '撤销填入' }).click()
      assert.equal(await field.inputValue(), '空白页正在写的草稿', 'adopted undo still works')
      await page.evaluate(() => { window.__props.conversationId = null })
      assert.equal(await field.inputValue(), landingDraft.text, 'the landing copy is retained independently')
      await page.evaluate(() => {
        window.__drafts['zhijun.reply-input.already-existing'] = JSON.stringify({ text: '目标会话已有草稿' })
        window.__composer.adoptLandingDraft('already-existing')
        window.__props.conversationId = 'already-existing'
      })
      assert.equal(await field.inputValue(), '目标会话已有草稿', 'an existing destination draft is never overwritten')
      await page.evaluate(() => window.__composer.adoptLandingDraft('must-not-copy'))
      assert.equal(await page.evaluate(() => window.__drafts['zhijun.reply-input.must-not-copy']), undefined, 'an established conversation cannot donate a landing draft')
      await page.evaluate(() => window.__app.unmount())
      await page.close()
    }
    assert.deepEqual(errors, []); assert.deepEqual(outgoing, [])
    console.log(`Composer regression screenshots: ${screenshots}`)
  } finally { await browser.close() }
})
