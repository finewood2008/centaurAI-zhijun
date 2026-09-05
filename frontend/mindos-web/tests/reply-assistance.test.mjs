import assert from 'node:assert/strict'
import { appendReply, undoReply } from '../src/shared/replyAssistance.ts'
const origin = { messageId: 'question-1', selections: [{ batchId: 'batch-1', candidateId: 'option-1' }] }
const result = appendReply('用户已经写好的内容', '候选起点', undefined, origin)
assert.equal(result.text, '用户已经写好的内容\n候选起点')
assert.equal(undoReply(result.text + '我补充的文字', result), '用户已经写好的内容我补充的文字')
assert.equal(undoReply('用户已经写好的内容\n我改过的候选', result), null)
assert.equal(undoReply(result.text, result), '用户已经写好的内容')
assert.deepEqual(result.origin.selections, origin.selections)
assert.throws(() => appendReply('原文', '新方向', origin, { ...origin, messageId: 'question-2' }))
assert.throws(() => appendReply('原文'.repeat(2000), '新方向', undefined, origin))
assert.equal(appendReply(result.text, '候选起点', origin, origin).origin.selections.length, 1)
assert.equal(appendReply('', '请换个说法', undefined, { messageId: 'question-1', selections: [], control: 'rephrase' }).origin.control, 'rephrase')
console.log('reply assistance: append, undo, preserve later edits, lineage, stale context and limits passed')
