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
  self_declared: { label: '你告诉我的', tone: 'success' },
  observed: { label: '资料里看到的', tone: 'info' },
  hypothesis: { label: '我推测的', tone: 'warning' },
  aspirational: { label: '你想成为的', tone: 'purple' },
}

export function layerMeta(layer: string): StatusMeta {
  return LAYER_META[layer as Layer] ?? { label: layer, tone: 'neutral' }
}

export const TRUST_META: Record<TrustState, StatusMeta> = {
  working: { label: '待确认', tone: 'warning' },
  confirmed: { label: '已确认', tone: 'success' },
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
