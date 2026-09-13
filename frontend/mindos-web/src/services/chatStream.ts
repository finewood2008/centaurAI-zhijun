import { ApiError } from './api'
import { streamPost, type SseHandlers } from './sse'
import { askRag, canRefreshRoute, prepareChatRoute, ragPromptOf, requiresFreshRagSearch, submitRagDecision } from './taskRouting'
import { reportReplyFailure } from '@/composables/useReplyRecovery'

const CHAT_TERMINAL_EVENTS = ['message_done', 'error'] as const

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
  const ragState: { terminal: (Record<string, unknown> & { userMessageId?: string }) | null } = { terminal: null }
  const guardedHandlers = Object.fromEntries(Object.entries(handlers).map(([event, handler]) => [event, (data: unknown) => {
    received = true
    if (event === 'error' && ragPromptOf(data)) {
      ragState.terminal = data as Record<string, unknown> & { userMessageId?: string }
      return
    }
    handler(data)
  }]))
  for (let attempt = 0; attempt < 6; attempt++) {
    try {
      signal?.throwIfAborted()
      await streamPost(`/mindos/conversations/${encodeURIComponent(conversationId)}/messages`, request, guardedHandlers, signal, {
        terminalEvents: CHAT_TERMINAL_EVENTS,
      })
      if (ragState.terminal) {
        const terminal = ragState.terminal
        ragState.terminal = null
        const prompt = ragPromptOf(terminal)
        if (!prompt || signal?.aborted || !isCurrent()) return false
        const choice = await askRag(prompt, signal)
        try { await submitRagDecision(conversationId, prompt, choice, signal) }
        catch (decisionError) {
          if (!requiresFreshRagSearch(decisionError)) throw decisionError
        }
        if (choice === 'cancel') {
          handlers.error?.(terminal)
          return true
        }
        const updated = await prepareChatRoute(conversationId, {
          ...request,
          ...(terminal.userMessageId ? { retryUserId: terminal.userMessageId } : {}),
        }, signal)
        if (!updated || !isCurrent()) return false
        request = updated
        continue
      }
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
