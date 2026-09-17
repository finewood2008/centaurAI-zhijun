import type { Conversation, GrowthDecision } from '../services/api'

export const REVISIT_PROMPT = '我想一起回看这段经历。请先核对我当时说过的话，帮我梳理当时在意什么、后来有什么变化。把原话和你的观察分开；依据不足时请直接说明，不要把一次经历概括成我的性格。我会补充或纠正。'

export type RevisitEntry =
  | { kind: 'decision'; id: string; at: string; decision: GrowthDecision; conversations: Conversation[] }
  | { kind: 'conversation'; id: string; at: string; conversation: Conversation }

function sourceConversations(decision: GrowthDecision): Set<string> {
  const ids = new Set<string>()
  for (const raw of decision.evidenceRefs) {
    try {
      const ref = JSON.parse(raw)
      if (ref && typeof ref.conversationId === 'string') ids.add(ref.conversationId)
    } catch { /* Legacy plain-text evidence has no reliable conversation link. */ }
  }
  return ids
}

export function revisitEntries(decisions: GrowthDecision[], conversations: Conversation[]): RevisitEntry[] {
  const linked = new Set<string>()
  const entries: RevisitEntry[] = decisions.map(decision => {
    const sources = sourceConversations(decision)
    const related = conversations.filter(c => c.decisionId === decision.id || sources.has(c.id))
    related.forEach(c => linked.add(c.id))
    const dates = [decision.createdAt, decision.outcome?.recordedAt, decision.review?.createdAt,
      ...related.map(c => c.lastMessageAt || c.updatedAt)].filter((v): v is string => !!v)
    dates.sort((a, b) => Date.parse(b) - Date.parse(a))
    return { kind: 'decision', id: decision.id, at: dates[0], decision, conversations: related }
  })
  for (const conversation of conversations) {
    if (!linked.has(conversation.id) && conversation.messageCount > 0) {
      entries.push({ kind: 'conversation', id: conversation.id,
        at: conversation.lastMessageAt || conversation.updatedAt, conversation })
    }
  }
  return entries.sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.id.localeCompare(b.id))
}
