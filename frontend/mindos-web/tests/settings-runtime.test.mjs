// P1 设置页回归：结构化错误、密钥清除和乐观锁冲突必须保持受控接线。
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const settings = await readFile(new URL('../src/pages/SettingsPage.vue', import.meta.url), 'utf8')
const api = await readFile(new URL('../src/services/api.ts', import.meta.url), 'utf8')

assert.match(api, /export class ApiError/)
assert.match(api, /throw new ApiError\(message, res\.status, code, details, preview\)/)
assert.match(api, /new Headers\(init\?\.headers\)/)
assert.match(api, /headers\.set\('X-Requested-By', 'centaur-vdb'\)/)
assert.match(settings, /ExternalProvidersPanel/)
assert.doesNotMatch(settings, /v-model="cApiKey"|v-model="cBaseUrl"|v-model="cModel"/)
assert.match(settings, /conflictPrompt\.value = true/)
assert.match(settings, /onConflictLoadLatest/)
assert.doesNotMatch(api, /ssrfProtectionEnabled|setSsrfProtection/)
assert.doesNotMatch(settings, /SSRF|ssrfProtectionEnabled/)
assert.doesNotMatch(settings, /qaDiagnostics/)
assert.match(settings, /async function disableExternalChatImmediately/)
assert.match(settings, /externalEnabled: false/)
assert.match(settings, /@click="disableExternalChatImmediately"/)
assert.match(settings, /保存超时设置/)
assert.match(settings, /applyChat\(\$event, true\)/)
assert.match(api, /activateExternalProvider/)
assert.match(api, /getExternalProviderModels/)
assert.match(api, /material-runtime\/pull/)
assert.match(api, /nextCursor/)

console.log('settings-runtime: 15 tests OK')
