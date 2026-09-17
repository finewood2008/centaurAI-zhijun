import type { ConversationDetail, Message } from '../services/api'

export interface ReplyIdentity {
  conversationId: string
  messageId: string
  userMessageId: string
  requestId: string
}

/** Never infer completion from the last message or from a successful job alone. */
export function persistedReply(detail: ConversationDetail, identity: ReplyIdentity): Message | null {
  if (!identity.requestId || !identity.messageId || identity.messageId.startsWith('local-') ||
      !identity.userMessageId || identity.userMessageId.startsWith('local-') ||
      detail.conversation.id !== identity.conversationId) return null
  const matches = detail.messages.filter(message => message.id === identity.messageId)
  if (matches.length !== 1) return null
  const message = matches[0]!
  return message.role === 'assistant' && message.conversationId === identity.conversationId &&
    message.status === 'complete' && Boolean(message.content.trim()) &&
    message.meta?.replyTo === identity.userMessageId && message.meta?.requestId === identity.requestId
    ? message : null
}

/** Bounded read-only reconciliation. Cancellation and ownership changes fail closed. */
export async function reconcileReply(
  identity: ReplyIdentity,
  read: (cid: string, signal: AbortSignal) => Promise<ConversationDetail>,
  isCurrent: () => boolean,
  signal?: AbortSignal,
  delays: readonly number[] = [0, 1500, 3000],
): Promise<Message | null> {
  if (!identity.requestId || identity.messageId.startsWith('local-') || !isCurrent() || signal?.aborted) return null
  const controller = new AbortController()
  const abort = () => controller.abort()
  signal?.addEventListener('abort', abort, { once: true })
  const timer = setTimeout(abort, 8000)
  const active = () => !controller.signal.aborted && isCurrent()
  try {
    for (const delay of delays.slice(0, 3)) {
      if (!active()) break
      if (delay) await new Promise<void>(resolve => {
        const done = () => { clearTimeout(wait); controller.signal.removeEventListener('abort', done); resolve() }
        const wait = setTimeout(done, delay)
        controller.signal.addEventListener('abort', done, { once: true })
      })
      if (!active()) break
      try {
        // Race bounds even a reader that accidentally ignores AbortSignal.
        let stop!: () => void
        const stopped = new Promise<null>(resolve => { stop = () => resolve(null); controller.signal.addEventListener('abort', stop, { once: true }) })
        let detail: ConversationDetail | null
        try { detail = await Promise.race([read(identity.conversationId, controller.signal), stopped]) }
        finally { controller.signal.removeEventListener('abort', stop) }
        if (!active()) break
        if (detail) {
          const reply = persistedReply(detail, identity)
          if (reply) return reply
        }
      } catch { break /* A failed read is not a reason to amplify traffic. */ }
    }
    return null
  } finally {
    clearTimeout(timer)
    signal?.removeEventListener('abort', abort)
    controller.abort()
  }
}
