import { inject, type InjectionKey, type Ref, type ShallowRef } from 'vue'
import type { DesktopController, DesktopViewState } from './controller'

export interface DesktopWorkspace {
  controller: DesktopController
  state: ShallowRef<DesktopViewState>
  ready: Readonly<Ref<boolean>>
  scopeKey: Readonly<Ref<string | null>>
}
export const desktopWorkspaceKey: InjectionKey<DesktopWorkspace> = Symbol('desktop-workspace')
export function useDesktopWorkspace(): DesktopWorkspace {
  const workspace = inject(desktopWorkspaceKey)
  if (!workspace) throw new Error('桌面连接服务未初始化')
  return workspace
}
