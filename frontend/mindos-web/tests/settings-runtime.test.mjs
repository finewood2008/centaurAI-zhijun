// P1 设置页回归：结构化错误、密钥清除和乐观锁冲突必须保持受控接线。
import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'

const settings = await readFile(new URL('../src/pages/SettingsPage.vue', import.meta.url), 'utf8')
const api = await readFile(new URL('../src/services/api.ts', import.meta.url), 'utf8')

assert.match(api, /export class ApiError/)
assert.match(api, /throw new ApiError\(message, res\.status, code, details\)/)
assert.match(api, /new Headers\(init\?\.headers\)/)
assert.match(api, /headers\.set\('X-Requested-By', 'centaur-vdb'\)/)
assert.match(settings, /clearApiKey: cClearApiKey\.value \? true : undefined/)
assert.match(settings, /cProvider === 'openai' && !cExternal && cApiKeyConfigured/)
assert.match(settings, /conflictPrompt\.value = true/)
assert.match(settings, /onConflictLoadLatest/)
assert.doesNotMatch(api, /ssrfProtectionEnabled|setSsrfProtection/)
assert.doesNotMatch(settings, /SSRF|ssrfProtectionEnabled/)
assert.doesNotMatch(settings, /qaDiagnostics/)
assert.match(settings, /async function disableExternalChatImmediately/)
assert.match(settings, /externalEnabled: false/)
assert.match(settings, /@change="disableExternalChatImmediately"/)
assert.match(api, /material-runtime\/pull/)
assert.match(api, /nextCursor/)

console.log('settings-runtime: 11 tests OK')
