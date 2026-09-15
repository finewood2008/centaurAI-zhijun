import assert from 'node:assert/strict'
import fs from 'node:fs'

const source = fs.readFileSync(new URL('../src/pages/ConversationPage.vue', import.meta.url), 'utf8')
assert.match(source, /onActiveChange: active => \{\s*if \(active\) clearStatusTimer\(\)/)
assert.match(source, /memoryPoller\.isActive\(\).*statusPollAttempts >= 8/)
assert.match(source, /pageReads\.run\('status', getZhijunStatus\)/)
assert.match(source, /pageReads\.run\('stats', getOntologyStats\)/)
assert.match(source, /pageReads\.run\(`attention:\$\{conversationId\}`/)
assert.match(source, /watch\(pendingJobs,[\s\S]*?current\.value && !memoryPoller\.isActive\(\)/)
const settled = source.slice(source.indexOf('onSettled: async'), source.indexOf('const memoryLoadGate'))
assert.doesNotMatch(settled, /refreshMemoryAttention\(/, 'settling tick has already refreshed attention')
assert.match(source, /<LearningCard v-if="workspaceVisited &&/)
assert.match(source, /watch\(workspaceOpen, open => \{ if \(open\) workspaceVisited\.value = true \}\)/)
assert.match(source, /workspaceOpen\.value = false; workspaceVisited\.value = false/)
console.log('conversation read budget: one status polling owner, coalesced reads and sticky lazy workspace passed')
