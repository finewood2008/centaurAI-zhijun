import type { DesktopSnapshot } from '../../../shared/desktop-contract'

export type ConnectionStageId = 'connect' | 'authorize' | 'workspace'
export type ConnectionStageState = 'pending' | 'active' | 'complete' | 'error' | 'blocked' | 'simulated'

export interface ConnectionStagePresentation {
  readonly id: ConnectionStageId
  readonly label: string
  readonly detail: string
  readonly state: ConnectionStageState
}

export interface ConnectionPresentation {
  readonly phase: DesktopSnapshot['phase'] | 'idle'
  readonly title: string
  readonly detail: string
  readonly deviceLabel: string
  readonly pathLabel: string | null
  readonly simulationNotice: string | null
  readonly securityNotice: string
  readonly stages: readonly ConnectionStagePresentation[]
  readonly elapsedLabel: string | null
  readonly longWaitMessage: string | null
  readonly retryMessage: string | null
  /** Stable across timer ticks so aria-live only announces an actual phase change. */
  readonly announcement: string
  readonly cancellablePhase: boolean
}

const LABELS: Readonly<Record<ConnectionStageId, string>> = {
  connect: '建立连接',
  authorize: '验证访问权限',
  workspace: '打开工作区',
}

function stage(id: ConnectionStageId, state: ConnectionStageState, detail: string): ConnectionStagePresentation {
  return { id, label: LABELS[id], state, detail }
}

function safeSeconds(value: number): number {
  return Number.isFinite(value) ? Math.max(0, Math.floor(value)) : 0
}

function deviceLabel(snapshot: DesktopSnapshot | null): string {
  const name = snapshot?.subject?.deviceName?.trim()
  return name || snapshot?.subject?.deviceId || '所选盒子'
}

function knownPath(snapshot: DesktopSnapshot | null): string | null {
  if (!snapshot || !['authorizing', 'ready'].includes(snapshot.phase)) return null
  if (snapshot.subject?.selectedPath === 'DIRECT') return '直连通道'
  if (snapshot.subject?.selectedPath === 'RELAY') return '安全中继通道'
  return null
}

function simulationStages(stages: readonly ConnectionStagePresentation[]): readonly ConnectionStagePresentation[] {
  return stages.map(item => item.state === 'complete' ? { ...item, state: 'simulated' as const } : item)
}

/** Identity of one connection attempt. Phase/sequence are deliberately excluded. */
export function connectionAttemptKey(snapshot: DesktopSnapshot | null): string {
  if (!snapshot) return ''
  return [snapshot.generation, snapshot.subject?.accountId || '', snapshot.subject?.deviceId || ''].join(':')
}

/**
 * Pure mapping from an authoritative desktop snapshot to safe user-facing copy.
 * It never infers a connection path, completion or permission from elapsed time.
 */
export function connectionPresentation(
  snapshot: DesktopSnapshot | null,
  elapsedSeconds = 0,
  canCancel = false,
): ConnectionPresentation {
  const elapsed = safeSeconds(elapsedSeconds)
  const phase = snapshot?.phase ?? 'idle'
  const simulation = snapshot?.environment === 'simulation'
  const productReady = snapshot?.phase === 'ready' && snapshot.capabilities.product === true
  const pathLabel = knownPath(snapshot)
  const cancellablePhase = phase === 'connecting' || phase === 'authorizing'
  let title = '准备连接盒子'
  let detail = '正在等待桌面连接状态。'
  let stages: readonly ConnectionStagePresentation[] = [
    stage('connect', 'pending', '等待开始连接。'),
    stage('authorize', 'pending', '连接完成后再验证访问权限。'),
    stage('workspace', 'pending', '访问验证完成后再打开工作区。'),
  ]

  if (phase === 'connecting') {
    title = `正在连接${deviceLabel(snapshot)}`
    detail = '正在获取连接凭证并协商可用通道；会优先尝试直连，必要时使用加密中继。'
    stages = [
      stage('connect', 'active', '正在获取连接凭证并协商可用通道。'),
      stage('authorize', 'pending', '连接完成后再验证访问权限。'),
      stage('workspace', 'pending', '访问验证完成后再打开工作区。'),
    ]
  } else if (phase === 'authorizing') {
    title = '正在验证访问权限'
    detail = `${pathLabel ? `${pathLabel}已建立，` : '连接通道已建立，'}正在确认当前账号可以访问所选盒子。`
    stages = [
      stage('connect', 'complete', pathLabel ? `${pathLabel}已建立。` : '连接通道已建立。'),
      stage('authorize', 'active', '正在确认当前账号的盒子访问权限。'),
      stage('workspace', 'pending', '访问验证完成后再打开工作区。'),
    ]
  } else if (phase === 'ready' && productReady) {
    title = '正在打开工作区'
    detail = '连接与访问检查已经完成，正在等待知君工作区可用。'
    stages = [
      stage('connect', 'complete', pathLabel ? `${pathLabel}已建立。` : '连接通道已建立。'),
      stage('authorize', 'complete', '当前账号的盒子访问权限已确认。'),
      stage('workspace', 'active', '正在等待知君工作区完成加载。'),
    ]
  } else if (phase === 'ready') {
    title = '工作区暂不可用'
    detail = '盒子已返回连接状态，但尚未提供知君工作区能力。请重新连接或检查盒端服务。'
    stages = [
      stage('connect', 'complete', pathLabel ? `${pathLabel}已建立。` : '连接通道已建立。'),
      stage('authorize', 'blocked', '尚不能确认完整的工作区访问能力。'),
      stage('workspace', 'blocked', '盒子尚未提供知君工作区能力。'),
    ]
  } else if (phase === 'failed') {
    title = '连接暂未就绪'
    detail = snapshot?.phase === 'failed' && snapshot.error?.message
      ? snapshot.error.message : '本次连接没有完成，你可以稍后重新连接。'
    stages = [
      stage('connect', 'blocked', '无法确认连接在哪个阶段中止。'),
      stage('authorize', 'blocked', '未确认当前访问权限。'),
      stage('workspace', 'blocked', '工作区没有打开。'),
    ]
  } else if (phase === 'disconnecting') {
    title = '正在断开连接'
    detail = '正在结束当前连接并清理本次临时状态。'
    stages = [
      stage('connect', 'blocked', '当前连接正在关闭。'),
      stage('authorize', 'blocked', '当前访问状态不再视为可用。'),
      stage('workspace', 'blocked', '工作区已停止打开。'),
    ]
  }

  if (simulation) stages = simulationStages(stages)

  const timingVisible = ['connecting', 'authorizing', 'disconnecting'].includes(phase)
    || (phase === 'ready' && productReady)
  let longWaitMessage: string | null = null
  if (elapsed >= 8) {
    if (phase === 'connecting') longWaitMessage = '建立连接所需时间比平时稍长，仍在等待盒子返回。'
    else if (phase === 'authorizing') longWaitMessage = '访问验证所需时间比平时稍长，仍在等待盒子确认。'
    else if (phase === 'ready' && productReady) longWaitMessage = '工作区打开时间比平时稍长，仍在等待可用状态。'
    else if (phase === 'disconnecting') longWaitMessage = '断开连接所需时间比平时稍长，仍在等待完成。'
  }

  const simulationNotice = simulation ? '当前为模拟流程，不代表已经建立真实安全连接。' : null
  const channelEstablished = ['authorizing', 'ready'].includes(phase) && !simulation
  const securityNotice = simulation
    ? '正式连接会通过加密通道通信；当前模拟流程不证明真实连接，在线模型的资料外发也未获授权。'
    : `${channelEstablished ? '电脑与盒子通过加密通道通信' : '电脑与盒子将通过加密通道通信'}；使用在线模型时，资料外发仍需单独授权。`
  const announcement = `${simulation ? '模拟环境。' : ''}${title}。${detail}`
  return {
    phase,
    title,
    detail,
    deviceLabel: deviceLabel(snapshot),
    pathLabel: simulation && pathLabel ? `模拟路径：${pathLabel}` : pathLabel,
    simulationNotice,
    securityNotice,
    stages,
    elapsedLabel: timingVisible ? `已等待 ${elapsed} 秒` : null,
    longWaitMessage,
    retryMessage: elapsed >= 20 && canCancel && cancellablePhase ? '你可以取消，稍后重新连接。' : null,
    announcement,
    cancellablePhase,
  }
}
