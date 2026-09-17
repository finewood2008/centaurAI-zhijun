export interface RefreshableDraft {
  revision: string | null
  confirmed?: boolean
  userEdited?: boolean
}

export interface GeneratedDraftRefresherOptions<T extends RefreshableDraft> {
  fetch: (materialId: string) => Promise<T>
  currentMaterialId: () => string | null
  currentDraft: () => T | null
  isDirty: () => boolean
  apply: (draft: T) => void
}

export interface GeneratedDraftRefresher {
  refresh(materialId: string): Promise<void>
  invalidate(): void
}

export interface PollableGeneratedDraft extends RefreshableDraft {
  status: string
}

export interface GeneratedDraftPollerOptions<T extends PollableGeneratedDraft>
  extends GeneratedDraftRefresherOptions<T> {
  onTimeout: (materialId: string) => void
  intervalMs?: number
  timeoutMs?: number
}

export interface GeneratedDraftPoller {
  start(materialId: string): void
  stop(): void
}

/**
 * Refresh an asynchronously generated draft without allowing a delayed request
 * to overwrite another material, a newer revision, or local user edits.
 */
export function createGeneratedDraftRefresher<T extends RefreshableDraft>(
  options: GeneratedDraftRefresherOptions<T>,
): GeneratedDraftRefresher {
  let session = 0

  function invalidate(): void {
    session += 1
  }

  async function refresh(materialId: string): Promise<void> {
    const displayed = options.currentDraft()
    if (
      !displayed || displayed.confirmed || displayed.userEdited || options.isDirty()
      || options.currentMaterialId() !== materialId
    ) return
    const displayedRevision = displayed.revision
    const mySession = ++session
    try {
      const latest = await options.fetch(materialId)
      const current = options.currentDraft()
      if (
        mySession !== session || options.currentMaterialId() !== materialId
        || !current || current.revision !== displayedRevision
        || current.confirmed || current.userEdited || options.isDirty()
      ) return
      options.apply(latest)
    } catch {
      // A later analysis result or an explicit detail refresh will retry.
    }
  }

  return { refresh, invalidate }
}

/**
 * Poll a generated draft independently from the summary and analysis tasks.
 * A session token cancels in-flight reads after navigation/unmount. The current
 * draft revision and dirty state are checked again after every await so a
 * generated result cannot overwrite a newer response or local edits.
 */
export function createGeneratedDraftPoller<T extends PollableGeneratedDraft>(
  options: GeneratedDraftPollerOptions<T>,
): GeneratedDraftPoller {
  const intervalMs = options.intervalMs ?? 3000
  const timeoutMs = options.timeoutMs ?? 200_000
  let session = 0
  let timer: ReturnType<typeof setTimeout> | null = null

  function clearTimer(): void {
    if (timer !== null) clearTimeout(timer)
    timer = null
  }

  function stop(): void {
    session += 1
    clearTimer()
  }

  function canContinue(materialId: string, current: T | null): current is T {
    return Boolean(
      current
      && current.status === 'pending'
      && !current.confirmed
      && !current.userEdited
      && !options.isDirty()
      && options.currentMaterialId() === materialId,
    )
  }

  function start(materialId: string): void {
    stop()
    const mySession = ++session
    const deadline = Date.now() + timeoutMs

    const scheduleNext = (tick: () => Promise<void>): void => {
      if (mySession !== session || !canContinue(materialId, options.currentDraft())) return
      timer = setTimeout(tick, intervalMs)
    }

    const tick = async (): Promise<void> => {
      if (mySession !== session) return
      const displayed = options.currentDraft()
      if (!canContinue(materialId, displayed)) return
      if (Date.now() >= deadline) {
        options.onTimeout(materialId)
        return
      }
      const displayedRevision = displayed.revision
      let latest: T | null = null
      try {
        latest = await options.fetch(materialId)
      } catch {
        latest = null
      }
      if (mySession !== session) return
      const current = options.currentDraft()
      if (!canContinue(materialId, current)) return
      if (current.revision !== displayedRevision) {
        scheduleNext(tick)
        return
      }
      if (latest) {
        options.apply(latest)
        if (latest.status !== 'pending' || latest.confirmed || latest.userEdited) return
      }
      scheduleNext(tick)
    }

    void tick()
  }

  return { start, stop }
}
