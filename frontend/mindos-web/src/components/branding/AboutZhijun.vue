<script setup lang="ts">
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'
import { X } from 'lucide-vue-next'
import centaur from '@/assets/centaur-official.png'

const props = defineProps<{ open: boolean }>()
const emit = defineEmits<{ (e: 'update:open', value: boolean): void }>()
const dialog = ref<HTMLDialogElement | null>(null)

watch(() => props.open, async (open) => {
  await nextTick()
  if (open) dialog.value?.showModal()
  else dialog.value?.close()
})
onBeforeUnmount(() => dialog.value?.close())

function closeOnBackdrop(event: MouseEvent) {
  const element = dialog.value
  if (!element || event.target !== element) return
  const bounds = element.getBoundingClientRect()
  if (event.clientX < bounds.left || event.clientX > bounds.right || event.clientY < bounds.top || event.clientY > bounds.bottom) element.close()
}
</script>

<template>
  <Teleport to="body">
    <dialog ref="dialog" class="about-zhijun" aria-label="关于知君" @keydown.esc.stop @close="emit('update:open', false)" @click="closeOnBackdrop">
      <button class="about-zhijun__close" type="button" aria-label="关闭关于知君" @click="dialog?.close()"><X :size="20" /></button>
      <h2>知君</h2>
      <p class="about-zhijun__intro">与你一起，慢慢看清自己。</p>
      <p class="about-zhijun__relation">知君提出理解，你来确认。<br />知君帮助思考，你来决定。</p>
      <div class="about-zhijun__origin">
        <img :src="centaur" alt="半人马人工智能" width="44" height="59" />
        <div><p class="about-zhijun__name">半人马人工智能出品</p><p>人决定方向，<br />人工智能拓展人的能力。</p></div>
      </div>
    </dialog>
  </Teleport>
</template>

<style scoped>
.about-zhijun { box-sizing:border-box; position:fixed; margin:auto; width:min(392px,calc(100vw - 32px)); max-height:calc(100dvh - 32px); overflow:auto; padding:44px 34px 32px; border:1px solid var(--ws-border-color-2,#e2ded4); border-radius:12px; background:var(--ws-body-bg,#fffcf6); color:var(--ws-text-primary-color,#1d211f); text-align:center; box-shadow:0 14px 60px #2828201c; }
.about-zhijun::backdrop { background:#24282045; }
.about-zhijun__close { position:absolute; top:12px; right:12px; display:grid; place-items:center; width:32px; height:32px; min-height:0; padding:0; border:0; border-radius:50%; background:transparent; color:var(--ws-text-secondary-color,#686b66); cursor:pointer; }
.about-zhijun__close:hover { background:var(--ws-surface-2,#fbf8f1); }
.about-zhijun__close:focus-visible { outline:2px solid var(--ws-primary-color,#a6452e); outline-offset:3px; }
.about-zhijun h2 { margin:0; font-family:var(--ws-font-display,serif); font-size:28px; font-weight:400; line-height:1.4; letter-spacing:.08em; }
.about-zhijun__intro { margin:16px 0 30px; color:var(--ws-text-secondary-color,#686b66); font-size:13px; line-height:1.8; }
.about-zhijun__relation { margin:0; font-size:13px; line-height:2.1; }
.about-zhijun__origin { display:flex; align-items:center; justify-content:center; gap:16px; margin-top:30px; padding-top:26px; border-top:1px solid var(--ws-border-color-2,#e2ded4); text-align:left; }
.about-zhijun__origin img { flex-shrink:0; width:44px; height:59px; object-fit:contain; }
.about-zhijun__origin p { margin:0; font-size:12px; line-height:1.8; color:var(--ws-text-secondary-color,#686b66); }
.about-zhijun__origin .about-zhijun__name { margin-bottom:5px; font-family:var(--ws-font-display,serif); font-size:16px; font-weight:400; color:var(--ws-text-primary-color,#1d211f); }
</style>
