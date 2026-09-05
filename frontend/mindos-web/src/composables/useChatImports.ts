import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { api, chatImports, type ChatImportBatch, type ChatImportFile, type ChatMaterialRef, type ChatFileService, type ChatFilePreview, type UploadResult } from '@/services/api'
import { validateImport } from '@/features/import/validation'
import type { ReplyAssistanceInput } from '@/shared/replyAssistance'

export interface StagedChatFile { id: string; name: string; size: number; file?: File; materialId?: string; version?: number }

export function useChatImports(options: {
  conversationId: Ref<string | null | undefined>
  ensure: () => Promise<string>
  refreshMessages: (id: string) => Promise<boolean>
  notify: (message: string) => void
}) {
  const staged = ref<StagedChatFile[]>([])
  const batches = ref<ChatImportBatch[]>([])
  const references = ref<ChatMaterialRef[]>([])
  const localOnly = ref(false)
  const service = ref<ChatFileService | null>(null)
  const uploading = ref(false)
  const pickerOpen = ref(false)
  const library = ref<UploadResult[]>([])
  const libraryLoading = ref(false)
  const libraryError = ref('')
  const query = ref('')
  const preview = ref<ChatFilePreview | null>(null)
  const previewRef = ref<ChatMaterialRef | null>(null)
  const previewError = ref('')
  const previewOpen = ref(false)
  const consentRefs = ref<ChatMaterialRef[] | null>(null)
  const consentBusy = ref(false)
  const busyBatch = ref<string | null>(null)
  const loadError = ref('')
  let timer: ReturnType<typeof setTimeout> | undefined
  let alive = true
  let generation = 0
  let lastSignature = ''
  let requestId = crypto.randomUUID()

  const files = computed(() => batches.value.flatMap(b => b.files))
  const selectedFiles = computed(() => references.value.map(r => files.value.find(f => f.materialId === r.materialId && f.version === r.version)).filter((f): f is ChatImportFile => !!f))
  const filteredLibrary = computed(() => library.value.filter(f => f.fileName.toLowerCase().includes(query.value.toLowerCase())))
  const pendingConsent = computed(() => batches.value.find(b => b.state === 'consent'))

  async function refresh(id = options.conversationId.value) {
    if (!id) return
    const epoch = generation
    try {
      const data = await chatImports.list(id)
      if (!alive || epoch !== generation || id !== options.conversationId.value) return
      batches.value = data.items
      references.value = data.selection.refs
      localOnly.value = data.selection.localOnly
      service.value = data.service
      loadError.value = ''
      const signature = JSON.stringify(data.items.map(b => [b.id, b.state, b.files.map(f => [f.id, f.state])]))
      if (signature !== lastSignature) {
        const refreshed = await options.refreshMessages(id)
        if (epoch !== generation) return
        if (refreshed) lastSignature = signature
      }
    } catch (error) {
      if (alive && epoch === generation) loadError.value = error instanceof Error ? error.message : '文件状态暂时无法同步'
    }
  }

  watch(options.conversationId, id => {
    generation++
    clearTimeout(timer)
    batches.value = []; references.value = []; service.value = null; localOnly.value = false
    consentRefs.value = null; previewOpen.value = false; lastSignature = ''; loadError.value = ''
    if (!uploading.value) { staged.value = []; requestId = crypto.randomUUID() }
    const epoch = generation
    async function poll() {
      await refresh(id)
      if (alive && epoch === generation && id) timer = setTimeout(poll, 2500)
    }
    if (id) void poll()
  }, { immediate: true })

  function stageFiles(input: FileList | File[]) {
    for (const file of Array.from(input)) {
      if (staged.value.length >= 5) { options.notify('每次最多发送 5 个文件'); break }
      const validation = validateImport(file.name, file.size)
      if (!file.size || validation.status !== 'ok') { options.notify(`${file.name}：${file.size ? validation.message : '文件为空'}`); continue }
      if (staged.value.some(f => f.file?.name === file.name && f.size === file.size && f.file.lastModified === file.lastModified)) continue
      staged.value.push({ id: crypto.randomUUID(), name: file.name, size: file.size, file })
    }
  }

  function stageMaterial(item: UploadResult) {
    if (staged.value.length >= 5) { options.notify('每次最多发送 5 个文件'); return }
    if (!staged.value.some(f => f.materialId === item.materialId)) staged.value.push({ id: crypto.randomUUID(), name: item.fileName, size: 0, materialId: item.materialId, version: item.versionNumber })
  }

  async function openPicker() {
    pickerOpen.value = true; libraryLoading.value = true; libraryError.value = ''
    try { library.value = (await api.listMaterials()).items }
    catch (e) { libraryError.value = e instanceof Error ? e.message : '资料列表读取失败' }
    finally { libraryLoading.value = false }
  }

  async function send(content: string, replyAssistance?: ReplyAssistanceInput) {
    if (uploading.value || !staged.value.length) return
    uploading.value = true
    const pending = [...staged.value]
    let id = ''
    try {
      id = await options.ensure()
      const batch = await chatImports.create(id, { requestId, content, replyAssistance, localOnly: localOnly.value, files: pending.map(({ file, ...metadata }) => metadata) })
      staged.value = []
      requestId = crypto.randomUUID()
      await refresh(id)
      for (const item of pending) {
        if (!item.file) continue
        try { await chatImports.upload(id, batch.id, item.id, item.file) }
        catch (e) {
          const message = e instanceof Error ? e.message : '上传失败'
          await chatImports.fail(id, batch.id, item.id, message).catch(() => undefined)
          options.notify(`${item.name}：${message}`)
        }
        await refresh(id)
      }
      await chatImports.seal(id, batch.id)
      await refresh(id)
    } catch (e) {
      options.notify(e instanceof Error ? e.message : '导入失败，可重试')
      if (!id || !batches.value.some(b => b.state === 'uploading')) staged.value = pending
    } finally { uploading.value = false }
  }

  async function chooseReferences(refs: ChatMaterialRef[]) {
    const id = options.conversationId.value
    if (!id) return
    try { await chatImports.select(id, refs, localOnly.value); references.value = refs }
    catch (e) { options.notify(e instanceof Error ? e.message : '参考文件更新失败') }
  }

  async function retry(batch: ChatImportBatch, fileId?: string) {
    busyBatch.value = batch.id
    try {
      if (fileId) await chatImports.retryFile(batch.conversationId, batch.id, fileId)
      else await chatImports.retry(batch.conversationId, batch.id)
      await refresh()
    }
    catch (e) { options.notify(e instanceof Error ? e.message : '暂时无法重试') }
    finally { busyBatch.value = null }
  }

  async function reupload(batch: ChatImportBatch, item: ChatImportFile, file: File) {
    busyBatch.value = batch.id
    try { await chatImports.upload(batch.conversationId, batch.id, item.id, file); await chatImports.seal(batch.conversationId, batch.id); await refresh() }
    catch (e) { options.notify(e instanceof Error ? e.message : '重传失败') }
    finally { busyBatch.value = null }
  }

  async function showConsent(refs?: ChatMaterialRef[]) {
    await refresh()
    consentRefs.value = refs || references.value
  }

  async function consent(onlyLocal: boolean) {
    const id = options.conversationId.value
    if (!id || !consentRefs.value?.length) return
    consentBusy.value = true
    try {
      await chatImports.consent(id, consentRefs.value, onlyLocal, service.value?.id)
      localOnly.value = onlyLocal; consentRefs.value = null
      await refresh()
    } catch (e) { options.notify(e instanceof Error ? e.message : '授权失败，请重新确认'); await refresh() }
    finally { consentBusy.value = false }
  }

  async function showPreview(ref: ChatMaterialRef, append = false) {
    const id = options.conversationId.value
    if (!id) return
    previewOpen.value = true; previewRef.value = ref; previewError.value = ''
    const offset = append && preview.value ? preview.value.offset + preview.value.text.length : 0
    if (!append) preview.value = null
    try {
      const result = await chatImports.preview(id, ref, offset)
      if (id !== options.conversationId.value || ref.materialId !== previewRef.value?.materialId || ref.version !== previewRef.value?.version) return
      preview.value = append && preview.value ? { ...result, offset: 0, text: preview.value.text + result.text } : result
    } catch (e) { previewError.value = e instanceof Error ? e.message : '暂时无法预览' }
  }

  function drop(e: DragEvent) {
    if (!e.dataTransfer?.types.includes('Files')) return
    e.preventDefault(); stageFiles(e.dataTransfer.files)
  }
  function paste(e: ClipboardEvent) {
    const images = Array.from(e.clipboardData?.files || []).filter(f => f.type.startsWith('image/'))
    if (images.length) { e.preventDefault(); stageFiles(images) }
  }
  onBeforeUnmount(() => { alive = false; generation++; clearTimeout(timer) })
  return { staged, batches, references, localOnly, service, uploading, pickerOpen, libraryLoading, libraryError, query, filteredLibrary,
    preview, previewRef, previewError, previewOpen, consentRefs, consentBusy, pendingConsent, busyBatch, loadError, selectedFiles, files,
    stageFiles, stageMaterial, openPicker, send, refresh, chooseReferences, retry, reupload, showConsent, consent, showPreview, drop, paste }
}
