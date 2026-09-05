import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { parse, compileScript } from '@vue/compiler-sfc'
import ts from 'typescript'
import * as Vue from 'vue'
import * as connection from '../src/shared/backendConnection.ts'

const source = await readFile(new URL('../src/components/ui/ErrorState.vue', import.meta.url), 'utf8')
const script = ts.transpileModule(compileScript(parse(source).descriptor, { id: 'connection-test' }).content,
  { compilerOptions: { module: ts.ModuleKind.CommonJS, target: ts.ScriptTarget.ES2022 } }).outputText

function setup(message, extra = {}) {
  const props = Vue.reactive({ message, showRetry: true, recoverOnReconnect: false, ...extra })
  const exports = {}, events = []
  new Function('require', 'exports', script)(id => {
    if (id === 'vue') return Vue
    if (id.includes('backendConnection')) return connection
    if (id === 'lucide-vue-next') return { AlertTriangle: {} }
    throw new Error(id)
  }, exports)
  const scope = Vue.effectScope()
  const ui = scope.run(() => exports.default.setup(props, { expose() {}, emit: event => events.push(event) }))
  return { props, ui, events, close() { scope.stop() } }
}

connection.connectionNoticeMounted.value = true
connection.markBackendConnected()
const network = setup('Failed to fetch')
assert.equal(connection.backendNoticeActive.value, true)
assert.equal(network.ui.hideNetworkError.value, true)
const business = setup('权限已经变化，请核对')
assert.equal(business.ui.hideNetworkError.value, false)
assert.equal(business.ui.displayMessage.value, '权限已经变化，请核对')
connection.markBackendConnected()
await Vue.nextTick()
assert.equal(network.ui.hideNetworkError.value, false)
assert.match(network.ui.displayMessage.value, /连接已恢复/)
assert.deepEqual(network.events, [], 'a generic retry may submit or reload: never invoke it automatically')
network.close(); business.close()

const readOnly = setup('Load failed', { recoverOnReconnect: true })
await Vue.nextTick()
connection.markBackendConnected()
await Vue.nextTick()
assert.deepEqual(readOnly.events, ['retry'])
connection.markBackendConnected()
await Vue.nextTick()
assert.deepEqual(readOnly.events, ['retry'], 'one recovery does not repeatedly reload the page')
readOnly.close()

connection.connectionNoticeMounted.value = false
const standalone = setup('NetworkError when attempting to fetch resource.')
assert.equal(standalone.ui.hideNetworkError.value, false, 'do not hide errors without a global banner')
assert.match(standalone.ui.displayMessage.value, /暂时无法连接/)
standalone.close()
for (const message of ['用户取消了请求', '请求失败（403）', 'Invalid JSON', '正文中提到 Failed to fetch']) {
  assert.equal(connection.isNetworkError(message), false)
}
connection.backendConnection.value = 'unknown'
console.log('backend connection: one offline notice, specific errors preserved, opt-in read recovery, no generic replay')
