<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue'
import { productPreview, releaseProductPreview } from '@/services/productFiles'
import { isDesktopProduct } from '@/shared/productScope'
import SideDrawer from './SideDrawer.vue'
const props = withDefaults(defineProps<{ src: string; alt: string; interactive?: boolean }>(), { interactive: true })
const container = ref<HTMLElement | null>(null)
const url = ref(''), error = ref(''), open = ref(false)
let observer: IntersectionObserver | null = null
let controller: AbortController | null = null
let visible = !isDesktopProduct()
let revision = 0
function clear() {
  revision++; controller?.abort(); controller = null
  if (url.value) releaseProductPreview(url.value)
  url.value = ''; open.value = false
}
async function load() {
  clear(); error.value = ''
  if (!visible || !props.src) return
  const ticket = revision
  controller = new AbortController()
  try {
    const result = await productPreview(props.src, controller.signal)
    if (ticket !== revision) releaseProductPreview(result)
    else url.value = result
  } catch (e) { if (ticket === revision) error.value = e instanceof Error ? e.message : '图片暂不可用' }
}
watch(open, value => { if (!value && !visible) clear() })
watch(() => props.src, () => { void load() })
onMounted(() => {
  if (!isDesktopProduct() || !('IntersectionObserver' in window)) { visible = true; void load(); return }
  observer = new IntersectionObserver(entries => {
    const next = entries[0]?.isIntersecting ?? false
    if (next === visible) return
    visible = next
    if (next) void load()
    else if (!open.value) clear()
  })
  if (container.value) observer.observe(container.value)
})
onBeforeUnmount(() => { observer?.disconnect(); clear() })
</script>
<template>
  <div ref="container" class="product-image">
    <img v-if="url && !interactive" :src="url" :alt="alt" class="product-image__thumb" />
    <button v-else-if="url" class="product-image__open" :aria-label="`查看${alt}`" @click="open = true"><img :src="url" :alt="alt" /></button>
    <p v-else-if="error" role="status">{{ error }} <button @click="load">重新加载</button></p>
    <span v-else>{{ visible ? '正在读取图片…' : alt }}</span>
    <SideDrawer v-if="interactive" :open="open" :title="alt" @close="open = false"><img v-if="url" :src="url" :alt="alt" style="max-width:100%;height:auto" /></SideDrawer>
  </div>
</template>
<style scoped>
.product-image{min-height:48px;max-width:100%}.product-image__open{padding:0;border:0;background:transparent;cursor:zoom-in;max-width:100%;width:100%}.product-image__open img,.product-image__thumb{max-width:100%;max-height:520px;object-fit:contain;display:block;margin:auto}.product-image p{font-size:12px;color:var(--ws-text-secondary-color)}
</style>
