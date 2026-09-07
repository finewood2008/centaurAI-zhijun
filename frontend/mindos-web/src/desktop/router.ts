import { createRouter, createWebHashHistory } from 'vue-router'
import { productRoutes } from '../router/routes'
import { installProductGuards } from '../router/guards'

export function createDesktopRouter() {
  const router = createRouter({ history: createWebHashHistory(), routes: productRoutes })
  installProductGuards(router)
  return router
}
