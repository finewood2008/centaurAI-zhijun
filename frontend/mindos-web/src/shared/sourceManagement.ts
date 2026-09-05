/**
 * 来源关系属于已持久化的知识卡片。新建卡片尚无 knowledgeId，不能独立保存
 * 来源；在创建接口支持原子写入前，必须禁止编辑以避免用户误以为来源已保存。
 */
export function canManageKnowledgeSources(isNew: boolean, locked: boolean): boolean {
  return !isNew && !locked
}
