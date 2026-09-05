import type { Message, StreamErrorEvent } from '../services/api'

const pendingCodes = new Set(['ROUTE_CONSENT_REQUIRED', 'CONSENT_REQUIRED', 'ROUTE_CHANGED', 'CHARTER_POLICY_CONFLICT'])
export function contextNeedsReview(message: Pick<Message, 'meta'>): boolean {
  const pending = message.meta?.contextPending
  return !!pending && typeof pending === 'object' && pendingCodes.has(String((pending as { code?: unknown }).code))
}
export function isContextReviewError(error: Pick<StreamErrorEvent, 'code' | 'stage' | 'preview'>): boolean {
  return pendingCodes.has(error.code) && (error.stage === 'supplemented' || !!error.preview)
}
/** A recovery continues the persisted user turn, not a new message or a new operation nonce. */
export function contextRetryBody(user: Message, assistant: Pick<Message, 'meta'>, localOnly: boolean): Record<string, unknown> {
  return {
    content: user.content,
    depth: assistant.meta?.depth === 'deep' ? 'deep' : 'brief',
    mode: assistant.meta?.turnMode === 'deliberate' ? 'deliberate' : 'chat',
    materialRefs: Array.isArray(user.meta?.materialRefs) ? user.meta.materialRefs : [],
    localOnly,
    retryUserMessageId: user.id,
    ...(typeof assistant.meta?.requestId === 'string' && assistant.meta.requestId ? { requestId: assistant.meta.requestId } : {}),
  }
}
