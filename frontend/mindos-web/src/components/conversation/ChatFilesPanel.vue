<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch, type UnwrapNestedRefs } from 'vue'
import { FileText, X } from 'lucide-vue-next'
import type { useChatImports } from '@/composables/useChatImports'
import ConfirmDialog from '@/components/ui/ConfirmDialog.vue'
const props = defineProps<{ model: UnwrapNestedRefs<ReturnType<typeof useChatImports>> }>()
const m = computed(() => props.model)
const previewDialog = ref<HTMLDialogElement | null>(null)
const consentNames = computed(() => m.value.consentRefs?.map(r => m.value.files.find(f => f.materialId === r.materialId && f.version === r.version)?.name || r.materialId).join('、'))
watch(() => m.value.previewOpen, async open => {
  await nextTick()
  if (open) previewDialog.value?.showModal()
  else previewDialog.value?.close()
})
onBeforeUnmount(() => previewDialog.value?.close())
</script>

<template>
  <div class="chat-files">
    <p v-if="m.loadError" class="file-warning" role="status">文件状态暂时无法同步：{{ m.loadError }} <button @click="m.refresh()">重试</button></p>
    <div v-if="m.staged.length" class="staged-files" aria-label="待发送文件">
      <div v-for="file in m.staged" :key="file.id" class="staged-file">
        <FileText :size="17" /><span>{{ file.name }}</span>
        <button :aria-label="`移除 ${file.name}`" @click="m.staged = m.staged.filter(f => f.id !== file.id)"><X :size="14" /></button>
      </div>
      <p>发送后保存到本机资料库 · {{ m.staged.length }}/5 个 · 不自动写入个人理解</p>
    </div>
    <details v-if="m.references.length || m.files.some(f => f.state === 'ready')" class="reference-picker">
      <summary>正在参考：{{ m.selectedFiles.length ? m.selectedFiles.map(f => f.name).join('、') : '未选择文件' }}</summary>
      <div class="reference-list">
        <label v-for="file in m.files.filter((f, i, a) => f.state === 'ready' && a.findIndex(x => x.materialId === f.materialId && x.version === f.version) === i)" :key="file.id">
          <input type="checkbox" :checked="m.references.some(r => r.materialId === file.materialId && r.version === file.version)"
            @change="m.chooseReferences(($event.target as HTMLInputElement).checked ? [...m.references, { materialId: file.materialId!, version: file.version! }] : m.references.filter(r => r.materialId !== file.materialId))" />
          {{ file.name }}
        </label>
      </div>
      <p>{{ m.localOnly ? '文件讨论仅使用本地模型' : '使用外部模型前会检查文件授权' }} <button v-if="m.references.length" @click="m.showConsent()">更改处理方式</button></p>
      <button v-if="m.references.length" @click="m.chooseReferences([])">暂不参考文件，聊点别的</button>
    </details>
    <p v-if="m.uploading" class="file-warning">文件正在上传；完成后会在对应消息里显示读取进度。</p>
  </div>

  <ConfirmDialog :open="m.pickerOpen" title="选择已有资料" confirm-text="完成选择" cancel-text="关闭" @confirm="m.pickerOpen = false" @cancel="m.pickerOpen = false">
    <input v-model="m.query" class="library-search" placeholder="搜索文件名称" aria-label="搜索已有资料" />
    <p v-if="m.libraryLoading">正在读取资料库…</p>
    <p v-else-if="m.libraryError" role="alert">{{ m.libraryError }}</p>
    <div v-else class="library-list">
      <button v-for="file in m.filteredLibrary" :key="file.materialId" :disabled="m.staged.some(f => f.materialId === file.materialId)" @click="m.stageMaterial(file)">
        <FileText :size="16" /><span>{{ file.fileName }}</span><small>{{ m.staged.some(f => f.materialId === file.materialId) ? '已选择' : '添加' }}</small>
      </button>
      <p v-if="!m.filteredLibrary.length">没有匹配的资料，可以先上传文件。</p>
    </div>
    <p>已有文件不会重复上传。已选择 {{ m.staged.length }}/5 个。</p>
  </ConfirmDialog>

  <ConfirmDialog :open="!!m.consentRefs" title="这些文件由谁来读？" :confirm-text="m.service?.external ? '允许发给此服务' : '仅用本地模型读取'" cancel-text="暂不处理" :loading="m.consentBusy" @confirm="m.consent(!m.service?.external)" @cancel="m.consentRefs = null">
    <p class="consent-files">{{ consentNames }}</p>
    <p>原文件和解析保留在本机。若允许，回答所需的文字片段及相关对话会发送给：</p>
    <p><strong>{{ m.service?.name || '当前服务未配置' }}</strong> · {{ m.service?.model }}</p>
    <p>授权绑定文件版本和此服务；换服务或换文件版本需重新确认。仅本地处理不会外发这些文件。</p>
    <button v-if="m.service?.external" class="local-choice" :disabled="m.consentBusy" @click="m.consent(true)">仅用本地模型读取</button>
  </ConfirmDialog>

  <Teleport to="body">
    <dialog ref="previewDialog" class="file-preview" @close="m.previewOpen = false" @cancel="m.previewOpen = false">
      <header><h2>{{ m.preview?.name || '文件正文' }}</h2><button aria-label="关闭文件预览" @click="m.previewOpen = false"><X :size="20" /></button></header>
      <p>这是本机解析出的文字，不等于原文排版。回答可能只使用其中相关片段。</p>
      <p v-if="m.previewError" role="alert">{{ m.previewError }}</p>
      <pre v-else-if="m.preview">{{ m.preview.text || '未提取到文字' }}</pre>
      <p v-else>正在读取正文…</p>
      <button v-if="m.preview?.hasMore && m.previewRef" class="local-choice" @click="m.showPreview(m.previewRef, true)">继续加载正文</button>
      <RouterLink v-if="m.previewRef" :to="`/materials/${encodeURIComponent(m.previewRef.materialId)}`" @click="m.previewOpen = false">打开资料详情</RouterLink>
    </dialog>
  </Teleport>
</template>

<style scoped>
.chat-files { display: grid; gap: 8px; font-size: 12px; color: var(--ws-text-secondary-color, #686b66); }
button { font: inherit; cursor: pointer; background: none; border: 0; color: var(--ws-primary-color, #a6452e); }
.staged-files { display: flex; flex-wrap: wrap; gap: 6px; }
.staged-file { display: flex; align-items: center; gap: 7px; max-width: 100%; padding: 8px 10px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 7px; background: var(--ws-surface-2, #fbf8f1); }
.staged-file span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 220px; }
.staged-files p { flex-basis: 100%; margin: 0; }
.reference-picker summary { cursor: pointer; line-height: 1.6; overflow-wrap: anywhere; }
.reference-list { display: grid; gap: 7px; max-height: 130px; overflow: auto; margin-top: 9px; }
.file-warning { margin: 0; color: var(--ws-danger-color, #a6452e); }
.library-search { width: 100%; padding: 9px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 6px; background: inherit; color: inherit; }
.library-list { max-height: 300px; overflow: auto; padding: 9px 0; }
.library-list button { display: flex; width: 100%; align-items: center; gap: 8px; padding: 10px 2px; text-align: left; border-bottom: 1px solid var(--ws-border-color-3, #ebe7de); }
.library-list span { flex: 1; overflow-wrap: anywhere; }
.library-list button:disabled { opacity: .55; }
.consent-files { overflow-wrap: anywhere; font-weight: 600; }
.local-choice { border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 6px; padding: 9px 12px; margin: 8px 0; }
.file-preview { box-sizing: border-box; margin: 0 0 0 auto; height: 100dvh; max-height: 100dvh; width: min(580px, 94vw); max-width: 94vw; padding: 22px; border: 0; border-left: 1px solid var(--ws-border-color, #d8d3c8); background: var(--ws-body-bg, #fffdf8); color: var(--ws-text-primary-color, #1d211f); }
.file-preview::backdrop { background: rgb(29 33 31 / 25%); }
.file-preview header { display: flex; justify-content: space-between; align-items: flex-start; gap: 15px; }
.file-preview h2 { font-size: 17px; margin: 0; overflow-wrap: anywhere; }
.file-preview p { font-size: 12px; line-height: 1.7; color: var(--ws-text-secondary-color, #686b66); }
.file-preview pre { font: inherit; font-size: 14px; line-height: 1.8; white-space: pre-wrap; overflow-wrap: anywhere; }
.file-preview a { display: block; margin: 18px 0; color: var(--ws-primary-color, #a6452e); }
</style>
