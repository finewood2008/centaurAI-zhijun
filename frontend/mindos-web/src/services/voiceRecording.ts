import { requestProductMicrophone } from './productFiles.ts'
import { transportRequest } from './transport.ts'
import { onProductScopeReset, workspaceRequestSignal } from '../shared/productScope.ts'

export type VoiceState = 'idle' | 'requesting' | 'recording' | 'transcribing'
const MAX_SECONDS = 120
const MAX_ENCODED = 2 * 1024 * 1024
const SAMPLE_RATE = 16000
const aborted = () => new DOMException('录音已取消', 'AbortError')

/** Bounded PCM16 mono WAV. AudioContext resamples before this conversion. */
export function pcmWave(buffer: Pick<AudioBuffer, 'sampleRate' | 'numberOfChannels' | 'length' | 'getChannelData'>): Uint8Array<ArrayBuffer> {
  if (buffer.sampleRate !== SAMPLE_RATE || buffer.numberOfChannels < 1 || buffer.numberOfChannels > 2 || buffer.length < 1 || buffer.length > (MAX_SECONDS + 1) * SAMPLE_RATE) throw new Error('录音格式或时长超出限制，请录制 120 秒以内的音频。')
  // Recorder/codec padding may exceed the timer slightly; never send over 120 seconds.
  const length = Math.min(buffer.length, MAX_SECONDS * SAMPLE_RATE)
  const channels = Array.from({ length: buffer.numberOfChannels }, (_, index) => buffer.getChannelData(index))
  const bytes = new Uint8Array(44 + length * 2)
  const view = new DataView(bytes.buffer)
  const word = (offset: number, value: string) => { for (let i = 0; i < value.length; i++) view.setUint8(offset + i, value.charCodeAt(i)) }
  word(0, 'RIFF'); view.setUint32(4, bytes.length - 8, true); word(8, 'WAVE'); word(12, 'fmt ')
  view.setUint32(16, 16, true); view.setUint16(20, 1, true); view.setUint16(22, 1, true)
  view.setUint32(24, SAMPLE_RATE, true); view.setUint32(28, SAMPLE_RATE * 2, true); view.setUint16(32, 2, true); view.setUint16(34, 16, true)
  word(36, 'data'); view.setUint32(40, bytes.length - 44, true)
  let peak = 0
  for (let i = 0; i < length; i++) {
    let sample = 0
    for (const channel of channels) sample += channel[i]! / channels.length
    if (!Number.isFinite(sample)) throw new Error('录音内容无法识别，请重新录制。')
    sample = Math.max(-1, Math.min(1, sample)); peak = Math.max(peak, Math.abs(sample))
    view.setInt16(44 + i * 2, Math.round(sample * (sample < 0 ? 32768 : 32767)), true)
  }
  if (peak < 0.0001) throw new Error('没有检测到声音，请检查麦克风后重新录制。')
  return bytes
}

interface RecorderLike {
  state: string
  ondataavailable: ((event: { data: Blob }) => void) | null
  onstop: (() => void) | null
  onerror: (() => void) | null
  start(timeslice?: number): void
  stop(): void
}
interface VoiceDependencies {
  permission(): Promise<boolean>
  media(): Promise<MediaStream>
  recorder(stream: MediaStream): RecorderLike
  decode(data: ArrayBuffer, signal: AbortSignal): Promise<Pick<AudioBuffer, 'sampleRate' | 'numberOfChannels' | 'length' | 'getChannelData'>>
  transcribe(file: File, signal: AbortSignal): Promise<string>
}
const defaults: VoiceDependencies = {
  permission: requestProductMicrophone,
  media: () => navigator.mediaDevices.getUserMedia({ audio: true, video: false }),
  recorder: stream => {
    if (typeof MediaRecorder === 'undefined') throw new Error('此设备暂不能录音，可上传已有音频。')
    const mimeType = ['audio/webm;codecs=opus', 'audio/ogg;codecs=opus', 'audio/mp4'].find(value => MediaRecorder.isTypeSupported(value))
    return new MediaRecorder(stream, { ...(mimeType ? { mimeType } : {}), audioBitsPerSecond: 32000 }) as RecorderLike
  },
  async decode(data, signal) {
    const context = new AudioContext({ sampleRate: SAMPLE_RATE })
    const close = () => { void context.close().catch(() => {}) }
    signal.addEventListener('abort', close, { once: true })
    try { signal.throwIfAborted(); return await context.decodeAudioData(data) }
    finally { signal.removeEventListener('abort', close); close() }
  },
  async transcribe(file, signal) {
    const form = new FormData(); form.append('file', file)
    const response = await transportRequest('/api/mindos/voice/transcribe', { method: 'POST', body: form, signal })
    if (!response.ok) throw new Error(response.status === 503 ? '盒子语音转写暂不可用，请稍后重试或输入文字。' : '盒子未完成语音转写，请重试或输入文字。')
    const result = await response.json()
    if (!result || typeof result.text !== 'string' || !result.text.trim()) throw new Error('没有识别出文字，请重新录制或输入文字。')
    if (result.text.length > 16000) throw new Error('识别结果超出长度限制，请分段录制。')
    return result.text.trim()
  },
}

/** No capture until start() is explicitly called; every resource belongs to one workspace lifetime. */
export function createVoiceRecording(options: { onState(state: VoiceState): void; onText(text: string): void; onError(message: string): void }, overrides: Partial<VoiceDependencies> = {}) {
  const deps = { ...defaults, ...overrides }
  interface Session { lifetime: ReturnType<typeof workspaceRequestSignal>; stream?: MediaStream; recorder?: RecorderLike; chunks: Blob[]; size: number; timer?: ReturnType<typeof setTimeout>; stopped?: Promise<void>; stopResolve?: () => void; finishing?: Promise<void> }
  let session: Session | null = null
  let disposed = false
  const state = (value: VoiceState) => { if (!disposed) options.onState(value) }
  const stopTracks = (current: Session) => { current.stream?.getTracks().forEach(track => track.stop()); current.stream = undefined }
  function release(current: Session) {
    clearTimeout(current.timer); stopTracks(current)
    if (current.recorder) { current.recorder.ondataavailable = null; current.recorder.onstop = null; current.recorder.onerror = null }
    current.chunks = []; current.size = 0; current.stopResolve?.(); current.lifetime.dispose()
  }
  function cancel() {
    const current = session
    if (!current) return
    session = null; current.lifetime.abort()
    if (current.recorder?.state === 'recording') { try { current.recorder.stop() } catch { /* Already stopped. */ } }
    release(current); state('idle')
  }
  const reset = onProductScopeReset(cancel)
  function fail(current: Session, error: unknown) {
    if (session !== current) return
    const report = !current.lifetime.signal.aborted
    cancel()
    if (report && !disposed) options.onError(error instanceof Error && error.name === 'NotAllowedError' ? '麦克风权限被拒绝，可改用文字或上传已有音频。' : error instanceof Error ? error.message : '录音未完成，请重试。')
  }
  async function start(): Promise<void> {
    if (disposed || session) return
    const current: Session = { lifetime: workspaceRequestSignal(), chunks: [], size: 0 }
    session = current; state('requesting')
    current.lifetime.signal.addEventListener('abort', () => { if (session === current) cancel() }, { once: true })
    try {
      if (!await deps.permission()) throw new Error('麦克风权限被拒绝，可改用文字或上传已有音频。')
      current.lifetime.signal.throwIfAborted()
      const stream = await deps.media()
      if (session !== current || current.lifetime.signal.aborted) { stream.getTracks().forEach(track => track.stop()); throw aborted() }
      current.stream = stream
      // A mono input keeps the decoded 120-second buffer below the memory budget.
      await Promise.all(stream.getAudioTracks().map(track => track.applyConstraints?.({ channelCount: 1 })))
      current.lifetime.signal.throwIfAborted()
      const recorder = deps.recorder(stream); current.recorder = recorder
      current.stopped = new Promise(resolve => { current.stopResolve = resolve })
      recorder.onstop = () => current.stopResolve?.()
      recorder.onerror = () => fail(current, new Error('录音设备停止工作，请重试或上传已有音频。'))
      recorder.ondataavailable = event => {
        if (session !== current || !event.data.size) return
        current.size += event.data.size
        if (current.size > MAX_ENCODED) { fail(current, new Error('录音超出大小限制，请分段录制。')); return }
        current.chunks.push(event.data)
      }
      recorder.start(500); state('recording')
      current.timer = setTimeout(() => { void finish() }, MAX_SECONDS * 1000)
    } catch (error) { fail(current, error) }
  }
  function finish(): Promise<void> {
    const current = session
    if (!current || !current.recorder) return Promise.resolve()
    if (current.finishing) return current.finishing
    current.finishing = (async () => {
      try {
        clearTimeout(current.timer); state('transcribing')
        current.recorder!.stop(); stopTracks(current)
        await current.stopped
        current.lifetime.signal.throwIfAborted()
        if (!current.size) throw new Error('没有录到声音，请重新录制。')
        let encoded: Blob | null = new Blob(current.chunks, { type: current.chunks[0]?.type || 'audio/webm' })
        current.chunks = []
        const data = await encoded.arrayBuffer(); encoded = null
        current.lifetime.signal.throwIfAborted()
        const audio = await deps.decode(data, current.lifetime.signal)
        current.lifetime.signal.throwIfAborted()
        const wav = pcmWave(audio)
        const file = new File([wav], 'voice-input.wav', { type: 'audio/wav' })
        const text = await deps.transcribe(file, current.lifetime.signal)
        if (session !== current) return
        current.lifetime.signal.throwIfAborted()
        options.onText(text)
        session = null; release(current); state('idle')
      } catch (error) { fail(current, error) }
    })()
    return current.finishing
  }
  return { start, finish, cancel, dispose() { cancel(); disposed = true; reset() } }
}
