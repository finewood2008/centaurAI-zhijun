import type { Router } from 'vue-router'
import { getOnboardingProgressForNavigation, onboardingNavigationRevision } from '@/services/api'
import { hasProductScope, isDesktopProduct, productScopeEpoch } from '@/shared/productScope'

export function installProductGuards(router: Router): void {
// 首次引导是产品的一部分，而不是对话页里碰巧出现的一张空白卡。
// 服务不可用时不锁死应用；已有用户由后端一次性迁移为 ready。
router.beforeEach(async (to) => {
  if (isDesktopProduct() && !hasProductScope()) return true
  if (to.path === '/settings') return true
  const scopeEpoch = productScopeEpoch()
  const navigationRevision = onboardingNavigationRevision()
  try {
    const progress = await getOnboardingProgressForNavigation()
    if (isDesktopProduct() && scopeEpoch !== productScopeEpoch()) return true
    if (navigationRevision !== onboardingNavigationRevision()) return true
    if (progress.state !== 'ready') {
      const path = progress.conversationId
        ? `/onboarding/c/${encodeURIComponent(progress.conversationId)}`
        : '/onboarding/chat'
      if (to.path !== path && to.path !== '/onboarding') return { path, replace: true }
      return true
    }
    if (to.meta.onboardingFlow === true) return { path: progress.conversationId ? `/c/${encodeURIComponent(progress.conversationId)}` : '/chat', replace: true }
  } catch {
    // 保留原页面的错误处理能力，避免一次状态请求失败让整个应用无法进入。
  }
  return true
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : '知君'
  document.title = `${title} · 知君`
})

}
