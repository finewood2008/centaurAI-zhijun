import assert from 'node:assert/strict'
import { readFile } from 'node:fs/promises'
import { placeMemoryAttention } from '../src/shared/memoryAttention.ts'

const message = (id, role, conversationId = 'own', status = 'complete') => ({ id, role, conversationId, status })
const messages = [message('user-1', 'user'), message('reply-1', 'assistant'), message('user-2', 'user'), message('reply-2', 'assistant')]
const candidate = (id, conversationId, messageId) => ({ id, evidence: [{ conversationId, messageId }] })
const attention = (claim, alignment = null) => ({ topicId: 'topic', candidate: claim, alignment })

assert.equal(placeMemoryAttention(attention(candidate('first', 'own', 'user-1')), messages, 'own')?.messageId, 'reply-1')
assert.equal(placeMemoryAttention(attention(candidate('second', 'own', 'user-2')), messages, 'own')?.messageId, 'reply-2')
assert.equal(placeMemoryAttention(attention(candidate('other', 'other', 'user-1')), messages, 'own'), null, 'other conversations never attach to the current reply')
assert.equal(placeMemoryAttention(attention(candidate('missing', 'own', 'missing')), messages, 'own'), null, 'missing evidence never falls back')
assert.equal(placeMemoryAttention(attention(candidate('assistant', 'own', 'reply-1')), messages, 'own'), null, 'ordinary extraction must reference the user turn')
assert.equal(placeMemoryAttention(attention(candidate('incomplete', 'own', 'user-2')), [...messages.slice(0, -1), message('reply-2', 'assistant', 'own', 'error')], 'own'), null)
assert.equal(placeMemoryAttention(attention(candidate('no-answer', 'own', 'user-1')), [message('user-1', 'user'), message('user-2', 'user'), message('reply-2', 'assistant')], 'own'), null, 'do not move across unanswered turns')
const calibration = { ...candidate('calibration', 'own', 'user-2'), selfAlignment: { proposal: { messageId: 'reply-2' } } }
assert.deepEqual(placeMemoryAttention(attention(null, calibration), messages, 'own'), { kind: 'alignment', claim: calibration, messageId: 'reply-2' })
assert.equal(placeMemoryAttention(attention(candidate('priority', 'own', 'user-1'), calibration), messages, 'own')?.kind, 'claim', 'even malformed dual responses use only one shared slot')
assert.equal(placeMemoryAttention(null, messages, 'own'), null)
assert.equal(placeMemoryAttention(attention(calibration), messages, null), null)

const conversation = await readFile(new URL('../src/pages/ConversationPage.vue', import.meta.url), 'utf8')
const settings = await readFile(new URL('../src/pages/SettingsPage.vue', import.meta.url), 'utf8')
const api = await readFile(new URL('../src/services/api.ts', import.meta.url), 'utf8')
assert.doesNotMatch(conversation, /getInbox|seenClaimIds|pollInbox|attachLateCandidates|m\.candidates/)
assert.match(conversation, /memoryPlacement\?\.kind === 'claim'/)
assert.match(conversation, /memoryPlacement\?\.kind === 'alignment'/)
assert.match(conversation, /dismissMemory\('claim', memoryPlacement\.claim\.id, true\)/)
assert.match(conversation, /clearTimeout\(memoryTimer\)/)
assert.match(conversation, /memoryLoadGate\.isCurrent\(ticket\)/)
const background = conversation.slice(conversation.indexOf('function clearMemoryAttention()'), conversation.indexOf('async function onReview('))
assert.doesNotMatch(background, /scrollToBottom|\.focus\(/, 'passive memory changes must not move reading position or focus')
assert.match(conversation, /openWorkspace\('memory'\)/)
assert.match(conversation, /只保存为这件事的记录/)
assert.match(conversation, /memoryDraft\.savedContent/)
assert.match(settings, /仅重要内容提醒/)
assert.match(settings, /仅在我要求时整理/)
assert.match(settings, /expectedRevision: memoryPolicy\.value\.revision/)
assert.match(settings, /不授予在线模型任何新的资料权限/)
assert.match(api, /\/memory\/attention/)
assert.match(api, /\/memory\/draft-review/)
console.log('memory attention placement, shared budget, quiet updates and settings contracts passed')
