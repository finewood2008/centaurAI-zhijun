import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { shouldRefreshDetailAfterRedactionTransition } from '../src/composables/redactionTransition.ts'

assert.equal(shouldRefreshDetailAfterRedactionTransition(undefined, 'ready'), false,
  '首次状态读取不得重载父详情，否则组件会反复卸载并形成刷新环')
assert.equal(shouldRefreshDetailAfterRedactionTransition('processing', 'ready'), true)
assert.equal(shouldRefreshDetailAfterRedactionTransition('pending', 'pending_summary'), true)
assert.equal(shouldRefreshDetailAfterRedactionTransition('ready', 'ready'), false)

const detailSource = await readFile(new URL('../src/pages/MaterialDetailPage.vue', import.meta.url), 'utf8')
assert.match(detailSource, /@updated="loadDetail\(detail\.materialId, \{ background: true \}\)"/)
assert.match(detailSource, /if \(!background\) \{\s*loading\.value = true/)

console.log('redaction-transition: initial hydration and background refresh OK')
