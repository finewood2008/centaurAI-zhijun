// 实体「作为标签添加」（P14-04）。
//
// 复用正式标签的 POST /materials/{id}/tags add 语义，绝不把实体伪装成
// 「候选标签确认」；请求前固定资料 ID，返回后校验当前详情仍一致，防止在
// 已切换资料间误写（沿用 P14-03/P14-04 的防串台策略）。
import type { EntityExtraction } from '../services/api'

export interface EntityTagAddContext {
  // 当前详情快照（materialId + 现有正式标签）；null 表示详情未就绪
  getDetail: () => { materialId: string; tags: string[] } | null
  // 是否已有实体标签添加请求在途（同一时刻只允许一个）
  isBusy: () => boolean
  // 记录在途实体（按钮显示「添加中…」）；结束后置空字符串
  setBusyEntityId: (entityId: string) => void
  setMaterialTags: (materialId: string, tags: string[], action: 'add' | 'remove') => Promise<{ tags: string[] }>
  // 成功后刷新正式标签（可顺带刷新关联推荐）
  applyTags: (materialId: string, tags: string[]) => void
  // 失败且当前资料仍一致时的错误提示
  onError: (message: string) => void
}

export function createEntityTagAdder(
  ctx: EntityTagAddContext,
): (entity: EntityExtraction) => Promise<boolean> {
  return async function addEntityAsTag(entity) {
    const detail = ctx.getDetail()
    if (!detail || ctx.isBusy()) return false
    const materialId = detail.materialId
    // 已在正式标签中 → 直接忽略（按钮显示「已添加」并禁用）
    if (detail.tags.includes(entity.name)) return false
    ctx.setBusyEntityId(entity.entityId)
    try {
      const result = await ctx.setMaterialTags(materialId, [entity.name], 'add')
      // 返回后二次校验：当前详情仍是该资料，才允许写回
      const current = ctx.getDetail()
      if (!current || current.materialId !== materialId) return false
      ctx.applyTags(current.materialId, result.tags)
      return true
    } catch (e) {
      const current = ctx.getDetail()
      if (current && current.materialId === materialId) {
        ctx.onError(e instanceof Error ? e.message : '添加实体标签失败')
      }
      return false
    } finally {
      ctx.setBusyEntityId('')
    }
  }
}