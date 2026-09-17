import type { Reflection, ReflectionFeedback } from '../services/api'

export const reflectionLabels: Record<Reflection['status'], string> = {
  candidate: '想和你核对', surfaced: '想和你核对', accepted: '你认可的理解',
  contextual: '有适用情境', rejected: '你不同意的观察', observing: '再观察看看', retired: '已撤回',
}
export const reflectionResponses: Record<ReflectionFeedback, string> = {
  accepted: '记下了。以后相关时，我会带着这些依据和保留来理解你。',
  contextual: '你的补充已保存。以后相关时，我会保留这个条件，不把它泛化。',
  rejected: '收到。我会停止沿用这个观察，也不会换个说法反复提出。',
  observing: '先留在这里，不把它当作对你的确定理解。',
  retired: '已撤回，后续对话不再使用这条照见。',
}
export const reflectionFilters = [
  { id: 'recent', label: '最近发现' }, { id: 'observing', label: '正在观察' },
  { id: 'accepted', label: '我认可的' }, { id: 'rejected', label: '我不同意的' },
] as const
export function filterReflections(items: Reflection[], filter: string): Reflection[] {
  return items.filter(item => item.status !== 'retired' && (filter === 'recent'
    ? ['candidate', 'surfaced'].includes(item.status)
    : filter === 'accepted' ? ['accepted', 'contextual'].includes(item.status) : item.status === filter))
}
