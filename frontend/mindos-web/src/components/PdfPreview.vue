<script setup lang="ts">
import { GlobalWorkerOptions, getDocument, type PDFDocumentLoadingTask, type PDFDocumentProxy, type RenderTask } from 'pdfjs-dist'
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url'
import { nextTick, onBeforeUnmount, ref, watch } from 'vue'

const props = defineProps<{ src: string; title: string }>()
GlobalWorkerOptions.workerSrc = workerUrl

const host = ref<HTMLElement | null>(null)
const canvas = ref<HTMLCanvasElement | null>(null)
const pageNumber = ref(1)
const pageCount = ref(0)
const loading = ref(false)
const error = ref('')
let documentTask: PDFDocumentLoadingTask | null = null
let documentValue: PDFDocumentProxy | null = null
let renderTask: RenderTask | null = null
let fetchController: AbortController | null = null
let revision = 0
const MAX_PDF_BYTES = 64 * 1024 * 1024
const MAX_CANVAS_AXIS = 8192
const MAX_CANVAS_PIXELS = 16_000_000

async function disposeDocument() {
  fetchController?.abort()
  fetchController = null
  renderTask?.cancel()
  renderTask = null
  const task = documentTask
  documentTask = null
  documentValue = null
  if (task) {
    try { await task.destroy() } catch { /* cancellation and teardown are best-effort */ }
  }
}

async function readBoundedPdf(response: Response, signal: AbortSignal): Promise<Uint8Array> {
  if (!response.ok || response.headers.get('content-type')?.split(';', 1)[0].trim().toLowerCase() !== 'application/pdf') {
    throw new Error('PDF_READ_FAILED')
  }
  const declared = Number(response.headers.get('content-length'))
  if (Number.isFinite(declared) && (declared <= 0 || declared > MAX_PDF_BYTES)) throw new Error('PDF_TOO_LARGE')
  if (!response.body) throw new Error('PDF_READ_FAILED')
  const reader = response.body.getReader()
  const chunks: Uint8Array[] = []
  let total = 0
  let complete = false
  try {
    while (true) {
      signal.throwIfAborted()
      const { done, value } = await reader.read()
      if (done) { complete = true; break }
      total += value.byteLength
      if (total > MAX_PDF_BYTES) throw new Error('PDF_TOO_LARGE')
      chunks.push(value)
    }
  } finally {
    if (!complete) await reader.cancel().catch(() => {})
    reader.releaseLock()
  }
  if (!total) throw new Error('PDF_READ_FAILED')
  const data = new Uint8Array(total)
  let offset = 0
  for (const chunk of chunks) { data.set(chunk, offset); offset += chunk.byteLength }
  return data
}

async function renderPage(ticket = revision) {
  if (!documentValue || !canvas.value || !host.value || ticket !== revision) return
  renderTask?.cancel()
  loading.value = true
  error.value = ''
  try {
    const page = await documentValue.getPage(pageNumber.value)
    if (ticket !== revision) return
    const initial = page.getViewport({ scale: 1 })
    const cssWidth = Math.max(280, Math.min(host.value.clientWidth - 24, 920))
    const scale = cssWidth / initial.width
    const pixelRatio = Math.min(window.devicePixelRatio || 1, 2)
    const viewport = page.getViewport({ scale: scale * pixelRatio })
    if (viewport.width > MAX_CANVAS_AXIS || viewport.height > MAX_CANVAS_AXIS
      || viewport.width * viewport.height > MAX_CANVAS_PIXELS) throw new Error('PDF_CANVAS_TOO_LARGE')
    const target = canvas.value
    target.width = Math.ceil(viewport.width)
    target.height = Math.ceil(viewport.height)
    const context = target.getContext('2d', { alpha: false })
    if (!context) throw new Error('PDF_CANVAS_UNAVAILABLE')
    renderTask = page.render({ canvasContext: context, viewport, background: '#ffffff' })
    await renderTask.promise
  } catch (cause) {
    if (ticket === revision && (cause as { name?: string })?.name !== 'RenderingCancelledException') error.value = 'PDF 页面暂时无法显示，请重试。'
  } finally {
    if (ticket === revision) loading.value = false
  }
}

async function load() {
  const ticket = ++revision
  await disposeDocument()
  pageNumber.value = 1; pageCount.value = 0; error.value = ''
  if (!props.src || ticket !== revision) return
  loading.value = true
  const controller = new AbortController()
  fetchController = controller
  try {
    const response = await fetch(props.src, { cache: 'no-store', signal: controller.signal })
    const data = await readBoundedPdf(response, controller.signal)
    if (ticket !== revision) return
    documentTask = getDocument({ data, useSystemFonts: true, isEvalSupported: false,
      maxImageSize: MAX_CANVAS_PIXELS, canvasMaxAreaInBytes: MAX_CANVAS_PIXELS * 4 })
    const loaded = await documentTask.promise
    if (ticket !== revision) return
    documentValue = loaded
    pageCount.value = loaded.numPages
    await nextTick()
    await renderPage(ticket)
  } catch (cause) {
    if (ticket === revision && (cause as { name?: string })?.name !== 'AbortError') {
      error.value = cause instanceof Error && cause.message === 'PDF_TOO_LARGE'
        ? 'PDF 文件过大，无法在应用内安全预览。'
        : 'PDF 原件暂时无法显示，请重试。'
    }
  } finally {
    if (fetchController === controller) fetchController = null
    if (ticket === revision) loading.value = false
  }
}

function changePage(offset: number) {
  const next = Math.min(pageCount.value, Math.max(1, pageNumber.value + offset))
  if (next === pageNumber.value) return
  pageNumber.value = next
  void renderPage()
}

watch(() => props.src, load, { immediate: true })
onBeforeUnmount(() => {
  revision++
  void disposeDocument()
})
</script>

<template>
  <div ref="host" class="pdf-preview" :aria-label="`${title} PDF 预览`">
    <div v-if="pageCount > 1" class="pdf-preview__toolbar">
      <button type="button" :disabled="loading || pageNumber <= 1" @click="changePage(-1)">上一页</button>
      <span aria-live="polite">第 {{ pageNumber }} / {{ pageCount }} 页</span>
      <button type="button" :disabled="loading || pageNumber >= pageCount" @click="changePage(1)">下一页</button>
    </div>
    <p v-if="loading && !pageCount" class="pdf-preview__state">正在打开 PDF…</p>
    <p v-if="error" class="pdf-preview__state pdf-preview__error" role="alert">{{ error }}</p>
    <canvas v-show="!error && pageCount" ref="canvas" class="pdf-preview__canvas" role="img" :aria-label="`${title}，第 ${pageNumber} 页`"></canvas>
  </div>
</template>

<style scoped>
.pdf-preview { min-height: 260px; padding: 12px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 8px; background: #f3f0e9; }
.pdf-preview__toolbar { display: flex; align-items: center; justify-content: center; gap: 12px; margin-bottom: 12px; color: var(--ws-text-secondary-color, #686b66); font-size: 13px; }
.pdf-preview__toolbar button { padding: 7px 12px; border: 1px solid var(--ws-border-color, #d8d3c8); border-radius: 7px; background: var(--ws-card-bg, #fffdf8); color: inherit; cursor: pointer; }
.pdf-preview__toolbar button:disabled { cursor: default; opacity: .45; }
.pdf-preview__state { display: grid; min-height: 220px; margin: 0; place-items: center; color: var(--ws-text-secondary-color, #686b66); }
.pdf-preview__error { color: var(--ws-danger-color, #a6452e); }
.pdf-preview__canvas { display: block; width: min(100%, 920px); height: auto; margin: 0 auto; box-shadow: 0 2px 12px rgb(30 28 24 / 10%); }
</style>
