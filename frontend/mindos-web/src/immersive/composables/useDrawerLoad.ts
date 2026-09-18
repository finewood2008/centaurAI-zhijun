// 抽屉的懒读取：第一次打开才读；读过之后失效（例如每完成一轮回复）时，开着就刷新，合着就等下次打开。
import { ref, watch, type Ref } from 'vue'

export interface DrawerLoad {
  loaded: Readonly<Ref<boolean>>
  loading: Readonly<Ref<boolean>>
  reload: () => Promise<void>
}

export function useDrawerLoad(open: () => boolean, load: () => Promise<void>, invalidator?: () => unknown): DrawerLoad {
  const loaded = ref(false)
  const loading = ref(false)
  const stale = ref(false)
  async function reload() {
    if (loading.value) return
    loading.value = true
    try {
      await load()
      loaded.value = true
      stale.value = false
    } finally {
      loading.value = false
    }
  }
  watch(open, isOpen => { if (isOpen && (!loaded.value || stale.value)) void reload() }, { immediate: true })
  if (invalidator) {
    watch(invalidator, (value, previous) => {
      if (value === previous || !loaded.value) return
      if (open()) void reload()
      else stale.value = true
    })
  }
  return { loaded, loading, reload }
}
