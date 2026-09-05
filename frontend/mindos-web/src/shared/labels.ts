// 助手正文的认识论标签 → 文字徽章（纯字符串处理，可在 node 下测试）。
//
// 输入是 markdown-it（html:false）渲染后的 HTML：模型输出里的 `<` 已被转义，
// 因此这里只会注入我们自己的 span / a，不会把用户或模型的原始 HTML 放行。
// 四种标记与 docs/development/zhijun-api-contract.md §1 一致。
export const LAYER_MARKERS: ReadonlyArray<{ marker: string; kind: 'told' | 'material' | 'guess' | 'view'; text: string }> = [
  { marker: '【你告诉我的】', kind: 'told', text: '你告诉我的' },
  { marker: '【资料里看到的】', kind: 'material', text: '资料里看到的' },
  { marker: '【我推测的】', kind: 'guess', text: '我推测的' },
  { marker: '【知君的看法】', kind: 'view', text: '知君的看法' },
]

const CITE_RE = /\[m([1-9])\]/g
const CONTEXT_CITE_RE = /[ \t\u00a0]*(?:\[p\d+\])+/g

/**
 * ContextPlan 的 pN 是逐轮临时审计编号，不是给用户阅读的脚注。
 * 只在显示边界隐藏；服务端仍保留原文，以便核验本轮实际引用。
 */
export function stripContextCitations(text: string): string {
  return text.replace(CONTEXT_CITE_RE, '')
}

export function decorateLabels(html: string): string {
  let out = html
  for (const { marker, kind, text } of LAYER_MARKERS) {
    out = out.split(marker).join(`<span class="layer-badge layer-badge--${kind}">${text}</span>`)
  }
  out = out.replace(CITE_RE, (_m, n: string) => `<a class="cite-chip" data-cite="${n}" href="#cite-${n}">m${n}</a>`)
  return out
}

/** 去掉正文里的标记，用于会话列表/摘要等纯文本场景。 */
export function stripLabels(text: string): string {
  let out = text
  for (const { marker } of LAYER_MARKERS) out = out.split(marker).join('')
  return stripContextCitations(out.replace(CITE_RE, ''))
}

/** 提醒种类的中文名（对话页顶部提醒条上的小印）。 */
export const NUDGE_KIND_LABELS: Readonly<Record<string, string>> = {
  review_due: '判断回访',
  commitment_due: '承诺回访',
  principle_tension: '原则与做法',
  weekly_review: '每周回顾',
  checkin: '打个招呼',
}

export function nudgeKindLabel(kind: string): string {
  return NUDGE_KIND_LABELS[kind] ?? '提醒'
}

/** 会话模式的方印文字：建档 / 商量 / 回访 / 对话。chat 会话若已确认过判断，也当作「商量」。 */
export function conversationModeLabel(mode: string | null | undefined, hasDecision = false): string {
  if (mode === 'onboarding') return '建档'
  if (mode === 'review') return '回访'
  if (mode === 'deliberate' || hasDecision) return '商量'
  return '对话'
}

/** 建档会话：七个问题 + 一次收尾，用户共发 8 轮才算聊完。 */
export const ONBOARDING_TOTAL_TURNS = 8

/** 由消息总数估计用户已发的轮数（用户 / 知君交替；系统备注很少，忽略）。 */
export function onboardingUserTurns(messageCount: number | null | undefined): number {
  const n = Math.max(0, messageCount ?? 0)
  return Math.ceil(n / 2)
}

/** 会话列表下面那行灰字：「3 条理解 · 1 待你点头 · 1 个判断」；全零返回空串。 */
export function outcomesLine(o: { confirmed?: number; working?: number; decision?: boolean; commitments?: number } | null | undefined): string {
  if (!o) return ''
  const parts: string[] = []
  if ((o.confirmed ?? 0) > 0) parts.push(`${o.confirmed} 条理解`)
  if ((o.working ?? 0) > 0) parts.push(`${o.working} 待你点头`)
  if (o.decision) parts.push('1 个判断')
  if ((o.commitments ?? 0) > 0) parts.push(`${o.commitments} 个承诺`)
  return parts.join(' · ')
}

/** 抽取被跳过时，知君回复下方那行灰字；其它原因不显示。 */
export const EXTRACTION_SKIP_NOTES: Readonly<Record<string, string>> = {
  too_short: '这句比较短，没有当作关于你的事记下',
  pure_question: '这是在问知君，没有记成关于你的事',
  disabled: '整理已暂停',
}

export function extractionSkipNote(reason: string | null | undefined): string {
  return reason ? (EXTRACTION_SKIP_NOTES[reason] ?? '') : ''
}

/** 候选轮询超时后的那行灰字。 */
export const EXTRACTION_STILL_WORKING = '知君还在整理，整理好会出现在「知君最近学到的」'

/** 从提醒文案里取出「……」引住的片段，用来拼进输入框的话头。 */
export function quotedParts(text: string): string[] {
  const out: string[] = []
  const re = /「([^「」]+)」/g
  let m: RegExpExecArray | null
  while ((m = re.exec(text)) !== null) out.push(m[1])
  return out
}

/** 出处摘要里那句「参考了你 8月2日 记下的「标题」（当时选了「…」）」；day 为空则不写日期。 */
export function pastDecisionSummary(d: { title: string; choice: string }, day = '', extra = 0): string {
  const when = day ? `你 ${day} 记下的` : '你记下的'
  const chosen = d.choice ? `（当时选了「${d.choice}」）` : ''
  const more = extra > 0 ? `，还有 ${extra} 个` : ''
  return `参考了${when}「${d.title}」${chosen}${more}`
}

/** 出处展开区里那行「和你的原则「…」有关」；找不到内容时只写数量。 */
export function anchorLine(contents: string[], total: number): string {
  if (!total) return ''
  const first = contents[0]
  if (!first) return `和你的 ${total} 条原则有关`
  return total > 1 ? `和你的原则「${first}」等 ${total} 条有关` : `和你的原则「${first}」有关`
}

/** 页头标题下那行灰字：只列非零项，各自链到该去的页面（没有链接的项 to 为空）。 */
export interface HeaderAggregateItem {
  key: 'inbox' | 'review' | 'reflect' | 'jobs'
  text: string
  to: string
}

export function headerAggregateItems(input: {
  inbox?: number | null
  dueReview?: number | null
  overdue?: number | null
  pendingReviews?: number | null
  pendingJobs?: number | null
}): HeaderAggregateItem[] {
  const out: HeaderAggregateItem[] = []
  const inbox = input.inbox ?? 0
  const overdue = input.overdue ?? 0
  const due = Math.max(input.dueReview ?? 0, overdue)
  const reflect = input.pendingReviews ?? 0
  const jobs = input.pendingJobs ?? 0
  if (inbox > 0) out.push({ key: 'inbox', text: `待确认 ${inbox}`, to: '/me/inbox' })
  if (due > 0) out.push({ key: 'review', text: overdue > 0 ? `待回访 ${due}（逾期 ${overdue}）` : `待回访 ${due}`, to: '/judgments' })
  if (reflect > 0) out.push({ key: 'reflect', text: `待复盘 ${reflect}`, to: '/judgments' })
  if (jobs > 0) out.push({ key: 'jobs', text: `还在整理 ${jobs} 件`, to: '' })
  return out
}

/** 「这段对话留下的」小卡有没有东西可显示（全零不显示）。 */
export function hasConversationOutcomes(o: {
  confirmedClaims?: unknown[] | null
  workingClaims?: unknown[] | null
  decision?: unknown | null
  commitments?: unknown[] | null
  pendingJobs?: number | null
} | null | undefined): boolean {
  if (!o) return false
  return (
    (o.confirmedClaims?.length ?? 0) > 0 ||
    (o.workingClaims?.length ?? 0) > 0 ||
    !!o.decision ||
    (o.commitments?.length ?? 0) > 0 ||
    (o.pendingJobs ?? 0) > 0
  )
}

/** 空白态「下一步」：最多三条，措辞不催。点击分别开回访会话 / 跳判断页 / 放进输入框 / 跳待确认。 */
export interface NextStep {
  key: string
  kind: 'review' | 'reflect' | 'commitment' | 'inbox'
  text: string
  decisionId?: string
  claimId?: string
  say?: string
}

export function buildNextSteps(
  input: {
    overdue?: { id: string; title: string }[]
    pendingReviews?: { id: string; title: string }[]
    dueCommitments?: { id: string; content: string }[]
    inbox?: number | null
  },
  max = 3,
): NextStep[] {
  const out: NextStep[] = []
  for (const d of input.overdue ?? []) out.push({ key: `review-${d.id}`, kind: 'review', text: `「${d.title}」到了回访的时候`, decisionId: d.id })
  for (const d of input.pendingReviews ?? []) out.push({ key: `reflect-${d.id}`, kind: 'reflect', text: `「${d.title}」结果已经记下，有空可以复盘`, decisionId: d.id })
  for (const c of input.dueCommitments ?? []) {
    out.push({ key: `commit-${c.id}`, kind: 'commitment', text: `承诺「${c.content}」到期了，说说进展`, claimId: c.id, say: `关于「${c.content}」，说说进展：` })
  }
  const inbox = input.inbox ?? 0
  if (inbox > 0) out.push({ key: 'inbox', kind: 'inbox', text: `${inbox} 条理解等你点头` })
  return out.slice(0, Math.max(0, max))
}

/** 「到期」：validTo 不晚于今天结束。 */
export function isDueByToday(iso: string | null | undefined, now: Date = new Date()): boolean {
  if (!iso) return false
  const t = new Date(iso).valueOf()
  if (Number.isNaN(t)) return false
  const end = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 23, 59, 59, 999).valueOf()
  return t <= end
}

// ---- 今日首屏（纯函数，可在 node 下测试）

const WEEKDAYS = ['周日', '周一', '周二', '周三', '周四', '周五', '周六']

/** 问候行：「阿远，9月3日 周三。」；没有称呼就只写日期。 */
export function greetingLine(name: string | null | undefined, now: Date = new Date()): string {
  const date = `${now.getMonth() + 1}月${now.getDate()}日 ${WEEKDAYS[now.getDay()]}`
  const who = (name ?? '').trim()
  return who ? `${who}，${date}。` : `${date}。`
}

/** 问候下面那行灰字：只列非零项；全零写「今天没有要催你的事。」 */
export function todaySummaryLine(input: {
  dueReview?: number | null
  inbox?: number | null
  dueCommitments?: number | null
  pendingReviews?: number | null
  nudges?: number | null
}): string {
  const parts: string[] = []
  const due = input.dueReview ?? 0
  const inbox = input.inbox ?? 0
  const commitments = input.dueCommitments ?? 0
  const reviews = input.pendingReviews ?? 0
  const nudges = input.nudges ?? 0
  if (due > 0) parts.push(`有 ${due} 件事到了回访的时候`)
  if (commitments > 0) parts.push(`${commitments} 个承诺到期了`)
  if (reviews > 0) parts.push(`${reviews} 个判断记了结果，可以复盘`)
  if (inbox > 0) parts.push(`${inbox} 条理解等你点头`)
  // 提醒（张力 / 每周回顾）不属于以上任何一类时，也不能说「没有要催你的事」。
  if (!parts.length && nudges > 0) parts.push(`有 ${nudges} 件事想和你聊聊`)
  return parts.length ? parts.join(' · ') : '今天没有要催你的事。'
}

const NICKNAME_RE = /(?:偏好被称呼为|希望被称呼为|被称呼为|称呼为|叫我|称呼我|喊我|我叫|名叫|自称)\s*「?([一-龥A-Za-z·]{1,8}?)」?(?:就行|就好|吧|即可|，|,|。|；|;|\s|$)/

/** 从「我是谁」分区里已确认的理解中找称呼（「叫我阿远」「偏好被称呼为阿远」）；找不到返回空串。 */
export function nicknameFromClaims(
  claims: ReadonlyArray<{ section: string; content: string; trustState?: string; predicate?: string }> | null | undefined,
): string {
  if (!claims) return ''
  const who = claims.filter((c) => c.section === 'who' && (c.trustState ?? 'confirmed') === 'confirmed')
  for (const c of who) {
    const m = NICKNAME_RE.exec(c.content || '')
    if (m && m[1]) return m[1]
  }
  return ''
}

/** 「最近留下的」：有产出（全零不算）的会话，按最近活动排序，最多 max 段。 */
export function recentOutcomeConversations<T extends { lastMessageAt?: string | null; updatedAt: string; outcomes?: { confirmed?: number; working?: number; decision?: boolean; commitments?: number } | null }>(
  items: ReadonlyArray<T>,
  max = 3,
): T[] {
  const at = (c: T) => new Date(c.lastMessageAt || c.updatedAt).valueOf() || 0
  return items
    .filter((c) => outcomesLine(c.outcomes) !== '')
    .slice()
    .sort((a, b) => at(b) - at(a))
    .slice(0, Math.max(0, max))
}

/** 相对时间：刚刚 / N 分钟前 / N 小时前 / 昨天 / N 天前 / M月D日（跨年带年份）。 */
export function relativeTime(iso: string | null | undefined, now: Date = new Date()): string {
  if (!iso) return ''
  const d = new Date(iso)
  if (Number.isNaN(d.valueOf())) return ''
  const diff = now.valueOf() - d.valueOf()
  if (diff < 60_000) return '刚刚'
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`
  const startOf = (x: Date) => new Date(x.getFullYear(), x.getMonth(), x.getDate()).getTime()
  const days = Math.round((startOf(now) - startOf(d)) / 86_400_000)
  if (days <= 0) return `${Math.floor(diff / 3_600_000)} 小时前`
  if (days === 1) return '昨天'
  if (days < 30) return `${days} 天前`
  const md = `${d.getMonth() + 1}月${d.getDate()}日`
  return d.getFullYear() === now.getFullYear() ? md : `${d.getFullYear()}年${md}`
}
