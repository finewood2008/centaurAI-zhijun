// 统一状态文案与语义色映射（B0：FE-UI-002）
// 各页面不得再自行定义 statusText / statusLabel / statusClass / STATUS_META。

/** 语义色类型（供 StatusBadge 等组件消费） */
export type BadgeTone = 'neutral' | 'info' | 'success' | 'warning' | 'danger' | 'purple' | 'ink' | 'guess' | 'accent' | 'muted'

export interface StatusMeta {
  label: string
  tone: BadgeTone
}

// ---- 原材料 / 上传状态（与后端 MaterialStatus 一致）----
export const MATERIAL_STATUS_META: Record<string, StatusMeta> = {
  uploaded: { label: '上传中', tone: 'info' },
  queued: { label: '等待处理', tone: 'info' },
  processing: { label: '处理中', tone: 'warning' },
  available: { label: '已完成', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
  recycled: { label: '已回收', tone: 'neutral' },
  restoring: { label: '恢复中', tone: 'info' },
  purging: { label: '删除中', tone: 'warning' },
  deleted: { label: '已删除', tone: 'neutral' },
}

export function materialStatusMeta(status: string): StatusMeta {
  return MATERIAL_STATUS_META[status] ?? { label: status, tone: 'neutral' }
}

export function materialStatusLabel(status: string): string {
  return materialStatusMeta(status).label
}

/** Scan completion describes detection, not permission to send original text. */
export function materialSensitiveScanStatusMeta(scan: {
  state: string; completedFields?: number; totalFields?: number
  errorCode?: string | null
} | null | undefined): StatusMeta | null {
  if (!scan) return null
  const progress = Number.isInteger(scan.totalFields) && Number(scan.totalFields) > 0
    && Number.isInteger(scan.completedFields) && Number(scan.completedFields) >= 0
    && Number(scan.completedFields) <= Number(scan.totalFields)
    ? ` ${scan.completedFields}/${scan.totalFields}` : ''
  const states: Record<string, StatusMeta> = {
    queued: { label: scan.errorCode ? `敏感识别等待重试${progress}` : '等待敏感识别', tone: scan.errorCode ? 'warning' : 'info' },
    processing: { label: `敏感识别中${progress}`, tone: 'warning' },
    completed: { label: '敏感识别已完成', tone: 'success' },
    failed: { label: '敏感识别失败', tone: 'danger' },
    canceled: { label: '敏感识别已取消', tone: 'neutral' },
  }
  return states[scan.state] ?? { label: '敏感识别状态未知', tone: 'neutral' }
}

// ---- 导入队列状态（页面级：待上传/上传中/已上传/处理中/可用/失败）----
export const QUEUE_STATUS_META: Record<string, StatusMeta> = {
  idle: { label: '待上传', tone: 'info' },
  uploading: { label: '上传中', tone: 'warning' },
  uploaded: { label: '已上传', tone: 'info' },
  processing: { label: '处理中', tone: 'warning' },
  available: { label: '可用', tone: 'success' },
  failed: { label: '失败', tone: 'danger' },
}

export function queueStatusMeta(state: string): StatusMeta {
  return QUEUE_STATUS_META[state] ?? { label: state, tone: 'neutral' }
}

// ---- 导入校验状态（ok / oversize / unsupported / audio_pending）----
export const IMPORT_VALIDATION_META: Record<string, StatusMeta> = {
  ok: { label: '待上传', tone: 'info' },
  oversize: { label: '超过大小限制', tone: 'danger' },
  unsupported: { label: '不支持的文件类型', tone: 'danger' },
  audio_pending: { label: '音频待开放', tone: 'purple' },
}

export function importValidationMeta(status: string): StatusMeta {
  return IMPORT_VALIDATION_META[status] ?? { label: status, tone: 'neutral' }
}

// ---- 治理状态 ----
export const GOVERNANCE_STATUS_META: Record<string, StatusMeta> = {
  pending: { label: '待处理', tone: 'info' },
  processing: { label: '处理中', tone: 'warning' },
  ignored: { label: '已忽略', tone: 'neutral' },
  merged: { label: '已合并', tone: 'success' },
  archived: { label: '已归档', tone: 'purple' },
}

export function governanceStatusMeta(status: string): StatusMeta {
  return GOVERNANCE_STATUS_META[status] ?? { label: status, tone: 'neutral' }
}

export function governanceStatusLabel(status: string): string {
  return governanceStatusMeta(status).label
}

// ---- 治理类型 ----
export const GOVERNANCE_KIND_META: Record<string, StatusMeta> = {
  duplicate: { label: '疑似重复', tone: 'warning' },
  outdated: { label: '可能过时', tone: 'warning' },
  relation: { label: '待确认关联', tone: 'info' },
  conflict: { label: '观点冲突', tone: 'danger' },
}

export function governanceKindMeta(kind: string): StatusMeta {
  return GOVERNANCE_KIND_META[kind] ?? { label: kind, tone: 'neutral' }
}
