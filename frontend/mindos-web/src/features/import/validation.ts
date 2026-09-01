// MindOS 导入校验规则（P1 / P13 开放音频 / 本期开放 Excel 与 PPT）：与后端 backend/mindos/validation.py 保持一致，
// 避免仅依赖浏览器限制（后端在真实上传时同样强制校验）。

export type ImportStatus = 'ok' | 'oversize' | 'unsupported' | 'audio_pending'
export type ImportCategory = 'document' | 'image' | 'audio' | 'unknown'

export const DOC_EXTENSIONS = ['.pdf', '.docx', '.txt', '.md', '.xlsx', '.xlsm', '.xls', '.pptx']
export const IMAGE_EXTENSIONS = ['.png', '.jpg', '.jpeg']
export const AUDIO_EXTENSIONS = ['.mp3', '.wav', '.m4a']
export const DOC_IMAGE_MAX_BYTES = 50 * 1024 * 1024 // 文档/图片 50MB
export const AUDIO_MAX_BYTES = 200 * 1024 * 1024 // 音频 200MB

export interface PendingFile {
  id: string
  name: string
  ext: string
  category: ImportCategory
  sizeBytes: number
  status: ImportStatus
  message: string
}

export function extOf(filename: string): string {
  const i = filename.lastIndexOf('.')
  return i >= 0 ? filename.slice(i).toLowerCase() : ''
}

export function categoryOf(ext: string): ImportCategory {
  if (DOC_EXTENSIONS.includes(ext)) return 'document'
  if (IMAGE_EXTENSIONS.includes(ext)) return 'image'
  if (AUDIO_EXTENSIONS.includes(ext)) return 'audio'
  return 'unknown'
}

export interface ImportValidation {
  status: ImportStatus
  category: ImportCategory
  message: string
}

export function validateImport(filename: string, sizeBytes: number): ImportValidation {
  const ext = extOf(filename)
  const category = categoryOf(ext)

  if (category === 'unknown') {
    return { status: 'unsupported', category, message: '不支持的文件类型' }
  }
  const maxBytes = category === 'audio' ? AUDIO_MAX_BYTES : DOC_IMAGE_MAX_BYTES
  if (sizeBytes > maxBytes) {
    const limitLabel = category === 'audio' ? '200MB' : '50MB'
    return { status: 'oversize', category, message: `文件超过 ${limitLabel} 限制` }
  }
  return { status: 'ok', category, message: '待上传' }
}

export const CATEGORY_LABELS: Record<ImportCategory, string> = {
  document: '文档',
  image: '图片',
  audio: '音频',
  unknown: '未知',
}
