import { createRouter, createWebHistory } from 'vue-router'
import { productRoutes } from './routes'
import { installProductGuards } from './guards'

const router = createRouter({ history: createWebHistory('/mindos/'), routes: productRoutes })
installProductGuards(router)
export default router
