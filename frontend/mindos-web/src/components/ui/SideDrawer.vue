<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
const props = defineProps<{ open: boolean; title: string }>()
const emit = defineEmits<{ (e: 'close'): void }>()
const dialog = ref<HTMLDialogElement | null>(null)
let returnFocus: HTMLElement | null = null
watch(() => props.open, async open => {
  await nextTick()
  if (open !== props.open) return
  if (open && !dialog.value?.open) {
    returnFocus = document.activeElement as HTMLElement
    dialog.value?.showModal()
  } else if (!open && dialog.value?.open) {
    dialog.value.close()
    if (returnFocus?.isConnected) returnFocus.focus()
  }
}, { immediate: true })
function backdrop(event: MouseEvent) {
  if (!dialog.value || event.target !== dialog.value) return
  const r = dialog.value.getBoundingClientRect()
  if (event.clientX < r.left || event.clientX > r.right || event.clientY < r.top || event.clientY > r.bottom) emit('close')
}
onBeforeUnmount(() => dialog.value?.close())
</script>
<template>
  <Teleport to="body">
    <!-- Keep contents mounted: closing or switching tabs must not erase draft edits. -->
    <dialog ref="dialog" class="side-drawer" :aria-label="title" @cancel.prevent="emit('close')" @click="backdrop">
      <header class="side-drawer__head"><h2>{{ title }}</h2><button type="button" :aria-label="`关闭${title}`" @click="emit('close')"><X :size="20" /></button></header>
      <div v-if="$slots.navigation" class="side-drawer__nav"><slot name="navigation" /></div>
      <div class="side-drawer__body"><slot /></div>
    </dialog>
  </Teleport>
</template>
<style scoped>
.side-drawer { position:fixed; inset:0 0 0 auto; margin:0; width:min(600px,100vw); height:100dvh; max-height:100dvh; max-width:100vw; padding:0; border:0; border-left:1px solid var(--ws-border-color,#d8d3c8); background:var(--ws-bg-color,#fffcf7); color:var(--ws-text-color,#3c403d); box-shadow:-12px 0 48px #29251e18; box-sizing:border-box; }
.side-drawer[open] { display:flex; flex-direction:column; }.side-drawer::backdrop { background:#211e191f; }
.side-drawer__head { display:flex; flex-shrink:0; align-items:center; justify-content:space-between; gap:16px; padding:20px 24px; border-bottom:1px solid var(--ws-border-color-3,#ebe7de); }
.side-drawer__head h2 { margin:0; font:600 20px var(--ws-font-display,serif); }.side-drawer__head button { display:grid; place-items:center; width:36px; height:36px; border:1px solid var(--ws-border-color-3,#ebe7de); border-radius:50%; background:transparent; color:inherit; cursor:pointer; }
.side-drawer__nav { flex-shrink:0; padding:12px 24px; border-bottom:1px solid var(--ws-border-color-3,#ebe7de); }
.side-drawer__body { min-height:0; flex:1; overflow-y:auto; overscroll-behavior:contain; padding:24px; overflow-wrap:anywhere; scrollbar-gutter:stable; }
@media(max-width:600px) { .side-drawer__head { padding:16px; }.side-drawer__nav { padding:10px 16px; }.side-drawer__body { padding:16px; } }
</style>
