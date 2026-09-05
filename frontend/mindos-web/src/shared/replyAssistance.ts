export interface ReplyAssistanceInput {
  messageId: string
  selections: Array<{ batchId: string; candidateId: string }>
  control?: 'rephrase' | 'pause' | null
}
export interface ReplyBatch {
  id: string; messageId: string; contextRevision: string
  candidates: Array<{ id: string; text: string }>
  model: string; external: boolean; excluded: string[]
}
export const REPLY_CONTROLS = {
  rephrase: '请换一种更简单、具体的说法，一次只问一个问题。',
  pause: '这个问题先放一放，换一个方向聊聊。',
} as const

export function appendReply(text: string, extra: string, current: ReplyAssistanceInput | undefined, incoming: ReplyAssistanceInput) {
  if (current && current.messageId !== incoming.messageId) throw new Error('输入框还保留着上一条回复的辅助文字，请先发送或清空，再选择新的方向。')
  const selections = [...new Map([...(current?.selections || []), ...incoming.selections].map(s => [s.batchId + s.candidateId, s])).values()]
  if (selections.length > 5) throw new Error('已选择多个方向，请先整理并发送。')
  const inserted = (text && !text.endsWith('\n') ? '\n' : '') + extra
  if ((text + inserted).length > 4000) throw new Error('输入框空间不足，原文未改变。')
  return { text: text + inserted, inserted, offset: text.length,
    origin: { messageId: incoming.messageId, selections, control: incoming.control || current?.control } as ReplyAssistanceInput }
}

export function undoReply(text: string, insertion: { inserted: string; offset: number }) {
  if (text.slice(insertion.offset, insertion.offset + insertion.inserted.length) !== insertion.inserted) return null
  return text.slice(0, insertion.offset) + text.slice(insertion.offset + insertion.inserted.length)
}
