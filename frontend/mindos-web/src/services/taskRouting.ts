import { shallowRef } from 'vue'
import { buildHeaders, throwApiError } from './api'
import { reportReplyFailure } from '@/composables/useReplyRecovery'

export interface RoutePreview {
  revision: string; conversationId: string; purpose: string; purposeLabel: string
  service: { id: string; name: string; model: string; external: boolean }
  missing: string[]; blocked: string[]; reason: string
  sources: Array<{ key: string; title: string; text: string; version: string; blocked: string; kind: string }>
  excluded: Array<{ id: string; reason: string }>
  request: { system: string; messages: Array<{ role: string; content: string }> }
  contextPlan?: import('./api').ContextPlan
  charterBasis?: { scope: string; charterId: string; version: number; clauseIds: string[] }
  charterConflict?: { code: string; detail: string; charterId: string; charterVersion: number; clauses: Array<{ id: string; version: number; text: string; control: string }>; canOverride: boolean; exceptionKey: string; notice?: string } | null
  charterUnresolved?: Array<{ id: string; text: string; reason: string }>
}
export type RouteChoice = { action: 'allow' | 'local' | 'omit' | 'cancel' | 'exception'; keys?: string[] }
export const routeQuestion = shallowRef<{ preview: RoutePreview; allowOmit: boolean; done: (choice: RouteChoice) => void } | null>(null)

export function askRoute(preview: RoutePreview, allowOmit = false, signal?: AbortSignal): Promise<RouteChoice> {
  routeQuestion.value?.done({ action: 'cancel' })
  return new Promise(resolve => {
    const question = { preview, allowOmit, done: (choice: RouteChoice) => {
      signal?.removeEventListener('abort', cancel)
      if (routeQuestion.value === question) routeQuestion.value = null
      resolve(choice)
    } }
    const cancel = () => question.done({ action: 'cancel' })
    routeQuestion.value = question
    if (signal?.aborted) cancel()
    else signal?.addEventListener('abort', cancel, { once: true })
  })
}

export async function routingRequest<T = any>(path: string, method = 'GET', data?: unknown, signal?: AbortSignal): Promise<T> {
  const res = await fetch(`/api${path}`, { method, headers: buildHeaders({ headers: { 'Content-Type': 'application/json' } }),
    body: data === undefined ? undefined : JSON.stringify(data), signal })
  if (!res.ok) await throwApiError(res)
  return res.json()
}
export const routePath = (id: string) => `/mindos/conversations/${encodeURIComponent(id)}/routing`

/** A stale preview may be rebuilt, never interpreted as permission to bypass it. */
export function canRefreshRoute(error: unknown): boolean {
  return !!error && typeof error === 'object' && (error as { status?: unknown }).status === 409
    && ['ROUTE_CHANGED', 'PREVIEW_EXPIRED'].includes(String((error as { code?: unknown }).code))
}

export async function prepareChatRoute(id: string, body: Record<string, unknown>, signal?: AbortSignal): Promise<(Record<string, unknown> & { routeRevision: string }) | null> {
  let data: Record<string, unknown> = { ...body, requestId: body.requestId || crypto.randomUUID() }
  let refreshed = false
  // Refresh the preview after granting: grants can change the exact revision.
  for (let i = 0; i < 6; i++) {
    try {
      signal?.throwIfAborted()
      const preview = await routingRequest<RoutePreview>(routePath(id) + '/preview', 'POST', data, signal)
      signal?.throwIfAborted()
      if (preview.charterConflict) {
        const choice = await askRoute(preview, false, signal)
        if (choice.action === 'cancel') return null
        if (choice.action === 'local') { data = { ...data, localOnly: true }; continue }
        if (choice.action === 'exception' && preview.charterConflict.canOverride) {
          const result = await routingRequest<{ exceptionId: string }>(routePath(id) + '/charter-exception', 'POST', { revision: preview.revision, exceptionKey: preview.charterConflict.exceptionKey, acknowledge: true }, signal)
          data = { ...data, charterExceptionId: result.exceptionId }; continue
        }
        return null
      }
      if (!preview.service.external || !preview.missing.length) return { ...data, routeRevision: preview.revision }
      const choice = await askRoute(preview, true, signal)
      if (choice.action === 'cancel') return null
      if (choice.action === 'local') data = { ...data, localOnly: true }
      else if (choice.action === 'omit') data = { ...data, omitSources: true }
      else await routingRequest(routePath(id) + '/grant', 'POST', { revision: preview.revision, keys: choice.keys }, signal)
    } catch (error) {
      if (!signal?.aborted && !refreshed && canRefreshRoute(error)) { refreshed = true; continue }
      reportReplyFailure(id, data.replyAssistance, error)
      throw error
    }
  }
  throw new Error('内容或授权仍在变化，请重新核对后发送。')
}

export async function routedTask<T>(id: string, path: string, body: object, signal?: AbortSignal): Promise<T> {
  let data: Record<string, unknown> = { requestId: crypto.randomUUID(), ...body }
  let refreshed = false
  for (let i = 0; i < 6; i++) {
    try {
      signal?.throwIfAborted()
      const { routePreview: preview } = await routingRequest<{ routePreview: RoutePreview }>(path, 'POST', { ...data, previewOnly: true }, signal)
      signal?.throwIfAborted()
      if (preview.charterConflict) {
        const choice = await askRoute(preview, false, signal)
        if (choice.action === 'local') { data = { ...data, localOnly: true }; continue }
        if (choice.action === 'exception' && preview.charterConflict.canOverride) {
          const result = await routingRequest<{ exceptionId: string }>(routePath(id) + '/charter-exception', 'POST', { revision: preview.revision, exceptionKey: preview.charterConflict.exceptionKey, acknowledge: true }, signal)
          data = { ...data, charterExceptionId: result.exceptionId }; continue
        }
        throw new Error('已取消本次处理，工作稿与输入均保留。')
      }
      if (!preview.service.external || !preview.missing.length) {
        return await routingRequest<T>(path, 'POST', { ...data, routeRevision: preview.revision }, signal)
      }
      const choice = await askRoute(preview, false, signal)
      if (choice.action === 'cancel') throw new Error('已取消生成，已填写的内容没有变化。')
      if (choice.action === 'local') data = { ...data, localOnly: true }
      else await routingRequest(routePath(id) + '/grant', 'POST', { revision: preview.revision, keys: choice.keys }, signal)
    } catch (error) {
      if (!signal?.aborted && !refreshed && canRefreshRoute(error)) { refreshed = true; continue }
      throw error
    }
  }
  throw new Error('来源已变化，请重新核对。')
}
