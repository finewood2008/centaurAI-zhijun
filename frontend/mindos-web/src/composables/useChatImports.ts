import { computed, onBeforeUnmount, ref, watch, type Ref } from 'vue'
import { api, chatImports, type ChatImportBatch, type ChatImportFile, type ChatMaterialRef, type ChatFileService, type ChatFilePreview, type UploadResult } from '@/services/api'
import { validateImport } from '@/features/import/validation'
import type { ReplyAssistanceInput } from '@/shared/replyAssistance'
import { createChatImportPoller, hasTransitionalImports } from './chatImportPolling'
import { askRag, requiresFreshRagSearch, submitRagDecision } from '@/services/taskRouting'
import { isDesktopProduct } from '@/shared/productScope'

export interface StagedChatFile { id: string; name: string; size: number; file?: File; materialId?: string; version?: number }

export function useChatImports(options: {
  conversationId: Ref<string | null | undefined>
  ensure: () => Promise<string>
  refreshMessages: (id: string) => Promise<boolean>
  notify: (message: string) => void
}) {
  const staged = ref<StagedChatFile[]>([])
  const retrievalOnly = ref(isDesktopProduct())
  function importBlocked() {
    if (!retrievalOnly.value) return false
    options.notify('请在「资料与边界 → 原材料」导入，Data Engine 处理完成后回到对话检索并确认使用。')
    return true
  }
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
  const ragBusyBatch = ref<string | null>(null)
  const loadError = ref('')
  let requestId = crypto.randomUUID()

  const files = computed(() => batches.value.flatMap(b => b.files))
  const selectedFiles = computed(() => references.value.map(r => files.value.find(f => f.materialId === r.materialId && f.version === r.version)).filter((f): f is ChatImportFile => !!f))
  const filteredLibrary = computed(() => library.value.filter(f => f.fileName.toLowerCase().includes(query.value.toLowerCase())))
  const pendingConsent = computed(() => batches.value.find(b => b.state === 'consent'))

  const poller = createChatImportPoller({
    read: id => chatImports.list(id),
    apply: data => {
      batches.value = data.items
      retrievalOnly.value = isDesktopProduct() || data.retrievalOnly === true || data.uploadEnabled === false
      references.value = retrievalOnly.value ? [] : data.selection.refs
      localOnly.value = data.selection.localOnly
      service.value = data.service
    },
    signature: data => JSON.stringify(data.items.map(b => [b.id, b.state, b.files.map(f => [f.id, f.state])])),
    isTransitional: data => !retrievalOnly.value && hasTransitionalImports(data.items),
    refreshMessages: options.refreshMessages,
    isTargetCurrent: id => options.conversationId.value === id,
    onSuccess: () => {
      loadError.value = ''
    },
    onError: error => {
      loadError.value = error instanceof Error ? error.message : '文件状态暂时无法同步'
    },
  })

  async function refresh(id = options.conversationId.value) {
    if (id && id === options.conversationId.value) await poller.refresh(id)
  }

  watch(options.conversationId, id => {
    poller.stop()
    batches.value = []; references.value = []; service.value = null; localOnly.value = false
    consentRefs.value = null; previewOpen.value = false; loadError.value = ''
    if (!uploading.value) { staged.value = []; requestId = crypto.randomUUID() }
    if (id) void poller.start(id)
  }, { immediate: true })

  function stageFiles(input: FileList | File[]) {
    if (importBlocked()) return
    for (const file of Array.from(input)) {
      if (staged.value.length >= 5) { options.notify('每次最多发送 5 个文件'); break }
      const validation = validateImport(file.name, file.size)
      if (!file.size || validation.status !== 'ok') { options.notify(`${file.name}：${file.size ? validation.message : '文件为空'}`); continue }
      if (staged.value.some(f => f.file?.name === file.name && f.size === file.size && f.file.lastModified === file.lastModified)) continue
      staged.value.push({ id: crypto.randomUUID(), name: file.name, size: file.size, file })
    }
  }

  function stageMaterial(item: UploadResult) {
    if (importBlocked()) return
    if (staged.value.length >= 5) { options.notify('每次最多发送 5 个文件'); return }
    if (!staged.value.some(f => f.materialId === item.materialId)) staged.value.push({ id: crypto.randomUUID(), name: item.fileName, size: 0, materialId: item.materialId, version: item.versionNumber })
  }

  async function openPicker() {
    if (importBlocked()) return
    pickerOpen.value = true; libraryLoading.value = true; libraryError.value = ''
    try { library.value = (await api.listMaterials()).items }
    catch (e) { libraryError.value = e instanceof Error ? e.message : '资料列表读取失败' }
    finally { libraryLoading.value = false }
  }

  async function send(content: string, replyAssistance?: ReplyAssistanceInput, forceLocalOnly = false): Promise<boolean> {
    if (importBlocked()) return false
    if (uploading.value || !staged.value.length) return false
    uploading.value = true
    const pending = [...staged.value]
    let id = ''
    let accepted = false
    try {
      id = await options.ensure()
      const batch = await chatImports.create(id, { requestId, content, replyAssistance, localOnly: forceLocalOnly || localOnly.value, files: pending.map(({ file, ...metadata }) => metadata) })
      accepted = true
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
      return true
    } catch (e) {
      options.notify(e instanceof Error ? e.message : '导入失败，可重试')
      // Once create() succeeds, the server owns the batch and request id. Do not
      // restore it as a fresh composer submission or a retry could duplicate it.
      if (!accepted && (!id || !batches.value.some(b => b.state === 'uploading'))) staged.value = pending
      return accepted
    } finally { uploading.value = false }
  }

  async function chooseReferences(refs: ChatMaterialRef[]) {
    if (importBlocked()) return
    const id = options.conversationId.value
    if (!id) return
    try { await chatImports.select(id, refs, localOnly.value); references.value = refs }
    catch (e) { options.notify(e instanceof Error ? e.message : '参考文件更新失败') }
  }

  async function retry(batch: ChatImportBatch, fileId?: string) {
    if (importBlocked()) return
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
    if (importBlocked()) return
    busyBatch.value = batch.id
    try { await chatImports.upload(batch.conversationId, batch.id, item.id, file); await chatImports.seal(batch.conversationId, batch.id); await refresh() }
    catch (e) { options.notify(e instanceof Error ? e.message : '重传失败') }
    finally { busyBatch.value = null }
  }

  async function showConsent(refs?: ChatMaterialRef[]) {
    if (importBlocked()) return
    await refresh()
    consentRefs.value = refs || references.value
  }

  async function consent(onlyLocal: boolean) {
    if (importBlocked()) return
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

  async function confirmSensitive(batch: ChatImportBatch) {
    if (importBlocked()) return
    if (!batch.ragV2 || ragBusyBatch.value) return
    ragBusyBatch.value = batch.id
    try {
      const action = await askRag(batch.ragV2)
      try { await submitRagDecision(batch.conversationId, batch.ragV2, action) }
      catch (error) {
        if (!requiresFreshRagSearch(error)) throw error
      }
      await refresh(batch.conversationId)
    } catch (e) {
      options.notify(e instanceof Error ? e.message : '敏感资料确认失败，请重试')
      await refresh(batch.conversationId)
    } finally {
      ragBusyBatch.value = null
    }
  }

  async function showPreview(ref: ChatMaterialRef, append = false) {
    if (importBlocked()) return
    const id = options.conversationId.value
    if (!id) return
    previewOpen.value = true; previewRef.value = ref; previewError.value = ''
    const offset = append && preview.value ? preview.value.offset + preview.value.text.length : 0
    if (!append) preview.value = null
    for (let attempt = 0; attempt < 4; attempt++) {
      try {
        const result = await chatImports.preview(id, ref, offset)
        if (id !== options.conversationId.value || ref.materialId !== previewRef.value?.materialId || ref.version !== previewRef.value?.version) return
        preview.value = append && preview.value ? { ...result, offset: 0, text: preview.value.text + result.text } : result
        return
      } catch (e) {
        const prompt = e && typeof e === 'object' ? (e as { ragV2?: import('@/services/taskRouting').RagV2Prompt }).ragV2 : undefined
        if (!prompt) { previewError.value = e instanceof Error ? e.message : '暂时无法预览'; return }
        const action = await askRag(prompt)
        try { await submitRagDecision(id, prompt, action) }
        catch (decisionError) {
          if (requiresFreshRagSearch(decisionError)) continue
          previewError.value = decisionError instanceof Error ? decisionError.message : '敏感资料确认失败，请重试'
          return
        }
        if (action === 'cancel') { previewError.value = '已取消领取资料片段'; return }
        if (action === 'without-materials') { previewOpen.value = false; return }
      }
    }
    previewError.value = '资料状态持续变化，请稍后重新预览'
  }

  function drop(e: DragEvent) {
    if (!e.dataTransfer?.types.includes('Files')) return
    e.preventDefault(); stageFiles(e.dataTransfer.files)
  }
  function paste(e: ClipboardEvent) {
    const images = Array.from(e.clipboardData?.files || []).filter(f => f.type.startsWith('image/'))
    if (images.length) { e.preventDefault(); stageFiles(images) }
  }
  onBeforeUnmount(() => poller.dispose())
  return { retrievalOnly, staged, batches, references, localOnly, service, uploading, pickerOpen, libraryLoading, libraryError, query, filteredLibrary,
    preview, previewRef, previewError, previewOpen, consentRefs, consentBusy, pendingConsent, busyBatch, ragBusyBatch, loadError, selectedFiles, files,
    stageFiles, stageMaterial, openPicker, send, refresh, chooseReferences, retry, reupload, showConsent, consent, confirmSensitive, showPreview, drop, paste }
}
