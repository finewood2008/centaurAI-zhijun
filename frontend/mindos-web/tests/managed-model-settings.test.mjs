import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import test from 'node:test'
import { parse, compileScript, compileTemplate } from '@vue/compiler-sfc'

const api = await readFile(new URL('../src/services/api.ts', import.meta.url), 'utf8')
const settings = await readFile(new URL('../src/pages/SettingsPage.vue', import.meta.url), 'utf8')

test('chat configuration accepts admin-managed and settings shares the API source type', () => {
  const contract = api.match(/export interface ChatProviderConfig \{([\s\S]*?)\n\}/)?.[1]
  assert.ok(contract)
  assert.match(contract, /source: 'defaults' \| 'runtime_settings' \| 'admin-managed'/)
  assert.match(settings, /ref<ChatProviderConfig\['source'\]>/)
})

test('managed cloud label removes key setup burden while preserving privacy and alternatives', () => {
  assert.match(settings, /cSource === 'admin-managed' \? '平台托管'/)
  assert.match(settings, /v-if="cSource === 'admin-managed'"[^>]*data-testid="managed-cloud-model-notice"/)
  assert.match(settings, /无需填写 API Key/)
  assert.match(settings, /仍可选择本地模型或自行配置在线服务/)
  assert.match(settings, /资料出域仍需按“模型与授权”中的选择确认/)
  assert.match(settings, /<RoutingPanel[^>]*:activate-online-channel="activateOnlineChannelFromRouting"/)
  assert.match(settings, /<ExternalProvidersPanel[^>]*@activated="handleProviderActivated"/)
  assert.match(settings, /@click="disableExternalChatImmediately"/)
  assert.doesNotMatch(settings, /v-model="cApiKey"|v-model="cBaseUrl"|v-model="cModel"/)
})

test('managed source addition compiles within the existing settings Vue SFC', () => {
  const { descriptor, errors } = parse(settings, { filename: 'SettingsPage.vue' })
  assert.deepEqual(errors, [])
  const script = compileScript(descriptor, { id: 'managed-cloud-settings', fs: { fileExists: () => false, readFile: () => undefined } })
  assert.ok(script.content)
  const template = compileTemplate({ source: descriptor.template.content, filename: 'SettingsPage.vue', id: 'managed-cloud-settings', compilerOptions: { bindingMetadata: script.bindings } })
  assert.deepEqual(template.errors, [])
})
