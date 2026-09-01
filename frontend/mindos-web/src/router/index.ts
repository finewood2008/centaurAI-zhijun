import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

const routes: RouteRecordRaw[] = [
  { path: '/', name: 'home', component: () => import('@/pages/HomePage.vue'), meta: { title: '今日' } },
  { path: '/growth', name: 'growth', component: () => import('@/pages/GrowthPage.vue'), meta: { title: '成长' } },
  { path: '/materials', name: 'materials', component: () => import('@/pages/RawMaterialsPage.vue'), meta: { title: '原材料' } },
  { path: '/materials/:materialId', name: 'material-detail', component: () => import('@/pages/MaterialDetailPage.vue'), meta: { title: '原材料详情' } },
  { path: '/knowledge', name: 'knowledge', component: () => import('@/pages/KnowledgePage.vue'), meta: { title: '知识档案' } },
  { path: '/knowledge/new', name: 'knowledge-new', component: () => import('@/pages/KnowledgeEditPage.vue'), meta: { title: '新建知识卡片' } },
  { path: '/knowledge/:knowledgeId', name: 'knowledge-edit', component: () => import('@/pages/KnowledgeEditPage.vue'), meta: { title: '编辑知识卡片' } },
  { path: '/recycle-bin', name: 'recycle-bin', component: () => import('@/pages/RecycleBinPage.vue'), meta: { title: '回收站' } },
  { path: '/search', name: 'search', component: () => import('@/pages/SearchPage.vue'), meta: { title: '搜索记忆' } },
  { path: '/qa', name: 'qa', component: () => import('@/pages/QaPage.vue'), meta: { title: '问知君' } },
  { path: '/graph', name: 'graph', component: () => import('@/pages/GraphPage.vue'), meta: { title: '关系图谱' } },
  { path: '/generate', name: 'generate', component: () => import('@/pages/GeneratePage.vue'), meta: { title: '内容生成' } },
  { path: '/corrections', name: 'corrections', component: () => import('@/pages/CorrectionsPage.vue'), meta: { title: '纠错本' } },
  { path: '/governance', name: 'governance', component: () => import('@/pages/GovernancePage.vue'), meta: { title: '本体治理' } },
  { path: '/settings', name: 'settings', component: () => import('@/pages/SettingsPage.vue'), meta: { title: '设置' } },
  { path: '/:pathMatch(.*)*', redirect: '/' },
]

const router = createRouter({
  history: createWebHistory('/mindos/'),
  routes,
})

router.afterEach((to) => {
  const title = typeof to.meta.title === 'string' ? to.meta.title : '知君'
  document.title = `${title} · 知君`
})

export default router
