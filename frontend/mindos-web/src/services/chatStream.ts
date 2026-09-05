import { ApiError } from './api'
import { streamPost, type SseHandlers } from './sse'
import { canRefreshRoute, prepareChatRoute } from './taskRouting'
import { reportReplyFailure } from '@/composables/useReplyRecovery'

/** Only an HTTP rejection before streaming can be re-previewed automatically.
 * The request identity and source ancestry survive; permissions are checked again.
 * A started stream, changed source, network failure or cancellation is never replayed.
 */
export async function streamChat(
  conversationId: string,
  body: Record<string, unknown>,
  handlers: SseHandlers,
  signal?: AbortSignal,
  isCurrent: () => boolean = () => true,
): Promise<boolean> {
  let request = body
  let received = false
  const guardedHandlers = Object.fromEntries(Object.entries(handlers).map(([event, handler]) => [event, (data: unknown) => {
    received = true
    handler(data)
  }]))
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      signal?.throwIfAborted()
      await streamPost(`/mindos/conversations/${encodeURIComponent(conversationId)}/messages`, request, guardedHandlers, signal)
      return true
    } catch (error) {
      if (attempt === 0 && !received && !signal?.aborted && error instanceof ApiError && error.status === 409 && canRefreshRoute(error)) {
        if (!isCurrent()) return false
        const updated = await prepareChatRoute(conversationId, request, signal)
        if (!updated || !isCurrent()) return false
        request = updated
        continue
      }
      reportReplyFailure(conversationId, request.replyAssistance, error)
      throw error
    }
  }
  return false
}
