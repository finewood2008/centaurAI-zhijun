import type { CharterClause, CharterWorkspace, GrowthCharter } from '../services/api'

export const charterKinds: Record<CharterClause['kind'], string> = {
  principle: '我认可的原则', aspiration: '我的愿望（不代表已经做到）', preference: '合作偏好', boundary: '我的边界',
}
export const charterControls: Record<string, string> = {
  memory_manual: '仅在我要求时整理记忆', no_proactive: '不主动提醒',
  local_only: '仅本地处理', confirm_decisions: '决定前先向我确认',
}
export function charterExecutionLabel(clause: CharterClause): string {
  if (clause.clarification || (clause.scope === 'contextual' && !clause.context?.trim())) return '需澄清，暂不执行'
  return clause.control && charterControls[clause.control] ? `自动遵守 · ${charterControls[clause.control]}` : '指导建议 · 不自动执行'
}
export function charterDocument(charter: GrowthCharter): string {
  if (charter.document) return charter.document
  const sections: Array<[string, string | string[] | undefined]> = [
    ['我的愿望', charter.vision], ['当前角色', charter.roles], ['我的原则', charter.principles],
    ['阶段目标', charter.goals], ['我的边界', charter.boundaries], ['合作方式', charter.challengeStyle], ['暂不触碰', charter.quietDomains],
  ]
  return sections.filter(([, value]) => value?.length).map(([title, value]) => `## ${title}\n${Array.isArray(value) ? value.map(v => `- ${v}`).join('\n') : value}`).join('\n\n')
}
export function workspaceMarkdown(workspace: Pick<CharterWorkspace, 'document' | 'documentFormat' | 'sourceText' | 'clauses'>): string {
  if (workspace.documentFormat === 'markdown') return workspace.document
  if (workspace.document) return workspace.document
  const legacy = renderCharterClauses(workspace.clauses)
  return legacy || workspace.sourceText || ''
}
export function renderCharterClauses(clauses: CharterClause[]): string {
  const sections = new Map<string, string[]>()
  for (const clause of clauses) {
    const text = clause.scope === 'contextual' && clause.context ? `${clause.text}（适用情境：${clause.context}）` : clause.text
    sections.set(clause.section, [...(sections.get(clause.section) ?? []), text])
  }
  return [...sections].map(([section, texts]) => `## ${section}\n\n${texts.join('\n\n')}`).join('\n\n')
}
export function downloadCharterMarkdown(text: string): void {
  const url = URL.createObjectURL(new Blob([text], { type: 'text/markdown;charset=utf-8' }))
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = '人生章程.md'; anchor.click()
  URL.revokeObjectURL(url)
}
export function cloneClauses(clauses: CharterClause[]): CharterClause[] {
  return JSON.parse(JSON.stringify(clauses))
}
export function charterSourceLabel(kind: string): string {
  return ({ charter: '已确认章程', charter_workspace: '章程工作稿', charter_draft: '章程草稿', message: '对话消息', material: '本机资料', claim: '本体理解', alignment: '自我校准', summary: '对话小结', decision: '历史判断', episode: '情境复盘' } as Record<string, string>)[kind] || kind
}
export function newCharterClause(): CharterClause {
  return { id: crypto.randomUUID(), section: '我的约定', text: '', kind: 'principle', scope: 'global', control: null, sources: [], origin: 'manual' }
}

/** An explicit user merge keeps their changed fields, while retaining unrelated newer work. */
export function keepCharterEdits(previous: CharterClause[], incoming: CharterClause[], edited: CharterClause[]): CharterClause[] {
  const next = new Map(cloneClauses(incoming).map(c => [c.id, c]))
  const old = new Map(previous.map(c => [c.id, c]))
  const mine = new Map(edited.map(c => [c.id, c]))
  for (const id of old.keys()) if (!mine.has(id)) next.delete(id)
  for (const clause of edited) {
    const before = old.get(clause.id)
    if (!before) { next.set(clause.id, cloneClauses([clause])[0]); continue }
    const changed = Object.keys({ ...before, ...clause }).filter(key => JSON.stringify((before as any)[key]) !== JSON.stringify((clause as any)[key]))
    if (!changed.length) continue
    const value = next.get(clause.id) ?? cloneClauses([before])[0]
    for (const key of changed) (value as any)[key] = (clause as any)[key]
    next.set(clause.id, value)
  }
  return [...next.values()]
}
