// 流里一条消息的形状：与 ConversationPage 的 UiMessage 同形（那里的接口未导出）。
import type { Message, ProvenanceEvent, TurnMetaEvent } from '@/services/api'

export interface StreamMessage extends Message {
  provenance?: (ProvenanceEvent & { fromReceipt?: boolean }) | null
  turnMeta?: TurnMetaEvent | null
  streaming?: boolean
  extractionNote?: string
  backgroundFailures?: string[]
  replySyncFailed?: boolean
  replySyncing?: boolean
}
