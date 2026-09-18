// 当前轮桥接：嵌入式 ConversationPage 挂载时把自己的响应式状态与动作放进模块级 shallowRef，
// 沉浸壳的其它部件（在场行、案头、草稿卡）只读这里，不各自再起一份对话逻辑。
// 模式同 services/taskRouting.ts 的 routeQuestion / conversationSetup。
import { shallowRef, type Ref, type ShallowRef, type UnwrapNestedRefs } from 'vue'
import type {
  Conversation,
  ConversationMemoryAttention,
  ConversationOutcomes,
  DecisionDraft,
  DecisionDraftConfirmPayload,
  GrowthDecision,
  TurnMode,
  ZhijunStatus,
} from '@/services/api'
import type { MemoryPlacement } from '@/shared/memoryAttention'
import type { useChatImports } from '@/composables/useChatImports'

export type ChatImportsModel = UnwrapNestedRefs<ReturnType<typeof useChatImports>>
export type WorkspaceTab = 'draft' | 'map' | 'review' | 'memory'

/** 字段都是对话页自己的 ref / computed，读到的永远是当前值；动作直接转发到对话页。 */
export interface ActiveTurnBridge {
  readonly owner: symbol
  readonly conversationId: Readonly<Ref<string | null>>
  readonly current: Readonly<Ref<Conversation | null>>
  readonly streaming: Readonly<Ref<boolean>>
  readonly status: Readonly<Ref<ZhijunStatus | null>>
  readonly draft: Readonly<Ref<DecisionDraft | null>>
  readonly draftPending: Readonly<Ref<boolean>>
  readonly draftTimedOut: Readonly<Ref<boolean>>
  readonly draftChanged: Readonly<Ref<string[]>>
  readonly draftBusy: Readonly<Ref<boolean>>
  readonly draftError: Readonly<Ref<string>>
  readonly decision: Readonly<Ref<GrowthDecision | null>>
  readonly memoryAttention: Readonly<Ref<ConversationMemoryAttention | null>>
  readonly memoryPlacement: Readonly<Ref<MemoryPlacement | null>>
  readonly turnOutcomes: Readonly<Ref<ConversationOutcomes | null>>
  readonly imports: ChatImportsModel
  readonly isReview: Readonly<Ref<boolean>>
  /** 最近一条已完成的知君回复的时间；没有则 null。 */
  readonly lastTurnAt: Readonly<Ref<string | null>>
  send(text: string, depth?: 'brief' | 'deep', mode?: TurnMode): Promise<void> | void
  stop(): void
  setText(text: string): void
  setDeliberate(on: boolean): void
  focus(): void
  openWorkspace(tab: WorkspaceTab): void
  onConfirmDraft(payload: DecisionDraftConfirmPayload): Promise<void> | void
  onDiscardDraft(): Promise<void> | void
  retryDraft(): void
}

const activeTurn = shallowRef<ActiveTurnBridge | null>(null)

export function setActiveTurn(bridge: ActiveTurnBridge): void {
  activeTurn.value = bridge
}

/** 只有登记者本人才能清空，避免两个页面实例交替挂载时误清。 */
export function clearActiveTurn(owner: symbol): void {
  if (activeTurn.value?.owner === owner) activeTurn.value = null
}

export function useActiveTurn(): Readonly<ShallowRef<ActiveTurnBridge | null>> {
  return activeTurn
}
