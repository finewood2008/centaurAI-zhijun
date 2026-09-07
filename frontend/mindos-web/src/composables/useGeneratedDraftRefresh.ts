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
