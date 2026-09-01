export type SourceRefInput = { sourceType: 'material' | 'knowledge'; id: string }
export type VersionSourceAction = 'replace' | 'keepBoth'

/** 用户确认后才生成新的完整来源列表；不承担任何自动迁移。 */
export function applyVersionSourceAction(
  refs: SourceRefInput[], oldMaterialId: string, newMaterialId: string, action: VersionSourceAction,
): SourceRefInput[] {
  const hasNew = refs.some((ref) => ref.sourceType === 'material' && ref.id === newMaterialId)
  const next: SourceRefInput[] = []
  for (const ref of refs) {
    if (action === 'replace' && ref.sourceType === 'material' && ref.id === oldMaterialId) {
      next.push({ sourceType: 'material', id: newMaterialId })
      continue
    }
    next.push(ref)
    if (action === 'keepBoth' && !hasNew && ref.sourceType === 'material' && ref.id === oldMaterialId) {
      next.push({ sourceType: 'material', id: newMaterialId })
    }
  }
  const seen = new Set<string>()
  return next.filter((ref) => {
    const key = `${ref.sourceType}:${ref.id}`
    if (seen.has(key)) return false
    seen.add(key)
    return true
  })
}
