// 一条按天分节的长流：纯函数，不碰网络与路由，单测直接 import。
// 服务端列表是「活跃置顶 + 最近活动」的顺序，这里按创建日的本地日期重新分节，
// 日期从旧到新（今天最后），同一天内按创建时间从早到晚。
import type { Conversation } from '../services/api'

export interface DayGroup {
  /** 本地日期键 YYYY-MM-DD */
  key: string
  /** 当天 00:00（本地） */
  date: Date
  label: string
  isToday: boolean
  items: Conversation[]
}

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']
const DAY_MS = 86_400_000

function parseTime(iso: string | null | undefined): number {
  if (!iso) return Number.NaN
  const t = new Date(iso).valueOf()
  return Number.isNaN(t) ? Number.NaN : t
}

function startOfDay(d: Date): Date {
  return new Date(d.getFullYear(), d.getMonth(), d.getDate())
}

export function dayKey(d: Date): string {
  const m = String(d.getMonth() + 1).padStart(2, '0')
  const day = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${m}-${day}`
}

/** 会话最近一次活动的时间（毫秒）：最后一条消息 → 更新 → 创建。 */
export function activityAt(conv: Pick<Conversation, 'lastMessageAt' | 'updatedAt' | 'createdAt'>): number {
  const candidates = [conv.lastMessageAt, conv.updatedAt, conv.createdAt].map(parseTime).filter(t => !Number.isNaN(t))
  return candidates.length ? Math.max(...candidates) : 0
}

/** 会话所属的本地日：按 createdAt；无法解析或在未来时算作今天。 */
export function dayOf(conv: Pick<Conversation, 'createdAt'>, now: Date): Date {
  const t = parseTime(conv.createdAt)
  if (Number.isNaN(t) || t > now.valueOf()) return startOfDay(now)
  return startOfDay(new Date(t))
}

function daysBetween(day: Date, today: Date): number {
  return Math.round((today.valueOf() - day.valueOf()) / DAY_MS)
}

/** 「今天 · 9月18日 周四」「昨天 · 9月17日 周三」「9月11日 · 周四」；跨年时带年份。 */
export function dayLabel(date: Date, now: Date = new Date()): string {
  const day = startOfDay(date)
  const today = startOfDay(now)
  const md = `${day.getMonth() + 1}月${day.getDate()}日`
  const withYear = day.getFullYear() === today.getFullYear() ? md : `${day.getFullYear()}年${md}`
  const weekday = WEEKDAYS[day.getDay()]
  const distance = daysBetween(day, today)
  if (distance === 0) return `今天 · ${withYear} ${weekday}`
  if (distance === 1) return `昨天 · ${withYear} ${weekday}`
  return `${withYear} · ${weekday}`
}

/** 按 id 去重：后来的覆盖先前的（列表刷新后的产出摘要更新）。 */
export function mergePage(existing: Conversation[], page: Conversation[]): Conversation[] {
  const map = new Map<string, Conversation>()
  for (const item of existing) map.set(item.id, item)
  for (const item of page) map.set(item.id, item)
  return [...map.values()]
}

function makeGroup(date: Date, now: Date): DayGroup {
  const day = startOfDay(date)
  return { key: dayKey(day), date: day, label: dayLabel(day, now), isToday: daysBetween(day, startOfDay(now)) === 0, items: [] }
}

/** 按本地创建日分节：去重、旧日在前今天最后、同日内按创建时间从早到晚。 */
export function groupByDay(items: Conversation[], now: Date = new Date()): DayGroup[] {
  const groups = new Map<string, DayGroup>()
  for (const conv of mergePage([], items)) {
    const day = dayOf(conv, now)
    const key = dayKey(day)
    let group = groups.get(key)
    if (!group) {
      group = makeGroup(day, now)
      groups.set(key, group)
    }
    group.items.push(conv)
  }
  const result = [...groups.values()].sort((a, b) => a.date.valueOf() - b.date.valueOf())
  for (const group of result) {
    group.items.sort((a, b) => {
      const diff = parseTime(a.createdAt) - parseTime(b.createdAt)
      return Number.isNaN(diff) || diff === 0 ? a.id.localeCompare(b.id) : diff
    })
  }
  return result
}

/** 保证某一天有一节（即使当天没有历史块）：今天的信、当前块都需要落在自己的那一天。 */
export function withDay(groups: DayGroup[], date: Date, now: Date = new Date()): DayGroup[] {
  const key = dayKey(startOfDay(date))
  if (groups.some(g => g.key === key)) return groups
  return [...groups, makeGroup(date, now)].sort((a, b) => a.date.valueOf() - b.date.valueOf())
}

/**
 * 当前块：路由优先；否则今天创建的 mode=chat 且 active 的会话里最近活动的一条；都没有则 null。
 * 路由指向的会话不在列表里时也返回 null（由对话页自己加载）。
 */
export function pickActiveConversation(items: Conversation[], now: Date, routeId: string | null | undefined): Conversation | null {
  if (routeId) return items.find(item => item.id === routeId) ?? null
  const todayKey = dayKey(startOfDay(now))
  let best: Conversation | null = null
  for (const conv of items) {
    if (conv.mode !== 'chat' || conv.status !== 'active') continue
    if (dayKey(dayOf(conv, now)) !== todayKey) continue
    if (!best || activityAt(conv) > activityAt(best)) best = conv
  }
  return best
}

/** 历史块占位上的一行：「记下 2 条 · 1 个判断 · 1 个承诺」；全零为空串。 */
export function outcomesLine(outcomes: Conversation['outcomes']): string {
  if (!outcomes) return ''
  const parts: string[] = []
  const recorded = (outcomes.confirmed ?? 0) + (outcomes.working ?? 0)
  if (recorded > 0) parts.push(`记下 ${recorded} 条`)
  if (outcomes.decision) parts.push('1 个判断')
  if ((outcomes.commitments ?? 0) > 0) parts.push(`${outcomes.commitments} 个承诺`)
  return parts.join(' · ')
}

export function hasOutcomesBrief(outcomes: Conversation['outcomes']): boolean {
  return !!outcomes && ((outcomes.confirmed ?? 0) > 0 || (outcomes.working ?? 0) > 0 || !!outcomes.decision || (outcomes.commitments ?? 0) > 0)
}

/** 「14:05」 */
export function clockLabel(iso: string | null | undefined): string {
  const t = parseTime(iso)
  if (Number.isNaN(t)) return ''
  const d = new Date(t)
  return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
}
