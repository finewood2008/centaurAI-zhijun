import { createRouter, createWebHashHistory } from 'vue-router'
import { productRoutes } from '../router/routes'
import { installProductGuards } from '../router/guards'

export function createDesktopRouter() {
  const routes = productRoutes.map(route => route.path === '/settings'
    ? { path: route.path, name: route.name, meta: route.meta, component: () => import('./DesktopSettingsPage.vue') }
    : route)
  const router = createRouter({ history: createWebHashHistory(), routes })
  installProductGuards(router)
  return router
}
