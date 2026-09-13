import { isDesktopProduct, onProductScopeReset } from '../shared/productScope.ts'
import { transportRequest } from './transport.ts'
import { shallowRef } from 'vue'
import { buildHeaders, throwApiError } from './api'
import { reportReplyFailure } from '@/composables/useReplyRecovery'

export interface RoutePreview {
  revision: string; conversationId: string; purpose: string; purposeLabel: string
  deConsentRequired?: boolean
  service: { id: string; name: string; model: string; external: boolean }
  missing: string[]; blocked: string[]; reason: string
  sources: Array<{ key: string; title: string; text: string; version: string; blocked: string; kind: string }>
  excluded: Array<{ id: string; reason: string }>
  request: { system: string; messages: Array<{ role: string; content: string }> }
  contextPlan?: import('./api').ContextPlan
  defaultAuthorization?: { enabled: boolean; revision: number; includeFiles?: boolean; includeCharter?: boolean; autoEgress?: boolean; applies?: boolean }
  charterBasis?: { scope: string; charterId: string; version: number; clauseIds: string[] }
  charterConflict?: { code: string; detail: string; charterId: string; charterVersion: number; clauses: Array<{ id: string; version: number; text: string; control: string }>; canOverride: boolean; exceptionKey: string; notice?: string } | null
  charterUnresolved?: Array<{ id: string; text: string; reason: string }>
}
export function needsDeConsent(preview: RoutePreview): boolean {
  return isDesktopProduct() && preview.service.external && preview.deConsentRequired === true
}
export function canUseDefaultDeConsent(preview: RoutePreview): boolean {
  // Eligibility is computed by the trusted domain worker. The grant endpoint
  // and DE gateway independently re-read the policy and exact preview.
  return needsDeConsent(preview) && preview.defaultAuthorization?.applies === true
    && preview.defaultAuthorization.autoEgress === true
    && Number.isInteger(preview.defaultAuthorization.revision) && preview.defaultAuthorization.revision > 0
}
function consentKeys(preview: RoutePreview, choice: RouteChoice): string[] | undefined {
  // DE receipts cover this exact request, including every source, even when the list is empty.
  return needsDeConsent(preview) ? preview.sources.map(source => source.key) : choice.keys
}
export type RouteChoice = { action: 'allow' | 'local' | 'omit' | 'cancel' | 'exception'; keys?: string[] }
export const routeQuestion = shallowRef<{ preview: RoutePreview; allowOmit: boolean; done: (choice: RouteChoice) => void } | null>(null)

export type RagV2Decision = 'masked' | 'original' | 'continue-passed' | 'retry' | 'risk-release' | 'cancel'
export interface RagV2Prompt {
  interactionId: string
  status: 'sensitive_confirmation_required' | 'sensitive_check_unavailable'
  hits: Array<{ category: string; redactedPreview: string; location?: string | { page?: number; paragraph?: number; section?: string }; title?: string }>
  detectionNotice?: { code?: string; message?: string; retrievedCount?: number; checkedCount?: number; withheldCount?: number; retryable?: boolean; riskEligibleCount?: number; riskMessage?: string }
  passedCount: number
  canReadOriginal: boolean
  riskAvailable: boolean
}
export const ragQuestion = shallowRef<{ prompt: RagV2Prompt; done: (choice: RagV2Decision) => void } | null>(null)

export function ragPromptOf(error: unknown): RagV2Prompt | null {
  if (!error || typeof error !== 'object') return null
  const value = error as { code?: unknown; ragV2?: unknown }
  if (!['RAG_SENSITIVE_CONFIRMATION_REQUIRED', 'RAG_SENSITIVE_CHECK_INCOMPLETE'].includes(String(value.code || ''))
      || !value.ragV2 || typeof value.ragV2 !== 'object') return null
  return value.ragV2 as RagV2Prompt
}

onProductScopeReset(() => {
  routeQuestion.value?.done({ action: 'cancel' }); routeQuestion.value = null
  ragQuestion.value?.done('cancel'); ragQuestion.value = null
})

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

export function askRag(prompt: RagV2Prompt, signal?: AbortSignal): Promise<RagV2Decision> {
  ragQuestion.value?.done('cancel')
  return new Promise(resolve => {
    const question = { prompt, done: (choice: RagV2Decision) => {
      signal?.removeEventListener('abort', cancel)
      if (ragQuestion.value === question) ragQuestion.value = null
      resolve(choice)
    } }
    const cancel = () => question.done('cancel')
    ragQuestion.value = question
    if (signal?.aborted) cancel()
    else signal?.addEventListener('abort', cancel, { once: true })
  })
}

export async function routingRequest<T = any>(path: string, method = 'GET', data?: unknown, signal?: AbortSignal): Promise<T> {
  const res = await transportRequest(`/api${path}`, { method, headers: buildHeaders({ headers: { 'Content-Type': 'application/json' } }),
    body: data === undefined ? undefined : JSON.stringify(data), signal })
  if (!res.ok) await throwApiError(res)
  return res.json()
}
export const routePath = (id: string) => `/mindos/conversations/${encodeURIComponent(id)}/routing`
const ragDecisionPath = (id: string) => `/mindos/conversations/${encodeURIComponent(id)}/rag-v2/decision`
export function requiresFreshRagSearch(error: unknown): boolean {
  return !!error && typeof error === 'object' && [
    'CONFIRM_TOKEN_EXPIRED', 'CONFIRM_TOKEN_CONSUMED', 'CONFIRM_CONTEXT_CHANGED',
    'RAG_CONFIRMATION_EXPIRED',
  ].includes(String((error as { code?: unknown }).code || ''))
}
export const submitRagDecision = (id: string, prompt: RagV2Prompt, action: RagV2Decision, signal?: AbortSignal) =>
  routingRequest(ragDecisionPath(id), 'POST', { interactionId: prompt.interactionId, action }, signal)

export async function grantDefaultDeConsent(id: string, preview: RoutePreview, signal?: AbortSignal): Promise<boolean> {
  if (!canUseDefaultDeConsent(preview)) return false
  try {
    await routingRequest(routePath(id) + '/grant', 'POST', {
      revision: preview.revision,
      keys: preview.sources.map(source => source.key),
      defaultPolicyRevision: preview.defaultAuthorization!.revision,
    }, signal)
    return true
  } catch (error) {
    if (!signal?.aborted && error && typeof error === 'object'
        && ['DEFAULT_CONSENT_CHANGED', 'MODEL_DEFAULT_CONSENT_CHANGED',
          'WORKSPACE_DEFAULT_CONSENT_CHANGED', 'WORKSPACE_DEFAULT_CONSENT_MISMATCH']
          .includes(String((error as { code?: unknown }).code))) return false
    throw error
  }
}

/** A stale preview may be rebuilt, never interpreted as permission to bypass it. */
export function canRefreshRoute(error: unknown): boolean {
  return !!error && typeof error === 'object' && (error as { status?: unknown }).status === 409
    && ['ROUTE_CHANGED', 'PREVIEW_EXPIRED', 'RAG_V2_CONTEXT_CHANGED']
      .includes(String((error as { code?: unknown }).code))
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
      if (!preview.service.external || (!preview.missing.length && !needsDeConsent(preview))) return { ...data, routeRevision: preview.revision }
      if (await grantDefaultDeConsent(id, preview, signal)) continue
      const choice = await askRoute(preview, true, signal)
      if (choice.action === 'cancel') return null
      if (choice.action === 'local') data = { ...data, localOnly: true }
      else if (choice.action === 'omit') data = { ...data, omitSources: true }
      else if (choice.action === 'allow') await routingRequest(routePath(id) + '/grant', 'POST', { revision: preview.revision, keys: consentKeys(preview, choice) }, signal)
    } catch (error) {
      const ragPrompt = ragPromptOf(error)
      if (!signal?.aborted && ragPrompt) {
        const choice = await askRag(ragPrompt, signal)
        try { await submitRagDecision(id, ragPrompt, choice, signal) }
        catch (decisionError) {
          if (!signal?.aborted && requiresFreshRagSearch(decisionError)) continue
          throw decisionError
        }
        if (choice === 'cancel') return null
        continue
      }
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
      if (!preview.service.external || (!preview.missing.length && !needsDeConsent(preview))) {
        return await routingRequest<T>(path, 'POST', { ...data, routeRevision: preview.revision }, signal)
      }
      if (await grantDefaultDeConsent(id, preview, signal)) continue
      const choice = await askRoute(preview, false, signal)
      if (choice.action === 'cancel') throw new Error('已取消生成，已填写的内容没有变化。')
      if (choice.action === 'local') data = { ...data, localOnly: true }
      else if (choice.action === 'allow') await routingRequest(routePath(id) + '/grant', 'POST', { revision: preview.revision, keys: consentKeys(preview, choice) }, signal)
    } catch (error) {
      const ragPrompt = ragPromptOf(error)
      if (!signal?.aborted && ragPrompt) {
        const choice = await askRag(ragPrompt, signal)
        try { await submitRagDecision(id, ragPrompt, choice, signal) }
        catch (decisionError) {
          if (!signal?.aborted && requiresFreshRagSearch(decisionError)) continue
          throw decisionError
        }
        if (choice === 'cancel') throw new Error('已取消生成，已填写的内容没有变化。')
        continue
      }
      if (!signal?.aborted && !refreshed && canRefreshRoute(error)) { refreshed = true; continue }
      throw error
    }
  }
  throw new Error('来源已变化，请重新核对。')
}
