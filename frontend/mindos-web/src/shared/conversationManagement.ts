import type { Conversation, ConversationListOptions } from '../services/api'

export function conversationActions(conversation: Conversation, allowRemove = true) {
  return [
    { action: 'rename', label: '重命名' },
    { action: 'pin', label: conversation.pinnedAt ? '取消置顶' : '置顶' },
    { action: conversation.status === 'archived' ? 'restore' : 'archive', label: conversation.status === 'archived' ? '移回最近' : '归档', hint: '保留对话与已有资料' },
    ...(allowRemove ? [{ action: 'delete', label: '删除', danger: true, hint: '需再次确认' }] : []),
  ]
}

export function conversationTitleError(title: string): string {
  const length = [...title.trim()].length
  return length < 1 ? '请输入对话名称。' : length > 80 ? '对话名称最多 80 个字。' : ''
}

export function conversationListQuery(tab: 'active' | 'archived', query: string, scope: 'all' | 'active' | 'archived', offset = 0): ConversationListOptions {
  const q = query.trim()
  return { status: q ? scope : tab, ...(q ? { q } : {}), offset, limit: 30 }
}

/** An old detail response may contain newer message counts but stale user-managed metadata. */
export function retainNewerConversationMetadata(incoming: Conversation, known?: Conversation | null): Conversation {
  if (!known || incoming.id !== known.id || incoming.metadataRevision >= known.metadataRevision) return incoming
  return { ...incoming, title: known.title, status: known.status, pinnedAt: known.pinnedAt, metadataRevision: known.metadataRevision }
}
