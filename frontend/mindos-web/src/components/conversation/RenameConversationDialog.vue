<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from 'vue'
import type { Conversation } from '@/services/api'
import { conversationTitleError } from '@/shared/conversationManagement'
import BaseButton from '@/components/ui/BaseButton.vue'

const props = defineProps<{ conversation: Conversation | null; busy?: boolean; error?: string }>()
const emit = defineEmits<{ save: [title: string]; close: [] }>()
const dialog = ref<HTMLDialogElement | null>(null)
const input = ref<HTMLInputElement | null>(null)
const title = ref('')
const validation = computed(() => conversationTitleError(title.value))
let returnFocus: HTMLElement | null = null
watch(() => props.conversation?.id, async id => {
  if (id) {
    title.value = props.conversation?.title || ''
    returnFocus = document.activeElement as HTMLElement
    await nextTick()
    if (props.conversation?.id !== id) return
    dialog.value?.showModal()
    input.value?.focus({ preventScroll: true })
    input.value?.select()
  } else {
    dialog.value?.close()
    if (returnFocus?.isConnected) returnFocus.focus({ preventScroll: true })
  }
}, { immediate: true })
function close() { if (!props.busy) emit('close') }
function save() { if (!props.busy && !validation.value) emit('save', title.value.trim()) }
onBeforeUnmount(() => dialog.value?.close())
</script>
<template>
  <Teleport to="body">
    <dialog ref="dialog" class="conversation-rename" aria-label="重命名对话" @cancel.prevent="close">
      <form @submit.prevent="save">
        <h2>重命名对话</h2>
        <label for="conversation-title">对话名称</label>
        <input id="conversation-title" ref="input" v-model="title" :disabled="busy" autocomplete="off" aria-describedby="conversation-title-hint" :aria-invalid="!!validation">
        <p id="conversation-title-hint">1～80 个字 · 只修改名称，不改变对话内容</p>
        <p v-if="validation || error" role="alert">{{ validation || error }}</p>
        <footer><BaseButton :disabled="busy" @click="close">取消</BaseButton><BaseButton type="submit" variant="primary" :disabled="!!validation" :loading="busy">保存名称</BaseButton></footer>
      </form>
    </dialog>
  </Teleport>
</template>
<style scoped>
.conversation-rename { width:min(420px,calc(100vw - 32px)); box-sizing:border-box; border:1px solid var(--ws-border-color,#d8d3c8); border-radius:12px; padding:22px; background:var(--ws-body-bg,#fffcf7); color:var(--ws-text-color,#3c403d); }
.conversation-rename::backdrop { background:#211e193d; }
h2 { margin:0 0 18px; font-size:19px; } label { font-size:14px; }
input { width:100%; box-sizing:border-box; margin-top:8px; padding:10px; border:1px solid var(--ws-border-color,#d8d3c8); border-radius:6px; background:var(--ws-card-bg,#fff); font:inherit; color:inherit; }
p { font-size:12px; line-height:1.6; color:var(--ws-text-secondary-color,#686b66); } [role=alert] { color:var(--ws-primary-color,#a6452e); }
footer { display:flex; justify-content:flex-end; gap:8px; margin-top:18px; }
</style>
