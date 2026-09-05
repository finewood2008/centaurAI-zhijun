import type { Claim, ConversationMemoryAttention, Message } from '../services/api'

export interface MemoryPlacement {
  kind: 'claim' | 'alignment'
  claim: Claim
  messageId: string
}

/** Place a scoped prompt only at its own evidence turn; missing lineage never falls back to the latest reply. */
export function placeMemoryAttention(attention: ConversationMemoryAttention | null, messages: Message[], conversationId: string | null): MemoryPlacement | null {
  if (!attention || !conversationId) return null
  const kind = attention.candidate ? 'claim' : 'alignment'
  const claim = attention.candidate || attention.alignment
  if (!claim) return null
  const belongs = (message: Message) => message.conversationId === conversationId
  const completeAssistant = (message: Message) => belongs(message) && message.role === 'assistant' && message.status === 'complete'
  if (kind === 'alignment') {
    const id = claim.selfAlignment?.proposal?.messageId
    const target = messages.find(message => message.id === id && completeAssistant(message))
    if (target) return { kind, claim, messageId: target.id }
  }
  const evidence = new Set((claim.evidence ?? []).filter(item => item.conversationId === conversationId).map(item => item.messageId))
  for (let index = messages.length - 1; index >= 0; index--) {
    const user = messages[index]!
    if (!belongs(user) || user.role !== 'user' || !evidence.has(user.id)) continue
    for (const reply of messages.slice(index + 1)) {
      if (reply.role === 'user') break
      if (completeAssistant(reply)) return { kind, claim, messageId: reply.id }
    }
  }
  return null
}
