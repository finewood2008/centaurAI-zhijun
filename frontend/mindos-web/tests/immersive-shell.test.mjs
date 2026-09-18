// 沉浸壳（第一阶段）源码钉：壳的切换只在 App.vue、路由表不含 immersive、
// src/immersive 不复制轮次逻辑也不引桌面模块、ConversationPage 只做增量的 Teleport / 注入。
// 运行：node --experimental-strip-types --test tests/immersive-shell.test.mjs
import assert from 'node:assert/strict'
import test from 'node:test'
import { readFile, readdir } from 'node:fs/promises'
import { resolveShellPreference, shellParamFrom, stripShellParam } from '../src/immersive/shellPreference.ts'
import { connectionLabel } from '../src/desktop/connectionPresentation.ts'

const read = rel => readFile(new URL('../' + rel, import.meta.url), 'utf8')
const immersiveDir = new URL('../src/immersive/', import.meta.url)
const immersiveFiles = (await readdir(immersiveDir, { recursive: true })).filter(f => /\.(ts|vue)$/.test(f))
const immersiveSources = Object.fromEntries(await Promise.all(immersiveFiles.map(async f => [f, await readFile(new URL(f, immersiveDir), 'utf8')])))

test('the shell choice lives in App.vue and never in the route table', async () => {
  const app = await read('src/App.vue')
  assert.match(app, /import \{ immersiveShell \} from '@\/immersive\/shellPreference'/)
  assert.match(app, /<ImmersiveShell v-if="useImmersiveShell"/)
  assert.match(app, /<MainLayout v-else>/)
  assert.match(app, /route\.meta\.onboardingFlow !== true/)
  assert.match(app, /workspaceReady\?: boolean; connectionLabel\?: string/)
  for (const file of ['routes.ts', 'guards.ts', 'index.ts']) {
    assert.doesNotMatch(await read('src/router/' + file), /immersive/i, `router/${file} 不该知道壳的存在`)
  }
})

test('src/immersive copies no turn engine and imports no desktop module', () => {
  assert.ok(immersiveFiles.length >= 10, `expected the immersive directory to be populated, got ${immersiveFiles.join(', ')}`)
  for (const [file, source] of Object.entries(immersiveSources)) {
    for (const forbidden of ['streamChat(', 'prepareChatRoute(', 'createMemoryAttentionPoller(']) {
      assert.equal(source.includes(forbidden), false, `${file} 不能自己起轮次逻辑：${forbidden}`)
    }
    assert.doesNotMatch(source, /from '(?:@\/desktop\/|\.\.\/desktop\/|\.\/desktop\/)/, `${file} 不能引入 src/desktop/*`)
    assert.doesNotMatch(source, /fetch\(/, `${file} 不能直接联网`)
  }
})

test('ConversationPage enters embedded mode by injection only and teleports into the shell hosts', async () => {
  const page = await read('src/pages/ConversationPage.vue')
  assert.match(page, /const shell = inject\(immersiveKey, null\)\s*const embedded = !!shell/)
  const disabledTeleports = page.match(/<Teleport [^>]*:disabled="!embedded"[^>]*>/g) ?? []
  assert.ok(disabledTeleports.length >= 3, `expected at least three shell Teleports, found ${disabledTeleports.length}`)
  for (const host of ['advancedHost', 'deskHost', 'composerHost']) {
    assert.ok(disabledTeleports.some(tag => tag.includes(`:to="shell?.${host}"`)), `missing Teleport to shell?.${host}`)
  }
  assert.ok(disabledTeleports.every(tag => tag.includes(' defer')), 'Vue 3.5 defer keeps hosts resolvable in the same render')
  assert.match(page, /:class="\{ 'zj-page--embedded': embedded \}"/)
  assert.match(page, /<aside v-if="!embedded" class="zj-page__side"/)
  assert.match(page, /v-if="showIntro && !embedded"/)
  assert.match(page, /v-if="showBlank && !embedded"/)
  assert.match(page, /<template v-if="!embedded">\s*<div class="zj-turn"/)
  assert.match(page, /<StreamTurn\s+v-else/)
  assert.match(page, /:quiet="embedded"/)
  assert.match(page, /shell\?\.scroller\(\) \?\? listRef\.value/)
  assert.match(page, /shell\?\.isNearBottom\(\)/)
  assert.match(page, /if \(embedded\) setActiveTurn\(activeTurnBridge\)/)
  assert.match(page, /clearActiveTurn\(bridgeOwner\)/)
  // 被其它测试钉住的经典片段仍在
  assert.match(page, /@mode="onRoutingMode" @mode-selected="onRoutingModeSelected"/)
  assert.match(page, /<OutcomesCard[^>]*:conversation-id="current\?\.id"[^>]*@refresh="current && refreshOutcomes\(current\.id, true\)"/)
  assert.match(page, /dismissMemory\('claim', memoryPlacement\.claim\.id, true\)/)
})

test('both entries load the immersive stylesheet after main.css and the shell skeleton has its hosts', async () => {
  for (const entry of ['src/main.ts', 'src/main-desktop.ts']) {
    const source = await read(entry)
    assert.ok(source.indexOf("import './styles/main.css'") < source.indexOf("import './styles/immersive.css'"), `${entry} 应在 main.css 之后引入 immersive.css`)
  }
  const css = await read('src/styles/immersive.css')
  assert.match(css, /grid-template-rows: auto minmax\(0, 1fr\) auto/)
  assert.match(css, /height: 100dvh/)
  assert.match(css, /--zj-tint/)
  assert.match(css, /prefers-reduced-motion/)
  assert.match(css, /@keyframes zj-seal-breathe/)
  const shell = immersiveSources['ImmersiveShell.vue']
  // C 部分：两个宿主搬进了抽屉（案头 / 偏好的「高级」），抽屉常驻挂载并先于流
  assert.doesNotMatch(shell, /id="zj-desk-host"/)
  assert.match(immersiveSources['drawers/Desk.vue'], /:id="hostId" ref="host"/)
  assert.match(immersiveSources['drawers/Desk.vue'], /DESK_HOST\.slice\(1\)/)
  assert.match(immersiveSources['drawers/Preferences.vue'], /:id="advancedHostId"/)
  assert.match(immersiveSources['drawers/Preferences.vue'], /ADVANCED_HOST\.slice\(1\)/)
  assert.match(shell, /id="zj-composer-host"/)
  assert.ok(shell.indexOf('<DeskDrawer') < shell.indexOf('class="zj-stage"'), 'Teleport 宿主（在抽屉里）先于流渲染')
  assert.ok(shell.indexOf('<PreferencesDrawer') < shell.indexOf('class="zj-stage"'), 'Teleport 宿主（在抽屉里）先于流渲染')
  assert.match(shell, /router\.replace\('\/chat'\)/)
  assert.match(shell, /provide\(immersiveKey, \{ embedded: true, scroller, composerHost: COMPOSER_HOST, deskHost: DESK_HOST, advancedHost: ADVANCED_HOST, isNearBottom \}\)/)
  assert.match(shell, /<RoutePageDrawer open :title="drawerTitle" @close="closeDrawer">/)
  assert.match(immersiveSources['SealDock.vue'], /role="toolbar" aria-label="印"/)
  assert.match(immersiveSources['SealDock.vue'], /aria-haspopup="dialog"/)
  for (const glyph of ['我', '昔', '案', '偏']) assert.match(immersiveSources['SealDock.vue'], new RegExp(`glyph: '${glyph}'`))
  assert.match(immersiveSources['PresenceBar.vue'], /class="zj-presence__seal" aria-label="关于知君" aria-haspopup="dialog"/)
  assert.match(immersiveSources['RoutePageDrawer.vue'], /<SideDrawer :open="open" :title="title" wide/)
  assert.match(await read('src/components/ui/SideDrawer.vue'), /\.side-drawer--wide \{ width:min\(960px,100vw\); \}/)
})

test('shell preference: url param wins, is persisted, stripped, then storage, default classic', () => {
  assert.equal(shellParamFrom('?shell=immersive'), 'immersive')
  assert.equal(shellParamFrom('?say=hi&shell=classic'), 'classic')
  assert.equal(shellParamFrom('?shell=other'), null)
  assert.equal(shellParamFrom('', '#/chat?shell=immersive'), 'immersive', '桌面端 hash 路由里的参数也算')
  assert.deepEqual(stripShellParam('?shell=immersive&say=hi', '#/chat'), { search: '?say=hi', hash: '#/chat' })
  assert.deepEqual(stripShellParam('', '#/chat?shell=classic&x=1'), { search: '', hash: '#/chat?x=1' })
  assert.deepEqual(stripShellParam('?shell=immersive', ''), { search: '', hash: '' })
  assert.equal(resolveShellPreference('immersive', 'classic'), true)
  assert.equal(resolveShellPreference('classic', 'immersive'), false)
  assert.equal(resolveShellPreference(null, 'immersive'), true)
  assert.equal(resolveShellPreference(null, null), false)
})

test('desktop passes connection state through App props and shares one label table', async () => {
  const desktopApp = await read('src/desktop/DesktopApp.vue')
  assert.match(desktopApp, /<App v-if="signedIn" :workspace-ready="ready" :connection-label="connectionLabel\(phase, ready\)">/)
  assert.match(desktopApp, /<RouterView v-if="ready \|\| isSettings"/, 'slot logic untouched')
  const topbar = await read('src/desktop/DesktopTopbar.vue')
  assert.match(topbar, /connectionLabelFor\(phase\.value, props\.workspaceReady\)/)
  assert.doesNotMatch(topbar, /Record<Phase, string>/)
  assert.equal(connectionLabel('connecting', false), '正在连接盒子')
  assert.equal(connectionLabel('failed', false), '连接未就绪')
  assert.equal(connectionLabel('ready', false), '正在打开工作区')
  assert.equal(connectionLabel(undefined, false), '正在初始化')
  assert.equal(connectionLabel('signed_out', true), '已连接')
})

test('composer quiet mode, faint provenance strip and the shared health composable', async () => {
  const composer = await read('src/components/conversation/Composer.vue')
  assert.match(composer, /quiet\?: boolean/)
  assert.match(composer, /v-if="allowDeliberate !== false && !quiet"/)
  assert.match(composer, /<button\s+v-if="!quiet"\s+type="button"\s+class="zj-composer__chip"\s+:class="\{ 'is-on': deep \}"/)
  assert.match(composer, /<template v-if="quiet">[\s\S]*整理成判断[\s\S]*展开分析[\s\S]*<\/template>/)
  assert.match(composer, /setDeep: \(on: boolean\) => \{\s*deep\.value = on\s*\}/)
  assert.match(composer, /v-else-if="showHint" class="zj-composer__intent"/, '意图提示行仍是内联建议')
  const strip = await read('src/components/conversation/ProvenanceStrip.vue')
  assert.match(strip, /variant\?: 'default' \| 'faint'/)
  assert.match(strip, /data-testid="provenance-toggle"\s*:hidden="faint \|\| undefined"/)
  assert.match(strip, /<div class="zj-prov" :class="\{ 'is-open': open, 'zj-prov--faint': faint \}">/)
  const topbar = await read('src/layouts/AppTopbar.vue')
  assert.match(topbar, /useBackendHealth\(\)/)
  assert.doesNotMatch(topbar, /api\.health\(/)
  const health = await read('src/composables/useBackendHealth.ts')
  assert.match(health, /connectionNoticeMounted\.value = true/)
  assert.match(health, /backendConnection\.value === 'disconnected' \? 5000 : 30000/)
  assert.match(immersiveSources['PresenceBar.vue'], /useBackendHealth\(\)/)
  assert.match(immersiveSources['StreamTurn.vue'], /<ProvenanceStrip v-if="provOpen && message\.provenance" conversation variant="faint"/)
})
