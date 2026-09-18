// 在场一行的措辞表（纯函数，不依赖 DOM 与网络；usePresence 负责收集输入）。
// 优先级：桌面未就绪 → 断连 → 没配模型 → 在想 → 在找资料 / 等你确认 / 在核对授权 → 在整理 N 件事 → 在听 → 认识的第 N 天 → 按小时问候。
export type PreparationStage = 'searching' | 'reviewing' | 'authorizing'
export type PresenceAction = 'reconnect' | 'prefs' | null

export interface PresenceInputs {
  workspaceReady: boolean
  connectionLabel: string
  backendDown: boolean
  modelUnavailable: boolean
  streaming: boolean
  preparationStage: PreparationStage | null
  pendingJobs: number
  composerFocused: boolean
  firstToday: boolean
  relationshipDays: number | null
  hour: number
}

export interface PresenceLine {
  text: string
  /** reconnect：后面接「重新连接」按钮；prefs：后面接「去偏好」链接。 */
  action: PresenceAction
}

export const STAGE_TEXT: Readonly<Record<PreparationStage, string>> = {
  searching: '在找资料',
  reviewing: '等你确认',
  authorizing: '在核对授权',
}

export function greetingForHour(hour: number): string {
  const h = ((Math.floor(hour) % 24) + 24) % 24
  if (h >= 5 && h < 11) return '早安'
  if (h >= 11 && h < 18) return '午后好'
  if (h >= 18 && h < 23) return '晚上好'
  return '夜深了'
}

export function presenceLine(i: PresenceInputs): PresenceLine {
  if (!i.workspaceReady) return { text: i.connectionLabel || '正在连接', action: null }
  if (i.backendDown) return { text: '暂时未连接', action: 'reconnect' }
  if (i.modelUnavailable) return { text: '还没配置模型', action: 'prefs' }
  if (i.streaming) return { text: '在想', action: null }
  if (i.preparationStage) return { text: STAGE_TEXT[i.preparationStage], action: null }
  if (i.pendingJobs > 0) return { text: `在整理 ${i.pendingJobs} 件事`, action: null }
  if (i.composerFocused) return { text: '在听', action: null }
  if (i.firstToday && i.relationshipDays && i.relationshipDays > 0) return { text: `今天是我们认识的第 ${i.relationshipDays} 天`, action: null }
  return { text: greetingForHour(i.hour), action: null }
}

/** 本地日期键（YYYY-MM-DD），用来判断「今天第一次打开」。 */
export function dayKeyOf(date: Date): string {
  const m = String(date.getMonth() + 1).padStart(2, '0')
  const d = String(date.getDate()).padStart(2, '0')
  return `${date.getFullYear()}-${m}-${d}`
}

export function isFirstOpenToday(stored: string | null | undefined, today: string): boolean {
  return stored !== today
}
