// 统一日期、文件大小、文件类型、百分比格式化（B0：FE-UI-002）
// 各页面不得再自行定义 formatDate / formatSize / formatFileType。

/** 日期格式化为本地时间字符串；空值或非法输入返回 fallback。 */
export function formatDate(
  value: string | number | Date | null | undefined,
  fallback = '—',
): string {
  if (value === null || value === undefined || value === '') return fallback
  const d = new Date(value)
  return Number.isNaN(d.valueOf()) ? fallback : d.toLocaleString('zh-CN', { hour12: false })
}

/** 文件大小（字节）格式化为可读文本。 */
export function formatFileSize(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined) return '—'
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / 1024 / 1024).toFixed(1)} MB`
  return `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`
}

/** 文件类型英文标识 → 中文标签。 */
export function formatFileType(type: string | null | undefined): string {
  if (type === 'image') return '图片'
  if (type === 'document') return '文档'
  if (type === 'audio') return '音频'
  return type || '—'
}

/** 0~1 比例格式化为百分比；空值返回 —。 */
export function formatPercent(value: number | null | undefined): string {
  if (value === null || value === undefined) return '—'
  return `${Math.round(value * 100)}%`
}
