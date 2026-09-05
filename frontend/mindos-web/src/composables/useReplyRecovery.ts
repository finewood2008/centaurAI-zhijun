import { shallowReactive } from 'vue'
import type { ReplyAssistanceInput } from '@/shared/replyAssistance'

// Presentation only. The server remains responsible for validating every source on send.
interface ReplyRecovery { messageId: string; batchIds: string[]; control?: ReplyAssistanceInput['control']; reason: string }
export const replyRecoveries = shallowReactive<Record<string, ReplyRecovery>>({})
const staleCodes = new Set(['SOURCE_CHANGED', 'SOURCE_UNAVAILABLE', 'SOURCE_LIMIT', 'REPLY_SOURCE_CHANGED', 'REPLY_CONTEXT_CHANGED', 'REPLY_BATCH_NOT_FOUND', 'REPLY_CANDIDATE_NOT_FOUND', 'REPLY_FORMAT_CHANGED'])
export function isReplySourceError(error: unknown): boolean {
  return !!error && typeof error === 'object' && staleCodes.has(String((error as { code?: unknown }).code))
}
export function reportReplyFailure(conversationId: string, origin: unknown, error: unknown) {
  if (!isReplySourceError(error) || !origin || typeof origin !== 'object') return
  const value = origin as ReplyAssistanceInput
  if (typeof value.messageId !== 'string' || !Array.isArray(value.selections)) return
  const code = (error as { code?: string }).code
  const reason = code === 'SOURCE_LIMIT' ? '这句辅助文字的来源链超出核对范围，暂时无法安全使用。'
    : code === 'SOURCE_UNAVAILABLE' ? '这句辅助文字的来源暂不可用，无法安全核对。'
    : '这句辅助文字的来源或对话已变化。'
  replyRecoveries[conversationId] = {
    messageId: value.messageId, batchIds: value.selections.map(s => s.batchId), control: value.control,
    reason: reason + '原文仍保留。请先撤销未修改的旧辅助句，再选择新回答；不会自动去掉来源后发送。',
  }
}
export function replyNeedsRecovery(conversationId: string | null | undefined, origin?: ReplyAssistanceInput): ReplyRecovery | undefined {
  const issue = conversationId ? replyRecoveries[conversationId] : undefined
  if (!issue || !origin || issue.messageId !== origin.messageId) return undefined
  if (origin.selections.some(s => issue.batchIds.includes(s.batchId)) || (origin.control && origin.control === issue.control)) return issue
}
