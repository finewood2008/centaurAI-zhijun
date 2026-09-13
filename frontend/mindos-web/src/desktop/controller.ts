import type {
  CallContext, DesktopSnapshot, DeviceSummary, MaterialStatus, MaterialType,
  MaterialsPage, MaterialsQuery, PasswordCredentials, PublicError, Result, ZhijunDesktopV1,
  PasswordResetCredentials, RememberedLogin, RegistrationCredentials,
} from '../../../shared/desktop-contract'

export interface DesktopViewState {
  readonly snapshot: DesktopSnapshot | null
  readonly hostAvailable: boolean
  readonly devices: readonly DeviceSummary[]
  readonly devicesLoading: boolean
  readonly page: MaterialsPage | null
  readonly query: MaterialsQuery
  readonly loading: boolean
  readonly controlPending: boolean
  readonly pendingOperation: 'beginSignIn' | 'signInWithPassword' | 'signInWithSavedPassword' | 'sendRegistrationCode'
    | 'resetPassword' | 'registerWithPassword' | 'claimDevice' | 'openProvisioning' | 'disconnect' | 'signOut' | 'connect' | null
  readonly error: PublicError | null
  readonly notice: string
}

const initialQuery = (): MaterialsQuery => ({ limit: 20, offset: 0 })
const unavailable = (): PublicError => ({
  code: 'TRANSPORT_UNAVAILABLE', message: '桌面连接暂时不可用，请重新打开知君桌面应用。', recovery: 'none',
})

/** Renderer state only. It owns no credentials, network transport or persistent cache. */
export class DesktopController {
  state: DesktopViewState
  private observer: ((state: DesktopViewState) => void) | undefined
  private unsubscribe: (() => void) | undefined
  private disposed = false
  private started = false
  private readRevision = 0
  private deviceRevision = 0
  private controlRevision = 0
  private activeRead: CallContext | null = null
  private readonly bridge: ZhijunDesktopV1 | undefined

  private readonly options: { materials?: boolean }

  constructor(bridge: ZhijunDesktopV1 | undefined, options: { materials?: boolean } = {}) {
    this.options = options
    this.bridge = bridge
    this.state = {
      snapshot: null, hostAvailable: bridge?.protocolVersion === 1,
      devices: [], devicesLoading: false, page: null, query: initialQuery(),
      loading: false, controlPending: false, pendingOperation: null, error: null, notice: '',
    }
  }

  observe(observer: (state: DesktopViewState) => void): () => void {
    this.observer = observer
    observer(this.state)
    return () => { if (this.observer === observer) this.observer = undefined }
  }

  private patch(partial: Partial<DesktopViewState>): void {
    if (this.disposed) return
    this.state = { ...this.state, ...partial }
    this.observer?.(this.state)
  }

  private context(): CallContext {
    return { callId: crypto.randomUUID(), expectedGeneration: this.state.snapshot?.generation ?? 0 }
  }

  async start(): Promise<void> {
    if (this.started || this.disposed || !this.state.hostAvailable || !this.bridge) return
    this.started = true
    try {
      // A newer event may arrive while getSnapshot is pending.
      this.unsubscribe = this.bridge.subscribe(snapshot => this.acceptSnapshot(snapshot))
      const result = await this.bridge.getSnapshot()
      if (result.ok) this.acceptSnapshot(result.data)
      else if (!this.state.snapshot) this.acceptError(result)
    } catch { if (!this.state.snapshot) this.patch({ error: unavailable() }) }
  }

  private acceptSnapshot(snapshot: DesktopSnapshot): void {
    if (this.disposed) return
    const previous = this.state.snapshot
    if (previous && snapshot.sequence <= previous.sequence) return
    if (previous && snapshot.generation < previous.generation) return
    const changedScope = !previous || snapshot.generation !== previous.generation
    if (changedScope) {
      this.readRevision++
      this.deviceRevision++
      this.activeRead = null
      this.patch({ page: null, devices: [], loading: false, devicesLoading: false,
        query: initialQuery(), error: null, notice: '' })
    }
    this.patch({ snapshot, error: snapshot.phase === 'failed' ? snapshot.error ?? null : null })
    if (snapshot.phase !== 'ready') this.patch({ page: null })
    if (snapshot.phase === 'selecting_device' && (changedScope || previous?.phase !== snapshot.phase)) {
      void this.loadDevices()
    }
    if (this.options.materials !== false && snapshot.phase === 'ready' && (changedScope || previous?.phase !== 'ready')) {
      void this.readPage()
    }
  }

  private acceptError(result: Result<unknown>): void {
    if (!result.ok && !this.disposed && result.generation === (this.state.snapshot?.generation ?? 0)) {
      if (result.error.code !== 'STALE_GENERATION' && result.error.code !== 'READ_CANCELLED') {
        this.patch({ error: result.error })
      }
    }
  }

  async control(operation: 'beginSignIn' | 'signInWithPassword' | 'signInWithSavedPassword' | 'registerWithPassword' | 'disconnect' | 'signOut' | 'connect', input?: string | boolean | PasswordCredentials | RegistrationCredentials): Promise<void> {
    if (!this.bridge || this.disposed || !this.state.snapshot) return
    // Cancelling a pending connection uses the existing host generation fence.
    // Do not let disconnect supersede login, sign-out or another disconnect.
    const cancelsConnection = operation === 'disconnect' && this.state.pendingOperation === 'connect'
      && ['connecting', 'authorizing'].includes(this.state.snapshot.phase)
    if (this.state.controlPending && !cancelsConnection
      && (operation !== 'signOut' || this.state.pendingOperation === 'signOut')) return
    const revision = ++this.controlRevision
    const generation = this.state.snapshot.generation
    this.patch({ controlPending: true, pendingOperation: operation, error: null, notice: '' })
    try {
      const context = this.context()
      const result = operation === 'connect'
        ? await this.bridge.connect(context, typeof input === 'string' ? input : '')
        : operation === 'signInWithPassword'
          ? await this.bridge.signInWithPassword(context, input as PasswordCredentials)
          : operation === 'registerWithPassword'
            ? await this.bridge.registerWithPassword(context, input as RegistrationCredentials)
          : operation === 'signInWithSavedPassword'
            ? await this.bridge.signInWithSavedPassword(context, input !== false)
          : await this.bridge[operation](context)
      if (this.disposed || revision !== this.controlRevision) return
      if (result.ok) this.acceptSnapshot(result.data)
      else this.acceptError(result)
    } catch {
      if (revision === this.controlRevision && generation === this.state.snapshot?.generation) {
        this.patch({ error: unavailable() })
      }
    } finally {
      if (revision === this.controlRevision) this.patch({ controlPending: false, pendingOperation: null })
    }
  }

  async sendRegistrationCode(phone: string): Promise<number | null> {
    if (!this.bridge || this.disposed || !this.state.snapshot || this.state.controlPending) return null
    const revision = ++this.controlRevision
    const generation = this.state.snapshot.generation
    this.patch({ controlPending: true, pendingOperation: 'sendRegistrationCode', error: null, notice: '' })
    try {
      const result = await this.bridge.sendRegistrationCode(this.context(), phone)
      if (this.disposed || revision !== this.controlRevision || generation !== this.state.snapshot?.generation) return null
      if (!result.ok) { this.acceptError(result); return null }
      this.patch({ notice: `验证码已发送，请查看手机短信；验证码 ${result.data.expiresIn} 秒内有效。` })
      return result.data.expiresIn
    } catch {
      if (revision === this.controlRevision && generation === this.state.snapshot?.generation) this.patch({ error: unavailable() })
      return null
    } finally {
      if (revision === this.controlRevision) this.patch({ controlPending: false, pendingOperation: null })
    }
  }

  async resetPassword(credentials: PasswordResetCredentials): Promise<boolean> {
    const snapshot = this.state.snapshot
    if (!this.bridge || this.disposed || !snapshot || this.state.controlPending
      || snapshot.subject || !['signed_out', 'failed'].includes(snapshot.phase)) return false
    const revision = ++this.controlRevision
    const generation = snapshot.generation
    this.patch({ controlPending: true, pendingOperation: 'resetPassword', error: null, notice: '' })
    try {
      const result = await this.bridge.resetPassword(this.context(), credentials)
      if (this.disposed || revision !== this.controlRevision || generation !== this.state.snapshot?.generation) return false
      if (!result.ok) { this.acceptError(result); return false }
      if (result.generation !== generation || result.data.processed !== true) return false
      this.patch({ notice: '密码重置请求已处理，请使用新密码登录' })
      return true
    } catch {
      if (revision === this.controlRevision && generation === this.state.snapshot?.generation) {
        this.patch({ error: unavailable() })
      }
      return false
    } finally {
      if (revision === this.controlRevision) this.patch({ controlPending: false, pendingOperation: null })
    }
  }

  async claimDevice(claimToken: string): Promise<boolean> {
    if (!this.bridge || this.disposed || !this.state.snapshot || this.state.controlPending) return false
    const revision = ++this.controlRevision
    const generation = this.state.snapshot.generation
    let claimed: DeviceSummary | null = null
    this.patch({ controlPending: true, pendingOperation: 'claimDevice', error: null, notice: '' })
    try {
      const result = await this.bridge.claimDevice(this.context(), claimToken)
      if (this.disposed || revision !== this.controlRevision || generation !== this.state.snapshot?.generation) return false
      if (!result.ok) { this.acceptError(result); return false }
      claimed = result.data
      this.patch({ devices: [...this.state.devices.filter(device => device.deviceId !== claimed?.deviceId), claimed],
        notice: `已认领盒子“${claimed.displayName}”，正在刷新设备列表。` })
    } catch {
      if (revision === this.controlRevision && generation === this.state.snapshot?.generation) this.patch({ error: unavailable() })
      return false
    } finally {
      if (revision === this.controlRevision) this.patch({ controlPending: false, pendingOperation: null })
    }
    if (claimed) await this.loadDevices()
    return Boolean(claimed)
  }

  async openProvisioning(): Promise<boolean> {
    const snapshot = this.state.snapshot
    if (!this.bridge || this.disposed || !snapshot || this.state.controlPending
      || !snapshot.subject?.accountId || !snapshot.capabilities.provisioning
      || !['selecting_device', 'failed'].includes(snapshot.phase)) return false
    const revision = ++this.controlRevision
    const generation = snapshot.generation
    this.patch({ controlPending: true, pendingOperation: 'openProvisioning', error: null, notice: '' })
    try {
      const result = await this.bridge.openProvisioning(this.context())
      if (this.disposed || revision !== this.controlRevision || generation !== this.state.snapshot?.generation) return false
      if (!result.ok) { this.acceptError(result); return false }
      if (result.generation !== generation || result.data.opened !== true) return false
      this.patch({ notice: '已打开盒子配网窗口。请在独立窗口完成设置，返回后刷新已绑定盒子。' })
      return true
    } catch {
      if (revision === this.controlRevision && generation === this.state.snapshot?.generation) {
        this.patch({ error: unavailable() })
      }
      return false
    } finally {
      if (revision === this.controlRevision) this.patch({ controlPending: false, pendingOperation: null })
    }
  }

  async getRememberedLogin(): Promise<RememberedLogin | null> {
    if (!this.bridge || this.disposed || !this.state.snapshot) return null
    const generation = this.state.snapshot.generation
    try {
      const result = await this.bridge.getRememberedLogin(this.context())
      if (this.disposed || generation !== this.state.snapshot?.generation) return null
      return result.ok && result.generation === generation ? result.data : null
    } catch { return null }
  }

  async loadDevices(): Promise<void> {
    if (!this.bridge || this.disposed || this.state.snapshot?.phase !== 'selecting_device') return
    const context = this.context()
    const revision = ++this.deviceRevision
    this.patch({ devicesLoading: true, error: null })
    const current = () => !this.disposed && revision === this.deviceRevision
      && context.expectedGeneration === this.state.snapshot?.generation
      && this.state.snapshot.phase === 'selecting_device'
    try {
      const result = await this.bridge.listDevices(context)
      if (!current()) return
      if (result.ok && result.generation === context.expectedGeneration) this.patch({ devices: result.data })
      else this.acceptError(result)
    } catch { if (current()) this.patch({ error: unavailable() }) }
    finally { if (current()) this.patch({ devicesLoading: false }) }
  }

  async setFilters(filters: { keyword: string; type: MaterialType | ''; status: MaterialStatus | '' }): Promise<void> {
    const keyword = filters.keyword.trim()
    if (keyword.length > 100) {
      this.patch({ error: { code: 'INVALID_REQUEST', message: '搜索词最多 100 个字符。', recovery: 'none' } })
      return
    }
    this.patch({ query: { ...initialQuery(), ...(keyword ? { keyword } : {}),
      ...(filters.type ? { type: filters.type } : {}), ...(filters.status ? { status: filters.status } : {}) } })
    await this.readPage()
  }

  async changePage(direction: -1 | 1): Promise<void> {
    if (!this.state.page || this.state.loading) return
    const offset = this.state.query.offset + direction * this.state.query.limit
    if (offset < 0 || offset > 10000 || (direction === 1 && !this.state.page.hasMore)) return
    this.patch({ query: { ...this.state.query, offset } })
    await this.readPage()
  }

  private suppressRead(): void {
    const previous = this.activeRead
    this.activeRead = null
    this.readRevision++
    if (previous && this.bridge && previous.expectedGeneration === this.state.snapshot?.generation) {
      void this.bridge.cancelRead(this.context(), previous.callId).catch(() => { /* No raw errors enter UI. */ })
    }
  }

  cancelRead(): void {
    this.suppressRead()
    this.patch({ loading: false, notice: '已停止接收本次结果；远端请求可能仍在执行。' })
  }

  async readPage(): Promise<void> {
    if (!this.bridge || this.disposed || this.state.snapshot?.phase !== 'ready') return
    this.suppressRead()
    const revision = this.readRevision
    const context = this.context()
    const query = this.state.query
    this.activeRead = context
    this.patch({ page: null, loading: true, error: null, notice: '' })
    const current = () => !this.disposed && revision === this.readRevision
      && context.expectedGeneration === this.state.snapshot?.generation
      && this.state.snapshot.phase === 'ready'
    try {
      const result = await this.bridge.materials.list(context, query)
      if (!current()) return
      if (result.ok && result.generation === context.expectedGeneration) this.patch({ page: result.data })
      else this.acceptError(result)
    } catch { if (current()) this.patch({ error: unavailable() }) }
    finally {
      if (current()) { this.activeRead = null; this.patch({ loading: false }) }
    }
  }

  dispose(): void {
    this.suppressRead()
    this.disposed = true
    this.deviceRevision++
    this.controlRevision++
    this.unsubscribe?.()
    this.observer = undefined
    // Window/view disposal does not disconnect the shared main-process session.
  }
}
