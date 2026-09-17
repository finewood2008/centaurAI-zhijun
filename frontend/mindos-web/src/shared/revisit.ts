import type { Conversation, GrowthDecision } from '../services/api'

export const REVISIT_PROMPT = '我想一起回看这段经历。请先核对我当时说过的话，帮我梳理当时在意什么、后来有什么变化。把原话和你的观察分开；依据不足时请直接说明，不要把一次经历概括成我的性格。我会补充或纠正。'

export interface RevisitPreferences {
  selected: Record<string, string>
  deferred: Record<string, string>
}
export type RevisitReason = 'due' | 'outcome' | 'reviewed' | 'selected' | null
interface EntryBase { id: string; key: string; at: string; title: string; reason: RevisitReason; revision: string; deferred: boolean }
export type RevisitEntry =
  | (EntryBase & { kind: 'decision'; decision: GrowthDecision; conversations: Conversation[]; sourceIds: string[] })
  | (EntryBase & { kind: 'conversation'; conversation: Conversation })

export const REVISIT_STORAGE_KEY = 'revisit.v1'
export function readRevisitPreferences(raw: string | null): RevisitPreferences {
  const result: RevisitPreferences = { selected: {}, deferred: {} }
  try {
    const value = JSON.parse(raw || '{}')
    for (const field of ['selected', 'deferred'] as const) {
      if (!value?.[field] || typeof value[field] !== 'object' || Array.isArray(value[field])) continue
      for (const [key, item] of Object.entries(value[field])) {
        if (/^(decision|conversation):.+/.test(key) && typeof item === 'string') result[field][key] = item
      }
    }
  } catch { /* An unreadable arrangement must not hide any source records. */ }
  return result
}

function sourceConversations(decision: GrowthDecision): Set<string> {
  const ids = new Set<string>()
  for (const raw of decision.evidenceRefs) {
    try {
      const ref = JSON.parse(raw)
      if (ref && typeof ref.conversationId === 'string' && ref.conversationId) ids.add(ref.conversationId)
    } catch { /* Legacy plain-text evidence has no reliable conversation link. */ }
  }
  return ids
}

/** All candidates remain selectable; only entries with a reason enter the default view. */
export function revisitEntries(decisions: GrowthDecision[], conversations: Conversation[],
  preferences: RevisitPreferences = { selected: {}, deferred: {} }, now = Date.now()): RevisitEntry[] {
  const linked = new Set<string>()
  const entries: RevisitEntry[] = decisions.map(decision => {
    const sources = sourceConversations(decision)
    const related = conversations.filter(c => c.decisionId === decision.id || sources.has(c.id))
    related.forEach(c => sources.add(c.id))
    sources.forEach(id => linked.add(id))
    const key = `decision:${decision.id}`
    const selected = preferences.selected[key] || [...sources].map(id => preferences.selected[`conversation:${id}`]).find(Boolean)
    const reason: RevisitReason = decision.review ? 'reviewed' : decision.outcome ? 'outcome'
      : decision.status === 'open' && decision.reviewAt && Date.parse(decision.reviewAt) <= now ? 'due' : selected ? 'selected' : null
    // Chat activity alone does not undo a deliberate defer. A changed result/date/review does.
    const revision = JSON.stringify([decision.status, decision.reviewAt, decision.outcome?.recordedAt, decision.review?.createdAt])
    const dates = [decision.createdAt, decision.outcome?.recordedAt, decision.review?.createdAt, selected].filter((v): v is string => !!v)
    dates.sort((a, b) => Date.parse(b) - Date.parse(a))
    return { kind: 'decision', key, id: decision.id, title: decision.title, at: dates[0], reason, revision,
      deferred: preferences.deferred[key] === revision, decision, conversations: related, sourceIds: [...sources] }
  })
  for (const conversation of conversations) {
    // A linked review may be outside the decision page; never present it as an unrelated experience.
    if (!linked.has(conversation.id) && !conversation.decisionId && conversation.messageCount > 0) {
      const key = `conversation:${conversation.id}`
      entries.push({ kind: 'conversation', key, id: conversation.id, title: conversation.title || '一段聊过的经历',
        at: preferences.selected[key] || conversation.lastMessageAt || conversation.updatedAt,
        reason: preferences.selected[key] ? 'selected' : null, revision: 'selected',
        deferred: preferences.deferred[key] === 'selected', conversation })
    }
  }
  return entries.sort((a, b) => Date.parse(b.at) - Date.parse(a.at) || a.key.localeCompare(b.key))
}

export function revisitLabel(entry: RevisitEntry): string {
  return entry.reason === 'reviewed' ? '已留下经验' : entry.reason === 'outcome' ? '你已补充结果'
    : entry.reason === 'due' ? '到了约定时间' : entry.reason === 'selected' ? '你主动选中' : '尚未安排回看'
}

export function revisitReason(entry: RevisitEntry): string {
  return entry.reason === 'reviewed' ? '这段经历已有你的复盘，过一段时间也可以有新的理解。'
    : entry.reason === 'outcome' ? '你已记录后来发生的事，可以与当时的预期放在一起看看。'
    : entry.reason === 'due' ? '到了你约定的回看时间，可以核对当时的预期和后来的变化。'
    : entry.reason === 'selected' ? '你把这段经历选进了回看，想再理解一下当时在意什么。'
    : '这是一条已保存的选择。你可以主动选入回看，或等约定时间到了再来。'
}
