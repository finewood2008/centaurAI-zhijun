// 本体分区 / 自我层 / 信任状态的统一文案（与后端枚举一致，页面不得另行定义）。
import type { Layer, ReviewAction, Section, TrustState } from '@/services/api'
import type { StatusMeta } from '@/shared/status'

export const SECTIONS: ReadonlyArray<{ key: Section; label: string; hint: string }> = [
  { key: 'who', label: '我是谁', hint: '身份、角色、背景' },
  { key: 'people', label: '我的人', hint: '重要的人与关系' },
  { key: 'matters', label: '我的事', hint: '在做的事、承诺、经历' },
  { key: 'principles', label: '我的原则', hint: '原则与边界' },
  { key: 'ways', label: '我的做法', hint: '偏好、习惯、判断方式' },
  { key: 'direction', label: '我的方向', hint: '目标、想成为的样子' },
]

export function sectionLabel(section: string): string {
  return SECTIONS.find((s) => s.key === section)?.label ?? section
}

export const LAYER_META: Record<Layer, StatusMeta> = {
  self_declared: { label: '你告诉我的', tone: 'ink' },
  observed: { label: '资料里看到的', tone: 'success' },
  hypothesis: { label: '我推测的', tone: 'guess' },
  aspirational: { label: '你想成为的', tone: 'accent' },
}

export function layerMeta(layer: string): StatusMeta {
  return LAYER_META[layer as Layer] ?? { label: layer, tone: 'neutral' }
}

export const TRUST_META: Record<TrustState, StatusMeta> = {
  working: { label: '待确认', tone: 'warning' },
  confirmed: { label: '已确认', tone: 'muted' },
  retracted: { label: '已撤回', tone: 'neutral' },
  superseded: { label: '已被替代', tone: 'neutral' },
}

export function trustMeta(state: string): StatusMeta {
  return TRUST_META[state as TrustState] ?? { label: state, tone: 'neutral' }
}

export const REVIEW_ACTIONS_WORKING: ReadonlyArray<{ action: ReviewAction; label: string }> = [
  { action: 'confirm', label: '对' },
  { action: 'partial', label: '部分对' },
  { action: 'context_only', label: '只适用于这件事' },
  { action: 'reject', label: '不对' },
  { action: 'defer', label: '先别存' },
]

/** 用户动作 → 会话内系统备注文案（后端同样会追加一条，这里只做即时反馈）。 */
export function reviewNote(action: ReviewAction, content: string): string {
  switch (action) {
    case 'confirm':
      return `你确认了：${content}`
    case 'partial':
      return `你修正为：${content}`
    case 'context_only':
      return `你限定为只适用于这件事：${content}`
    case 'reject':
      return `你否定了：${content}`
    case 'defer':
      return `先不保存：${content}`
    case 'retract':
      return `你撤回了：${content}`
    case 'reaffirm':
      return `你重申了：${content}`
    default:
      return content
  }
}

/** 谓词 → 中文（用户永远不看到英文谓词）。 */
export const PREDICATE_LABEL: Record<string, string> = {
  is: '是', has_trait: '特点', background: '背景', role: '角色',
  knows: '认识', works_with: '合作', relationship: '关系', attitude_toward: '态度',
  working_on: '在做', committed_to: '承诺', happened: '经历', owns: '拥有',
  holds_principle: '原则', boundary: '边界',
  prefers: '偏好', tends_to: '倾向', decides_by: '判断方式',
  wants_to: '想要', goal: '目标', avoids: '避免',
}

export function predicateLabel(predicate: string | null | undefined): string {
  if (!predicate) return ''
  return PREDICATE_LABEL[predicate] ?? predicate
}

/** 今天 / 昨天 / N 天前 / M月D日（跨年带年份）。 */
export function formatDay(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.valueOf())) return ''
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((startOf(now) - startOf(d)) / 86400000)
  if (days === 0) return '今天'
  if (days === 1) return '昨天'
  if (days > 1 && days < 30) return `${days} 天前`
  const md = `${d.getMonth() + 1}月${d.getDate()}日`
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}年${md}`
}

/** 来源一句话：「你 9月2日 在对话里说的」/「资料里看到的」/「你 昨天 手写的」。 */
export function sourceLine(claim: { evidence: Array<{ kind: string; createdAt: string; materialId?: string | null }>; firstSeen: string; layer: string }): string {
  const ev = claim.evidence[0]
  const day = formatDay(ev?.createdAt || claim.firstSeen)
  if (!ev) return day ? `${day} 记下的` : ''
  if (ev.kind === 'material_span') return `${day} 从资料里看到的`
  if (ev.kind === 'user_edit') return `你 ${day} 手写的`
  if (ev.kind === 'review' || ev.kind === 'decision') return `你 ${day} 复盘时写下的`
  return claim.layer === 'hypothesis' ? `知君 ${day} 从对话里推测的` : `你 ${day} 在对话里说的`
}

/** 五个动作里默认只露两个；其余进「···」并带一句解释。 */
export const REVIEW_PRIMARY: ReadonlyArray<{ action: ReviewAction; label: string }> = [
  { action: 'confirm', label: '对' },
  { action: 'reject', label: '不对' },
]
export const REVIEW_MORE: ReadonlyArray<{ action: ReviewAction; label: string; hint: string }> = [
  { action: 'partial', label: '部分对', hint: '改几个字再记' },
  { action: 'context_only', label: '只适用于这件事', hint: '记住，但不当成长期的你' },
  { action: 'defer', label: '先别存', hint: '14 天后再问我' },
]
