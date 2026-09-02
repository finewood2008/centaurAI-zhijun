<script setup lang="ts">
// 侧栏导航：五个一级入口 今日 / 对话 / 我的本体 / 判断 / 资料与边界；底部偏好。
// 桌面 ≥768px 常驻（768-1199 折叠为图标栏），<768px 转为抽屉（由 open 控制）。
import { computed, nextTick, onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import { getOntologyStats } from '@/services/api'
import { confirmedFraction } from '@/shared/selfmap'
import RingGlyph from '@/components/ui/RingGlyph.vue'
import {
  Database,
  MessageCircle,
  Scale,
  Settings,
  Sun,
  UserRound,
  X,
  type LucideIcon,
} from 'lucide-vue-next'

interface NavItem {
  to: string
  label: string
  icon: LucideIcon
  exact?: boolean
  // 额外算作本项激活的路径前缀（对话既是 /chat 也是 /c/:id）
  alsoPrefix?: string
}

interface NavGroup {
  title: string
  items: NavItem[]
}

const groups: NavGroup[] = [
  {
    title: '知君',
    items: [
      { to: '/', label: '今日', icon: Sun, exact: true },
      { to: '/chat', label: '对话', icon: MessageCircle, alsoPrefix: '/c/' },
      { to: '/me', label: '我的本体', icon: UserRound },
      { to: '/judgments', label: '判断', icon: Scale },
      { to: '/data', label: '资料与边界', icon: Database },
    ],
  },
]

// 「我的本体」旁的小环：实心弧 = 已确认占比。读取失败静默。
const ontoCounts = ref<{ confirmed: number; working: number } | null>(null)
onMounted(async () => {
  try {
    const stats = await getOntologyStats()
    ontoCounts.value = { confirmed: stats.claims.confirmed, working: stats.claims.working }
  } catch {
    ontoCounts.value = null
  }
})
const ontoFraction = computed(() => (ontoCounts.value ? confirmedFraction(ontoCounts.value.confirmed, ontoCounts.value.working) : 0))
const ontoTitle = computed(() => (ontoCounts.value ? `已确认 ${ontoCounts.value.confirmed} 条 · 待确认 ${ontoCounts.value.working} 条` : ''))

const props = defineProps<{
  open?: boolean
}>()

const emit = defineEmits<{ (e: 'close'): void; (e: 'navigate'): void }>()

const route = useRoute()
const currentPath = computed(() => route.path)

// ---- 移动端抽屉：关闭时不可聚焦、不可被读屏器访问 ----
const MOBILE_QUERY = '(max-width: 767px)'
const isMobile = ref(typeof window !== 'undefined' && window.matchMedia(MOBILE_QUERY).matches)

function onMobileChange(e: MediaQueryListEvent) {
  const wasMobile = isMobile.value
  isMobile.value = e.matches
  // 从移动端切回桌面时，若抽屉仍打开，同步重置父级 open，避免遮罩残留覆盖桌面页面
  if (wasMobile && !e.matches && props.open) {
    emit('close')
  }
}

const mq = typeof window !== 'undefined' ? window.matchMedia(MOBILE_QUERY) : null
mq?.addEventListener('change', onMobileChange)
onBeforeUnmount(() => mq?.removeEventListener('change', onMobileChange))

// 移动端且抽屉打开：进入对话框/焦点陷阱模式
const drawerActive = computed(() => isMobile.value && !!props.open)
// 移动端且抽屉关闭：从 Tab 顺序与读屏器树中移除
const hidden = computed(() => isMobile.value && !props.open)

const closeBtn = ref<HTMLElement | null>(null)
const sidebarRef = ref<HTMLElement | null>(null)

// 打开后焦点移到关闭按钮；关闭后焦点还给顶栏菜单按钮
watch(
  () => props.open,
  async (open) => {
    if (!isMobile.value) return
    await nextTick()
    if (open) {
      closeBtn.value?.focus()
    } else {
      document.querySelector<HTMLElement>('.ws-topbar__menu')?.focus()
    }
  },
)

// 抽屉打开时限制 Tab 焦点在抽屉内循环，避免键盘用户聚焦被遮罩的主内容
function onDrawerKeydown(e: KeyboardEvent) {
  if (!drawerActive.value) return
  if (e.key !== 'Tab') return
  const focusables = Array.from(
    sidebarRef.value?.querySelectorAll<HTMLElement>('a[href], button, input, select, textarea, [tabindex]:not([tabindex="-1"])') ?? [],
  ).filter((el) => !el.hasAttribute('disabled'))
  if (!focusables.length) return
  const first = focusables[0]
  const last = focusables[focusables.length - 1]
  if (e.shiftKey && document.activeElement === first) {
    e.preventDefault()
    last.focus()
  } else if (!e.shiftKey && document.activeElement === last) {
    e.preventDefault()
    first.focus()
  }
}

watch(
  drawerActive,
  (active) => {
    if (active) window.addEventListener('keydown', onDrawerKeydown)
    else window.removeEventListener('keydown', onDrawerKeydown)
  },
  { immediate: true },
)

onBeforeUnmount(() => window.removeEventListener('keydown', onDrawerKeydown))

function isActive(item: NavItem): boolean {
  if (item.exact) return currentPath.value === item.to
  if (item.alsoPrefix && currentPath.value.startsWith(item.alsoPrefix)) return true
  return currentPath.value === item.to || currentPath.value.startsWith(`${item.to}/`)
}
</script>

<template>
  <aside
    ref="sidebarRef"
    class="ws-sidebar"
    :class="{ 'is-open': open }"
    :inert="hidden || undefined"
    :aria-hidden="hidden || undefined"
    :role="drawerActive ? 'dialog' : undefined"
    :aria-modal="drawerActive ? 'true' : undefined"
    :aria-label="drawerActive ? '导航菜单' : undefined"
  >
    <div class="ws-sidebar__brand">
      <span class="ws-sidebar__seal" aria-hidden="true">知</span>
      <span class="ws-sidebar__brand-text">知君</span>
      <button
        ref="closeBtn"
        class="ws-sidebar__close"
        type="button"
        aria-label="关闭导航"
        @click="emit('close')"
      >
        <X :size="18" aria-hidden="true" />
      </button>
    </div>

    <nav class="ws-sidebar__nav" aria-label="主导航">
      <section v-for="group in groups" :key="group.title" class="ws-sidebar__group">
        <h2 class="ws-sidebar__group-title">{{ group.title }}</h2>
        <ul class="ws-sidebar__list">
          <li v-for="item in group.items" :key="item.to">
            <RouterLink
              :to="item.to"
              class="ws-sidebar__item"
              :class="{ 'is-active': isActive(item) }"
              :aria-current="isActive(item) ? 'page' : undefined"
              :aria-label="item.label"
              :title="item.label"
              @click="emit('navigate')"
            >
              <component :is="item.icon" class="ws-sidebar__icon" :size="18" aria-hidden="true" />
              <span class="ws-sidebar__label">{{ item.label }}</span>
              <RingGlyph v-if="item.to === '/me' && ontoCounts" class="ws-sidebar__ring" :fraction="ontoFraction" :size="18" :title="ontoTitle" />
            </RouterLink>
          </li>
        </ul>
      </section>
    </nav>

    <div class="ws-sidebar__footer">
      <RouterLink
        to="/settings"
        class="ws-sidebar__item"
        :class="{ 'is-active': currentPath === '/settings' }"
        :aria-current="currentPath === '/settings' ? 'page' : undefined"
        aria-label="偏好"
        :title="'偏好'"
        @click="emit('navigate')"
      >
        <Settings class="ws-sidebar__icon" :size="18" aria-hidden="true" />
        <span class="ws-sidebar__label">偏好</span>
      </RouterLink>
    </div>
  </aside>

  <Teleport to="body">
    <div
      v-if="drawerActive"
      class="ws-sidebar-backdrop"
      aria-hidden="true"
      @click="emit('close')"
    />
  </Teleport>
</template>

<style scoped>
.ws-sidebar {
  display: flex;
  flex-direction: column;
  width: 240px;
  flex-shrink: 0;
  background: var(--ws-body-bg, #fff);
  border-right: 1px solid var(--ws-border-color-2, #e2ded4);
}

.ws-sidebar__brand {
  display: flex;
  align-items: center;
  gap: 10px;
  height: 60px;
  padding: 0 16px;
  border-bottom: 1px solid var(--ws-border-color-3, #ebe7de);
  flex-shrink: 0;
}

/* 朱砂印：衬线「知」字，替代原企业 logo */
.ws-sidebar__seal {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  width: 28px;
  height: 28px;
  border: 1.5px solid var(--ws-primary-color, #a6452e);
  border-radius: 4px;
  color: var(--ws-primary-color, #a6452e);
  font-family: var(--ws-font-display, serif);
  font-size: 17px;
  font-weight: 700;
  line-height: 1;
  flex-shrink: 0;
}

.ws-sidebar__brand-text {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  font-family: var(--ws-font-display, serif);
  font-size: 18px;
  font-weight: 700;
  letter-spacing: 0.08em;
  color: var(--ws-text-primary-color, #1d211f);
  white-space: nowrap;
}

.ws-sidebar__close {
  display: none;
  align-items: center;
  justify-content: center;
  margin-left: auto;
  width: 30px;
  height: 30px;
  border: none;
  border-radius: var(--ws-radius, 6px);
  background: transparent;
  color: var(--ws-text-secondary-color, #686b66);
  cursor: pointer;
}
.ws-sidebar__close:hover {
  background: var(--ws-surface-2, #fbf8f1);
  color: var(--ws-text-primary-color, #1d211f);
}

.ws-sidebar__nav {
  flex: 1;
  overflow-y: auto;
  padding: 12px 8px;
}

.ws-sidebar__group {
  margin-bottom: 16px;
}

.ws-sidebar__group-title {
  margin: 0 0 4px;
  padding: 0 12px;
  font-size: 12px;
  font-weight: 600;
  color: var(--ws-text-secondary-color, #686b66);
  letter-spacing: 0.04em;
}

.ws-sidebar__list {
  list-style: none;
  margin: 0;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 2px;
}

.ws-sidebar__item {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border: 1px solid transparent;
  border-radius: var(--ws-radius, 6px);
  color: var(--ws-text-color, #3c403d);
  font-size: 13px;
  font-weight: 500;
  text-decoration: none;
  transition:
    background 0.15s,
    color 0.15s,
    border-color 0.15s;
}

.ws-sidebar__item:hover {
  background: var(--ws-surface-2, #fbf8f1);
  color: var(--ws-text-primary-color, #1d211f);
}

.ws-sidebar__item.is-active {
  background: var(--ws-edit-color, rgba(166, 69, 46, 0.06));
  color: var(--ws-primary-color, #a6452e);
  font-weight: 600;
}

.ws-sidebar__item.is-disabled {
  opacity: 0.5;
  cursor: not-allowed;
}

.ws-sidebar__ring {
  margin-left: auto;
  opacity: 0.85;
}
.ws-sidebar__icon {
  flex-shrink: 0;
}

.ws-sidebar__footer {
  padding: 12px 8px;
  border-top: 1px solid var(--ws-border-color-3, #ebe7de);
}

/* 768-1199px：折叠为图标栏 */
@media (max-width: 1199px) {
  .ws-sidebar {
    width: 64px;
  }
  .ws-sidebar__brand {
    justify-content: center;
    padding: 0;
  }
  .ws-sidebar__brand-text,
  .ws-sidebar__group-title,
  .ws-sidebar__label {
    display: none;
  }
  .ws-sidebar__item {
    justify-content: center;
    padding: 9px;
  }
}

/* <768px：转为抽屉 */
@media (max-width: 767px) {
  .ws-sidebar {
    position: fixed;
    top: 0;
    bottom: 0;
    left: 0;
    z-index: 2100;
    width: 240px;
    transform: translateX(-100%);
    transition: transform 0.2s ease;
    box-shadow: var(--ws-shadow-lg, 0 16px 48px rgba(0, 0, 0, 0.18));
  }
  .ws-sidebar.is-open {
    transform: translateX(0);
  }
  .ws-sidebar__brand {
    justify-content: flex-start;
    padding: 0 16px;
  }
  .ws-sidebar__brand-text,
  .ws-sidebar__group-title,
  .ws-sidebar__label {
    display: inline;
  }
  .ws-sidebar__item {
    justify-content: flex-start;
    padding: 9px 12px;
  }
  .ws-sidebar__close {
    display: inline-flex;
  }
}

.ws-sidebar-backdrop {
  position: fixed;
  inset: 0;
  z-index: 2090;
  background: rgba(29, 33, 31, 0.42);
}
</style>
