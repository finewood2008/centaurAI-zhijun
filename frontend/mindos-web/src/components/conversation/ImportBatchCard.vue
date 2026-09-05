<script setup lang="ts">
import { FileText, RotateCw } from 'lucide-vue-next'
import type { ChatImportBatch, ChatImportFile, ChatMaterialRef } from '@/services/api'
defineProps<{ batch: ChatImportBatch; busy?: boolean }>()
const emit = defineEmits<{
  (e: 'preview', ref: ChatMaterialRef): void
  (e: 'retry', fileId?: string): void
  (e: 'consent', refs: ChatMaterialRef[]): void
  (e: 'reupload', item: ChatImportFile, file: File): void
  (e: 'reference', refs: ChatMaterialRef[]): void
}>()
const labels: Record<string, string> = { pending: '等待上传', uploading: '上传中', saved: '已保存到本机 · 排队读取', reading: '正在读取', ready: '可以讨论', failed: '处理失败', paused: '已暂停', empty: '未提取到文字', unavailable: '文件不可用' }
function readyRefs(batch: ChatImportBatch) {
  return [...new Map(batch.files.filter(f => f.state === 'ready' && f.materialId && f.version).map(f => [f.materialId, { materialId: f.materialId!, version: f.version! }])).values()]
}
function reupload(item: ChatImportFile, event: Event) {
  const input = event.target as HTMLInputElement
  if (input.files?.[0]) emit('reupload', item, input.files[0])
  input.value = ''
}
</script>

<template>
  <section class="import-batch" aria-label="这条消息的文件" :data-import-id="batch.id">
    <div v-for="file in batch.files" :key="file.id" class="import-file">
      <FileText :size="19" aria-hidden="true" />
      <div class="import-file__body">
        <button class="import-file__name" :disabled="file.state !== 'ready'" @click="emit('preview', { materialId: file.materialId!, version: file.version! })">{{ file.name }}</button>
        <p :class="{ 'is-error': ['failed', 'empty', 'unavailable'].includes(file.state) }">{{ labels[file.state] }}<span v-if="file.error"> · {{ file.error }}</span></p>
      </div>
      <label v-if="!file.materialId && ['failed', 'pending', 'uploading'].includes(file.state)" class="reupload">
        重新选择
        <input type="file" :disabled="busy" :aria-label="`重新上传 ${file.name}`" @change="reupload(file, $event)" />
      </label>
      <button v-if="file.materialId && ['failed', 'empty', 'paused'].includes(file.state)" :disabled="busy || batch.state === 'replying'" @click="emit('retry', file.id)">{{ file.state === 'paused' ? '继续' : '重试读取' }}</button>
    </div>
    <p v-if="batch.state === 'waiting' || batch.state === 'queued'" class="batch-note">读取在后台进行，你可以继续聊其他内容。</p>
    <p v-if="batch.state === 'replying'" class="batch-note" role="status">知君正在整理这批文件的反馈…</p>
    <p v-if="batch.error && !['queued', 'waiting'].includes(batch.state)" class="batch-note">{{ batch.error }}</p>
    <div class="batch-actions">
      <button v-if="batch.state === 'consent'" @click="emit('consent', readyRefs(batch))">确认文件处理方式</button>
      <button v-if="['failed', 'paused', 'uploading'].includes(batch.state)" :disabled="busy" @click="emit('retry')"><RotateCw :size="12" /> {{ batch.state === 'paused' ? '继续读取' : '重试 / 继续' }}</button>
      <button v-if="readyRefs(batch).length" @click="emit('reference', readyRefs(batch))">继续参考这批文件</button>
    </div>
  </section>
</template>

<style scoped>
.import-batch { box-sizing: border-box; margin: 7px 0 0 auto; width: fit-content; max-width: min(760px, 100%); border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 9px; background: var(--ws-surface-2, #fbf8f1); padding: 12px 16px; }
.import-file { display: flex; align-items: center; gap: 11px; padding: 7px 0; color: var(--ws-text-secondary-color, #686b66); }
.import-file__body { flex: 1; min-width: 0; }
.import-file__name { display: block; max-width: 100%; width: 100%; text-align: left; white-space: normal; color: var(--ws-text-primary-color, #1d211f); overflow-wrap: anywhere; word-break: break-word; }
button { border: 0; background: none; font: inherit; cursor: pointer; padding: 0; }
button:disabled { cursor: default; }
p { margin: 5px 0 0; font-size: 12px; line-height: 1.6; }
.is-error { color: var(--ws-danger-color, #a6452e); }
.batch-note { color: var(--ws-text-secondary-color, #686b66); }
.batch-actions { display: flex; flex-wrap: wrap; gap: 14px; margin-top: 9px; }
.batch-actions button, .reupload { color: var(--ws-primary-color, #a6452e); font-size: 12px; display: inline-flex; align-items: center; gap: 5px; }
.reupload { position: relative; cursor: pointer; }
.reupload input { position: absolute; inset: 0; opacity: 0; width: 100%; cursor: pointer; }
</style>
