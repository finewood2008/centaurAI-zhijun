import { createRouter, createWebHistory, type RouteRecordRaw } from 'vue-router'

// 五个一级入口：今日（首屏）/ 对话 / 我的本体 / 判断 / 资料与边界。
// 旧的资料管理页面保留为隐藏路由（不进侧栏），由「资料与边界」枢纽链接过去。
const routes: RouteRecordRaw[] = [
  { path: '/', name: 'today', component: () => import('@/pages/TodayPage.vue'), meta: { title: '今日' } },
  { path: '/chat', name: 'conversation', component: () => import('@/pages/ConversationPage.vue'), meta: { title: '对话' } },
  { path: '/c/:conversationId', name: 'conversation-detail', component: () => import('@/pages/ConversationPage.vue'), meta: { title: '对话' } },
  { path: '/me', name: 'ontology', component: () => import('@/pages/OntologyPage.vue'), meta: { title: '我的本体' } },
  { path: '/me/inbox', name: 'ontology-inbox', component: () => import('@/pages/OntologyPage.vue'), meta: { title: '我的本体' } },
  { path: '/judgments', name: 'judgments', component: () => import('@/pages/GrowthPage.vue'), meta: { title: '判断' } },
  { path: '/growth', redirect: '/judgments' },
  { path: '/data', name: 'data', component: () => import('@/pages/DataHubPage.vue'), meta: { title: '资料与边界' } },
  // 隐藏路由（不进侧栏）
  { path: '/materials', name: 'materials', component: () => import('@/pages/RawMaterialsPage.vue'), meta: { title: '原材料' } },
  { path: '/materials/:materialId', name: 'material-detail', component: () => import('@/pages/MaterialDetailPage.vue'), meta: { title: '原材料详情' } },
  { path: '/knowledge', name: 'knowledge', component: () => import('@/pages/KnowledgePage.vue'), meta: { title: '知识档案' } },
  { path: '/knowledge/new', name: 'knowledge-new', component: () => import('@/pages/KnowledgeEditPage.vue'), meta: { title: '新建知识卡片' } },
  { path: '/knowledge/:knowledgeId', name: 'knowledge-edit', component: () => import('@/pages/KnowledgeEditPage.vue'), meta: { title: '编辑知识卡片' } },
  { path: '/recycle-bin', name: 'recycle-bin', component: () => import('@/pages/RecycleBinPage.vue'), meta: { title: '回收站' } },
  { path: '/search', name: 'search', component: () => import('@/pages/SearchPage.vue'), meta: { title: '搜索记忆' } },
  { path: '/graph', name: 'graph', component: () => import('@/pages/GraphPage.vue'), meta: { title: '关系图谱' } },
  { path: '/settings', name: 'settings', component: () => import('@/pages/SettingsPage.vue'), meta: { title: '偏好' } },
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
