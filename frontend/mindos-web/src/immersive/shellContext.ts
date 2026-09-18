// 沉浸壳注入给嵌入式对话页的上下文：滚动容器、三个 Teleport 宿主与「是否贴近底部」。
// 对话页只在被注入时进入嵌入模式；经典壳下 inject 得到 null，行为不变。
import type { InjectionKey } from 'vue'

export const COMPOSER_HOST = '#zj-composer-host'
export const DESK_HOST = '#zj-desk-host'
export const ADVANCED_HOST = '#zj-prefs-advanced-host'

export interface ImmersiveShellContext {
  readonly embedded: true
  /** 流的滚动容器；未挂载时为 null，对话页回退到自己的消息区。 */
  scroller(): HTMLElement | null
  readonly composerHost: string
  readonly deskHost: string
  readonly advancedHost: string
  /** 用户是否停在流的底部附近；只有贴近底部时新 token 才自动跟随。 */
  isNearBottom(): boolean
}

export const immersiveKey: InjectionKey<ImmersiveShellContext> = Symbol('immersive-shell')
