import { ApiError, buildHeaders, throwApiError } from './api'
import { transportRequest } from './transport.ts'

export interface SensitiveRuleStatus {
  state: 'disabled' | 'active' | 'pending' | 'applying' | 'failed'
  applying: boolean
  scanEnabled?: boolean
  historicalScanRequired?: boolean
  retryAvailable?: boolean
  targetDetectorRevision?: string | null
  rolloutManage?: boolean
}

export async function getSensitiveRuleStatus(signal?: AbortSignal, rolloutManage = false): Promise<SensitiveRuleStatus> {
  const response = await transportRequest(rolloutManage
    ? '/api/mindos/settings/sensitive-rules/rollout/status'
    : '/api/mindos/settings/sensitive-rules/status', {
    headers: buildHeaders(), cache: 'no-store', signal,
  })
  if (!response.ok) await throwApiError(response)
  const raw: unknown = await response.json()
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) {
    throw new ApiError('规则应用状态暂时无法读取', 502, 'INVALID_SENSITIVE_RULE_STATUS_RESPONSE')
  }
  const value = raw as Record<string, unknown>
  if (typeof value.state !== 'string' || typeof value.applying !== 'boolean') {
    throw new ApiError('规则应用状态暂时无法读取', 502, 'INVALID_SENSITIVE_RULE_STATUS_RESPONSE')
  }
  if (!rolloutManage) {
    if (!Object.keys(value).every(key => ['state', 'applying', 'historicalScanRequired'].includes(key))
        || (value.historicalScanRequired !== undefined && typeof value.historicalScanRequired !== 'boolean')
        || !['active', 'applying'].includes(value.state)
        || value.applying !== (value.state === 'applying')) {
      throw new ApiError('规则应用状态暂时无法读取', 502, 'INVALID_SENSITIVE_RULE_STATUS_RESPONSE')
    }
    return { state: value.state as SensitiveRuleStatus['state'], applying: value.applying,
      ...(typeof value.historicalScanRequired === 'boolean' ? { historicalScanRequired: value.historicalScanRequired } : {}),
      rolloutManage: false }
  }
  if (!['disabled', 'active', 'pending', 'applying', 'failed'].includes(value.state)
      || typeof value.scanEnabled !== 'boolean'
      || typeof value.historicalScanRequired !== 'boolean'
      || typeof value.retryAvailable !== 'boolean'
      || !(typeof value.targetDetectorRevision === 'string' || value.targetDetectorRevision === null)
      || value.applying !== (value.state === 'applying')) {
    throw new ApiError('规则应用状态暂时无法读取', 502, 'INVALID_SENSITIVE_RULE_STATUS_RESPONSE')
  }
  return { state: value.state as SensitiveRuleStatus['state'], applying: value.applying,
    scanEnabled: value.scanEnabled, historicalScanRequired: value.historicalScanRequired,
    retryAvailable: value.retryAvailable, targetDetectorRevision: value.targetDetectorRevision,
    rolloutManage: true }
}

const POLL_INTERVAL_MS = 30_000
const MAX_TIMER_DELAY_MS = 2_147_483_647

/** Optional settings-only reader: one request at a time, no polling once active. */
export function createSensitiveRuleStatusPoller(options: {
  apply: (status: SensitiveRuleStatus) => void
  onError: (error: unknown | null) => void
  read?: (signal: AbortSignal) => Promise<SensitiveRuleStatus>
}) {
  const read = options.read || getSensitiveRuleStatus
  let enabled = false
  let disposed = false
  let generation = 0
  let timer: ReturnType<typeof setTimeout> | undefined
  let controller: AbortController | undefined
  let flight: Promise<void> | undefined
  let requested = false
  let authorizationDenied = false
  let automaticPollingPaused = false
  let nextAutomaticReadAt = 0

  function clearTimer() {
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
  }

  function schedule(delay: number) {
    clearTimer()
    if (!Number.isFinite(delay) || delay > MAX_TIMER_DELAY_MS) {
      // Browsers/Node may turn an overflowing timeout into an immediate one.
      // Leave the visible error in place and require an explicit refresh.
      automaticPollingPaused = true
      nextAutomaticReadAt = 0
      requested = false
      return
    }
    nextAutomaticReadAt = Date.now() + delay
    if (enabled && !disposed && !authorizationDenied) timer = setTimeout(() => {
      timer = undefined
      void run()
    }, delay)
  }

  function run(): Promise<void> {
    if (!enabled || disposed || authorizationDenied) return Promise.resolve()
    if (flight) { requested = true; return flight }
    clearTimer()
    nextAutomaticReadAt = 0
    const ticket = generation
    const current = new AbortController()
    controller = current
    flight = Promise.resolve().then(async () => {
      try {
        if (current.signal.aborted) return
        const result = await read(current.signal)
        if (!enabled || disposed || current.signal.aborted || ticket !== generation) return
        options.apply(result)
        options.onError(null)
        if (result.state === 'applying') schedule(POLL_INTERVAL_MS)
      } catch (error) {
        if (!enabled || disposed || current.signal.aborted || ticket !== generation) return
        options.onError(error)
        const status = error && typeof error === 'object' && 'status' in error ? error.status : undefined
        authorizationDenied = status === 401 || status === 403
        const retryAfter = error && typeof error === 'object' && 'retryAfter' in error ? error.retryAfter : undefined
        const delay = typeof retryAfter === 'number' && Number.isFinite(retryAfter) && retryAfter >= 0
          ? Math.max(POLL_INTERVAL_MS, Math.ceil(retryAfter * 1000)) : POLL_INTERVAL_MS
        if (!authorizationDenied) schedule(delay)
      } finally {
        if (controller === current) controller = undefined
        flight = undefined
        if (requested && enabled && !disposed && !authorizationDenied && !automaticPollingPaused) {
          requested = false
          void run()
        }
      }
    })
    return flight
  }

  return {
    setEnabled(value: boolean) {
      if (disposed || enabled === value) return
      enabled = value
      generation++
      clearTimer()
      if (!value) { requested = false; controller?.abort() }
      else if (!automaticPollingPaused) {
        const remaining = nextAutomaticReadAt - Date.now()
        if (remaining > 0) schedule(remaining)
        else void run()
      }
    },
    refresh(retryAuthorization = false) {
      if (disposed || (authorizationDenied && !retryAuthorization)) return
      if (retryAuthorization) authorizationDenied = false
      automaticPollingPaused = false
      nextAutomaticReadAt = 0
      generation++
      clearTimer()
      // A save invalidates a read started before it. Wait for that read to settle
      // before fetching again, even if its transport ignores cancellation.
      controller?.abort()
      if (flight) { requested = true; return }
      void run()
    },
    dispose() {
      disposed = true
      enabled = false
      generation++
      requested = false
      clearTimer()
      controller?.abort()
    },
  }
}
