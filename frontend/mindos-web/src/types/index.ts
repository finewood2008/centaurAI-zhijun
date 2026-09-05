// MindOS 浏览器前端类型定义（P0 阶段只定义导航与健康检查所需的最小类型）
export interface HealthInfo {
  status: string
  // 仅暴露脱敏逻辑字段，不返回宿主机物理路径
  watch_folder_configured: boolean
  capabilities: {
    text_model: string
    reranker: boolean
    visual: boolean
    ocr: boolean
    hybrid_bm25: boolean
    video: boolean
    transcribe: boolean
  }
}

// 统一状态词：上传中 / 等待处理 / 处理中 / 已完成 / 失败 / 已删除
export type MaterialStatus = 'uploaded' | 'queued' | 'processing' | 'available' | 'failed' | 'deleted'

export interface RawMaterial {
  id: string
  name: string
  type: string
  status: MaterialStatus
  folder?: string
  created_at: string
}
