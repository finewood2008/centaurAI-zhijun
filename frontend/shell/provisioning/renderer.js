'use strict'

const api = window.desktopProvisioning
const testBuild = api?.testBuild === true
const formalMode = !testBuild
const available = Boolean(api?.available)
const element = id => document.getElementById(id)
const result = element('result')
let selectedTransportId = null
let busy = false
let scanning = false
let connecting = false
let connected = false
let operationEpoch = 0
let formalState = 'idle'
let formalSessionActive = false
let formalRestartRequired = false
let hasPreview = false
let physicalConfirmed = false
let discoveryState = 'idle'
let discoveryCandidates = []
const liveDiscovery = formalMode && typeof api?.onDiscoverySnapshot === 'function'

const ERROR_MESSAGES = Object.freeze({
  PROVISIONING_FAILED: '配网操作未完成，请重试。',
  PROVISIONING_ENCRYPTION_UNAVAILABLE: '当前盒子固件未提供可验证的加密配网会话，已拒绝发送 Wi-Fi 密码。',
  PROVISIONING_BLUETOOTH_UNAVAILABLE: '当前系统或蓝牙适配器不支持此配网方式。',
  PROVISIONING_USER_GESTURE_REQUIRED: '请直接点击“扫描附近盒子”重试。',
  PROVISIONING_SCAN_IN_PROGRESS: '正在扫描，请先完成或取消当前选择。',
  PROVISIONING_SCAN_CANCELLED: '已取消扫描。',
  PROVISIONING_SCAN_TIMEOUT: '本轮未发现可配网的盒子。请确认盒子已进入配网模式、电脑蓝牙已开启，然后重新扫描。',
  DISCOVERY_RUNTIME_FAILURE: '蓝牙扫描未能启动或已中断。请检查系统蓝牙与知君的蓝牙权限后重试。（DISCOVERY_RUNTIME_FAILURE）',
  CLAIM_OPERATION_IN_PROGRESS: '当前扫描或配网尚未结束，请先取消再重试。',
  LOCAL_SELECTION_LEASE_INVALID: '本次设备选择已过期，请重新扫描后选择盒子。',
  LOCAL_CONNECT_TIMEOUT: '连接盒子超时，请靠近盒子并确认它仍处于配网模式。',
  LOCAL_TRANSPORT_DISCONNECTED: '与盒子的蓝牙连接已断开，请重新扫描。',
  LOCAL_DEVICE_INCOMPATIBLE: '盒子仍使用旧版配网协议，当前知君要求 V2 加密配网。请先升级盒端配网组件并核对交付配置；重复扫描无法解决。',
  LOCAL_DEVICE_INFO_INVALID: '盒子的设备信息无效或不完整，无法安全确认设备。请检查盒端配网服务。',
  PROTOCOL_CHANGED: '盒子返回的配网数据不符合当前安全协议，已停止连接。请核对盒端与知君的协议版本。',
  CLAIM_INVALID_STATE: '本次配网会话已结束，请关闭“添加 AI 盒子”窗口后重新打开。',
  CLAIM_UNKNOWN_ERROR: '建立配网会话时发生未识别的错误，请关闭“添加 AI 盒子”窗口后重试；若仍失败，请检查盒端日志。',
  GATT_SERVICE_NOT_FOUND: '盒子未提供所需配网服务，请确认配网模式或更新盒端程序。',
  GATT_CHARACTERISTIC_MISMATCH: '盒子的配网协议不匹配，请更新盒端程序。',
  DEVICE_NOT_ENROLLED: '盒子尚未完成设备证书登记，请联系管理员完成交付配置。',
  DEVICE_ALREADY_OWNED: '这台盒子已有归属，不能作为新盒子再次认领。',
  DEVICE_NOT_CLAIMABLE: '这台盒子当前不能认领，请核对交付状态。',
  SETUP_WINDOW_CLOSED: '盒子的配网窗口已关闭，请按设备说明重新进入配网模式。',
  PROVISIONING_INVALID_WIFI_CREDENTIALS: '请检查 Wi-Fi 名称、安全类型和密码。',
  PROVISIONING_LEGACY_CONFIRMATION_REQUIRED: '配网协议 v1 的明文兼容模式需要再次确认。',
  PROVISIONING_STATUS_TIMEOUT: '盒子尚未确认联网结果，请检查路由器和盒子状态。',
  PROVISIONING_AUTHENTICATION_FAILED: '无法认证这台盒子，请重新进入配网模式后再试。',
  PROVISIONING_WIFI_AUTHENTICATION_FAILED: '盒子未能连接 Wi-Fi，请检查网络名称和密码。',
  PROVISIONING_OWNERSHIP_CONFLICT: '这台盒子暂时无法绑定到当前账号，请联系管理员处理。',
  PROVISIONING_CLOUD_UNAVAILABLE: '盒子暂时无法连接服务，请检查网络后重试。',
  UNSUPPORTED_NETWORK_SECURITY: '当前网络安全类型不受支持，请选择开放网络或 WPA/WPA2 个人网络。',
  HELLO_CONTEXT_EXPIRED: '盒子的安全会话已过期，请重新扫描并核对设备短码。',
  REQUESTED_OPS_MISMATCH: '盒子固件与当前知君版本的安全能力不匹配，请先更新盒子固件。',
  PROVISIONING_CANCELLED: '已取消本次配网。',
  PROVISIONING_NOT_STARTED: '请先扫描并确认眼前的盒子。',
  PROVISIONING_DEVICE_REJECTED: '盒子未能完成操作，请检查状态后重试。',
  NotFoundError: '未选择盒子或扫描已取消。',
  SecurityError: '蓝牙权限未授予，请到系统设置中允许知君使用蓝牙。',
  NotAllowedError: '蓝牙访问未获允许，请在系统设置的隐私与安全性中检查知君的蓝牙权限。',
})

const FORMAL_STATES = Object.freeze({
  idle: ['准备开始', '打开盒子的配网模式，然后扫描附近盒子。'],
  authenticating: ['正在认证盒子', '请核对设备信息与眼前盒子的固定设备短码，然后确认。'],
  awaitingWifi: ['可以设置 Wi-Fi', '网络密码只在加密会话中发送，提交后会立即从页面清除。'],
  awaitingOwnershipConfirmation: ['等待确认绑定', '确认后，这台盒子会绑定到当前知君账号。'],
  waitingCloud: ['正在等待盒子上线', '盒子正在连接网络和服务，请保持通电。'],
  attentionRequired: ['需要处理后继续', '请根据安全提示检查盒子或网络，然后重试。'],
  completed: ['盒子已添加', '盒子已经联网并绑定，可以返回知君主窗口。'],
  cancelled: ['已取消本次配网', '本次安全会话已结束。'],
})

function show(message, error = false) {
  result.textContent = message
  result.classList.toggle('error', error)
}

function errorCode(error) {
  // Electron's isolated bridge can preserve only Error.message. Accept exact
  // allowlisted codes, never arbitrary native/server error text.
  for (const candidate of [error?.code, error?.name, error?.message]) {
    if (typeof candidate === 'string' && Object.hasOwn(ERROR_MESSAGES, candidate)) return candidate
  }
  return 'PROVISIONING_FAILED'
}

function safeError(error) {
  return ERROR_MESSAGES[errorCode(error)]
}

function safeAttention(code) {
  return typeof code === 'string' && Object.hasOwn(ERROR_MESSAGES, code)
    ? ERROR_MESSAGES[code] : FORMAL_STATES.attentionRequired[1]
}

function safeScalar(value, maxLength = 160) {
  if (typeof value === 'number' || typeof value === 'boolean') return value
  return typeof value === 'string' ? value.slice(0, maxLength) : undefined
}

function renderPreview(preview) {
  const visible = {}
  const fields = ['name', 'deviceId', 'shortCode', 'macSuffix', 'publicKeyFingerprint', 'centaurosVersion', 'hardwareProfile', 'state']
  for (const field of fields) {
    const value = safeScalar(preview?.[field])
    if (value !== undefined) visible[field] = value
  }
  element('device-info').textContent = JSON.stringify(visible, null, 2)
  element('device-info').hidden = Object.keys(visible).length === 0
}

function renderFormalSnapshot(snapshot) {
  if (!formalMode) return
  if (['terminalError', 'restartRequired', 'cancelled'].includes(snapshot?.state)) formalRestartRequired = true
  const state = typeof snapshot?.state === 'string' && Object.hasOwn(FORMAL_STATES, snapshot.state)
    ? snapshot.state : 'attentionRequired'
  formalState = state
  if (['completed', 'cancelled'].includes(state)) formalSessionActive = false
  else if (state !== 'idle') formalSessionActive = true
  if (['awaitingWifi', 'awaitingOwnershipConfirmation', 'waitingCloud', 'completed'].includes(state)) {
    physicalConfirmed = true
  }
  const copy = FORMAL_STATES[state]
  element('v2-state').textContent = copy[0]
  element('v2-guidance').textContent = state === 'attentionRequired'
    ? safeAttention(snapshot?.attentionCode) : copy[1]
  if (formalRestartRequired) element('v2-guidance').textContent += ' 处理后请关闭此窗口，再重新打开“添加 AI 盒子”。'
  if (state === 'attentionRequired' && discoveryState === 'selected') {
    element('scan-status').textContent = '已选择盒子，但本次连接未完成。请查看下方原因。'
  } else if (hasPreview && discoveryState === 'selected') {
    element('scan-status').textContent = state === 'authenticating' && !physicalConfirmed
      ? '已读取设备信息，请核对眼前盒子的固定设备短码。' : '已选择盒子，请查看下方配网进度。'
  }
  element('v2-progress').hidden = false
  element('v2-progress').classList.toggle('attention', state === 'attentionRequired')
  element('v2-progress').classList.toggle('completed', state === 'completed')
  element('physical-confirmation-panel').hidden = !(hasPreview && !physicalConfirmed && state === 'authenticating')
  element('wifi-panel').hidden = state !== 'awaitingWifi'
  element('ownership-panel').hidden = state !== 'awaitingOwnershipConfirmation'
  updateControls()
}

function updateControls() {
  if (formalMode) {
    const inProgress = formalSessionActive && !['attentionRequired', 'completed'].includes(formalState)
    element('scan').disabled = !available || busy || inProgress || scanning || formalRestartRequired
    element('cancel-scan').disabled = !scanning
    element('disconnect').disabled = !formalSessionActive || formalRestartRequired
    element('read-status').disabled = true
    element('scan-networks').disabled = true
    element('confirm-physical').disabled = busy || !hasPreview || physicalConfirmed || formalState !== 'authenticating'
    element('submit-wifi').disabled = busy || formalState !== 'awaitingWifi'
    element('confirm-ownership').disabled = busy || formalState !== 'awaitingOwnershipConfirmation'
    element('refresh-state').disabled = busy || !formalSessionActive || formalRestartRequired
    return
  }
  element('scan').disabled = !available || busy || scanning
  element('cancel-scan').disabled = !scanning
  element('disconnect').disabled = !scanning && !connecting && !connected
  element('read-status').disabled = !connected || busy
  element('scan-networks').disabled = !connected || busy
  element('submit-wifi').disabled = !connected || busy
}

async function run(task) {
  if (busy) return
  const epoch = ++operationEpoch
  busy = true
  updateControls()
  try {
    await task(epoch)
  } catch (error) {
    if (epoch === operationEpoch) {
      const emptyScan = formalMode && liveDiscovery && errorCode(error) === 'PROVISIONING_SCAN_TIMEOUT'
      if (emptyScan) {
        formalSessionActive = false
        renderFormalSnapshot({ state: 'idle' })
      } else if (formalMode) renderFormalSnapshot({ state: 'attentionRequired', attentionCode: errorCode(error) })
      show(safeError(error), !emptyScan)
    }
  } finally {
    if (epoch === operationEpoch) {
      busy = false
      updateControls()
    }
  }
}

function updateWifiPasswordRule() {
  const open = element('security').value === 'open'
  element('password').disabled = open
  element('password').required = !open
  if (open) element('password').value = ''
}

function applyMode() {
  element('test-build-warning').hidden = !testBuild
  element('v2-progress').hidden = testBuild
  element('physical-confirmation-panel').hidden = true
  element('ownership-panel').hidden = true
  element('cancel-scan').hidden = formalMode && !liveDiscovery
  element('disconnect').textContent = formalMode ? '取消配网' : '断开蓝牙'
  element('candidates').hidden = false
  element('scan-networks').hidden = formalMode
  element('scan-networks').tabIndex = formalMode ? -1 : 0
  element('legacy-row').hidden = formalMode || !api?.legacyAllowed
  element('legacy').disabled = formalMode || !api?.legacyAllowed
  element('legacy').tabIndex = formalMode ? -1 : 0
  if (formalMode) {
    element('legacy').checked = false
    element('device-actions').hidden = true
    element('wifi-panel').hidden = true
    element('mode-description').textContent = '打开盒子的配网模式，由知君建立加密会话。请核对设备短码并确认眼前的盒子，再设置网络并确认绑定。'
    element('secure-notice').textContent = '知君只会通过已认证的加密会话发送网络设置；Wi-Fi 密码提交后会立即从页面清除。'
    renderFormalSnapshot({ state: 'idle' })
  } else {
    element('wifi-panel').hidden = false
    element('refresh-state').disabled = true
    element('secure-notice').textContent = 'CentaurOS 1.2.0 是整机版本；当前盒端配网能力仍为协议 v1。勾选兼容模式后可进行内部联调，但 Wi-Fi 密码不会被端到端加密。'
  }
}

if (!available) {
  element('availability').textContent = '当前 Electron/系统没有可用的 Web Bluetooth。'
} else {
  element('availability').textContent = formalMode
    ? '点击后开始扫描，发现的盒子会实时显示在下方。本轮扫描结束后，可点击重新扫描。'
    : '扫描只会在你点击后开始，并由你确认目标盒子。'
}

applyMode()
updateControls()

if (formalMode && typeof api?.onSnapshot === 'function') {
  api.onSnapshot(snapshot => renderFormalSnapshot(snapshot))
}

if (!formalMode && typeof api?.onCandidates === 'function') {
  api.onCandidates(devices => {
    const list = element('candidates')
    list.replaceChildren()
    for (const device of devices) {
      const item = document.createElement('li')
      const button = document.createElement('button')
      const suffix = typeof device?.transportId === 'string' ? device.transportId.slice(-6) : ''
      button.textContent = `${safeScalar(device?.name, 80) || 'CentaurOS-Setup'} · ${suffix}`
      button.addEventListener('click', () => { void api.select(device.transportId).then(
        () => show('已选择盒子，正在读取设备信息…'),
        error => show(safeError(error), true),
      ) })
      item.append(button)
      list.append(item)
    }
  })
}

function renderFormalCandidates(devices, selectable = true) {
  const list = element('candidates')
  list.replaceChildren()
  let count = 0
  for (const device of Array.isArray(devices) ? devices : []) {
    const candidateId = typeof device?.candidateId === 'string' && device.candidateId.length > 0 && device.candidateId.length <= 256
      ? device.candidateId : null
    if (!candidateId) continue
    const item = document.createElement('li')
    const button = document.createElement('button')
    const name = safeScalar(device?.name, 80) || 'CentaurOS-Setup'
    const shortCode = safeScalar(device?.shortCode, 12)
    button.textContent = shortCode ? `${name} · ${shortCode}` : name
    button.disabled = !selectable
    button.addEventListener('click', () => {
      void run(async epoch => {
        scanning = false
        show('正在选择盒子并建立认证会话…')
        await api.select(candidateId)
        if (epoch !== operationEpoch) return
        renderFormalSnapshot({ state: 'authenticating' })
        const response = await api.begin()
        if (epoch !== operationEpoch) return
        const preview = response?.preview && typeof response.preview === 'object' ? response.preview : response
        hasPreview = true
        renderPreview(preview)
        if (response?.snapshot && typeof response.snapshot === 'object') renderFormalSnapshot(response.snapshot)
        else renderFormalSnapshot({ state: 'authenticating' })
        show('已找到盒子。请核对设备信息与眼前盒子的固定设备短码。')
      })
    })
    item.append(button)
    list.append(item)
    count++
  }
  return count
}

function renderDiscovery(snapshot) {
  if (!snapshot || !['idle', 'scanning', 'completed', 'cancelled', 'error', 'selected'].includes(snapshot.state)) return
  discoveryState = snapshot.state
  discoveryCandidates = Array.isArray(snapshot.candidates) ? snapshot.candidates.slice(0, 20) : []
  scanning = discoveryState === 'scanning'
  const count = renderFormalCandidates(discoveryCandidates, scanning)
  const status = element('scan-status')
  status.hidden = discoveryState === 'idle'
  if (scanning) {
    status.textContent = count ? `已发现 ${count} 台盒子，列表持续更新。首次发现后保留 30 秒，请点击选择。`
      : '正在扫描附近盒子… 暂未发现设备，请保持盒子处于配网模式。'
    show('扫描结果会实时更新，请选择眼前的盒子。')
  } else if (discoveryState === 'completed') {
    formalSessionActive = false
    status.textContent = count ? `本轮扫描结束 · 发现 ${count} 台盒子。选择时限已结束，请重新扫描。`
      : ERROR_MESSAGES.PROVISIONING_SCAN_TIMEOUT
    show(status.textContent)
  } else if (discoveryState === 'cancelled') {
    formalSessionActive = false
    status.textContent = '已停止扫描，可重新开始。'
    show('已取消扫描。')
  } else if (discoveryState === 'error') {
    formalSessionActive = false
    status.textContent = safeError({ code: snapshot.errorCode })
    show(status.textContent, true)
  } else if (discoveryState === 'selected') {
    status.textContent = '已选择盒子，正在建立安全连接…'
  }
  updateControls()
}

if (liveDiscovery) api.onDiscoverySnapshot(renderDiscovery)

async function startFormal(epoch) {
  formalSessionActive = true
  hasPreview = false
  physicalConfirmed = false
  renderPreview(null)
  renderFormalSnapshot({ state: 'idle' })
  element('candidates').replaceChildren()
  show('正在扫描附近盒子…')
  const devices = await api.scan()
  if (epoch !== operationEpoch) return
  if (liveDiscovery) return // Subscription owns the full scan lifecycle.
  if (renderFormalCandidates(devices) === 0) throw Object.assign(new Error(), { code: 'NotFoundError' })
  show('请选择名称和盒身短码一致的盒子。')
}

async function startLegacy(epoch) {
  selectedTransportId = null
  connected = false
  scanning = true
  element('device-actions').hidden = true
  element('device-info').hidden = true
  element('candidates').replaceChildren()
  updateControls()
  show('正在扫描。请从下方候选项中选择你的盒子…')
  try {
    const devices = await api.scan(element('legacy').checked)
    if (epoch !== operationEpoch) return
    scanning = false
    connecting = true
    updateControls()
    const candidate = Array.isArray(devices) ? devices[0] : null
    if (!candidate?.transportId) throw Object.assign(new Error(), { code: 'NotFoundError' })
    selectedTransportId = candidate.transportId
    const device = await api.connect(selectedTransportId)
    if (epoch !== operationEpoch) return
    renderPreview(device)
    element('device-actions').hidden = false
    connecting = false
    connected = true
    show('已连接并读取盒子信息。请核对盒子短码和指纹后再配网。')
  } finally {
    if (epoch === operationEpoch) {
      scanning = false
      connecting = false
      updateControls()
    }
  }
}

element('scan').addEventListener('click', () => { void run(formalMode ? startFormal : startLegacy) })

element('cancel-scan').addEventListener('click', () => {
  if (formalMode) {
    if (!scanning || !liveDiscovery) return
    const epoch = ++operationEpoch
    busy = true
    scanning = false
    updateControls()
    void Promise.resolve(api.cancelScan()).then(() => {
      if (epoch !== operationEpoch) return
      formalSessionActive = false
      show('已取消扫描。')
    }, error => { if (epoch === operationEpoch) show(safeError(error), true) })
      .finally(() => { if (epoch === operationEpoch) { busy = false; updateControls() } })
    return
  }
  if (!scanning) return
  const epoch = ++operationEpoch
  scanning = false
  busy = true
  element('candidates').replaceChildren()
  updateControls()
  void Promise.resolve(api.cancelScan()).then(
    () => { if (epoch === operationEpoch) show('已取消扫描。') },
    error => { if (epoch === operationEpoch) show(safeError(error), true) },
  ).finally(() => {
    if (epoch === operationEpoch) {
      busy = false
      updateControls()
    }
  })
})

function resetFormal() {
  formalSessionActive = false
  scanning = false
  discoveryState = 'idle'
  discoveryCandidates = []
  element('candidates').replaceChildren()
  element('scan-status').hidden = true
  hasPreview = false
  physicalConfirmed = false
  element('password').value = ''
  renderPreview(null)
  renderFormalSnapshot({ state: formalRestartRequired ? 'cancelled' : 'idle' })
}

element('disconnect').addEventListener('click', () => {
  if (formalMode) {
    if (!formalSessionActive) return
    const epoch = ++operationEpoch
    busy = true
    updateControls()
    void Promise.resolve(api.cancel()).then(
      () => {
        if (epoch !== operationEpoch) return
        resetFormal()
        show('已取消本次配网。')
      },
      error => {
        if (epoch !== operationEpoch) return
        renderFormalSnapshot({ state: 'attentionRequired', attentionCode: errorCode(error) })
        show(safeError(error), true)
      },
    ).finally(() => {
      if (epoch === operationEpoch) {
        busy = false
        updateControls()
      }
    })
    return
  }
  if (!scanning && !connecting && !connected) return
  const epoch = ++operationEpoch
  scanning = false
  connecting = false
  connected = false
  busy = true
  selectedTransportId = null
  element('device-actions').hidden = true
  element('device-info').hidden = true
  element('candidates').replaceChildren()
  updateControls()
  void Promise.resolve(api.disconnect()).then(
    () => { if (epoch === operationEpoch) show('已断开蓝牙连接。') },
    error => { if (epoch === operationEpoch) show(safeError(error), true) },
  ).finally(() => {
    if (epoch === operationEpoch) {
      busy = false
      updateControls()
    }
  })
})

element('physical-confirmation').addEventListener('submit', event => {
  event.preventDefault()
  void run(async epoch => {
    renderFormalSnapshot({ state: 'authenticating' })
    const snapshot = await api.confirmPhysicalDevice()
    if (epoch !== operationEpoch) return
    physicalConfirmed = true
    renderFormalSnapshot(snapshot && typeof snapshot === 'object' ? snapshot : { state: 'awaitingWifi' })
    show('盒子身份已确认，请设置要连接的 Wi-Fi。')
  })
})

const LEGACY_STATUS_MESSAGES = Object.freeze({
  idle: '空闲', connected: '已连接', connecting: '连接中', failed: '未完成', provisioned: '已配置',
})

element('read-status').addEventListener('click', () => {
  if (formalMode) return
  void run(async epoch => {
    const status = await api.status()
    if (epoch === operationEpoch) show(`盒子状态：${LEGACY_STATUS_MESSAGES[status?.state] || '状态待确认'}`)
  })
})

element('scan-networks').addEventListener('click', () => {
  if (formalMode) return
  void run(async epoch => {
    const networks = await api.networks()
    if (epoch !== operationEpoch) return
    const list = element('networks')
    list.replaceChildren()
    for (const network of Array.isArray(networks) ? networks : []) {
      const item = document.createElement('li')
      item.textContent = `${safeScalar(network?.ssid, 32) || '未命名网络'} · ${network?.security === 'open' ? '开放网络' : 'WPA/WPA2'} · ${safeScalar(network?.signal, 16) ?? '未知信号'}`
      list.append(item)
    }
    show(`盒子扫描到 ${Array.isArray(networks) ? networks.length : 0} 个 Wi-Fi 网络。`)
  })
})

element('security').addEventListener('change', updateWifiPasswordRule)
updateWifiPasswordRule()

element('wifi').addEventListener('submit', event => {
  event.preventDefault()
  void run(async epoch => {
    if (formalMode) {
      if (formalState !== 'awaitingWifi') throw Object.assign(new Error(), { code: 'PROVISIONING_NOT_STARTED' })
      const credentials = {
        ssid: element('ssid').value,
        password: element('password').value,
        security: element('security').value,
      }
      element('password').value = ''
      try {
        show('正在通过加密会话发送网络设置…')
        const snapshot = await api.provideWifi(credentials)
        if (epoch !== operationEpoch) return
        renderFormalSnapshot(snapshot && typeof snapshot === 'object' ? snapshot : { state: 'waitingCloud' })
      } finally {
        credentials.password = ''
        element('password').value = ''
      }
      return
    }
    if (!selectedTransportId) throw Object.assign(new Error(), { code: 'PROVISIONING_NOT_STARTED' })
    const legacy = element('legacy').checked
    if (legacy && (!api.legacyAllowed || !window.confirm('当前使用配网协议 v1 兼容模式。Wi-Fi 密码将通过明文 GATT 指令发送。只在你控制的盒子和测试网络中继续；测试完成后请卸载此版本。'))) return
    const credentials = {
      ssid: element('ssid').value,
      password: element('password').value,
      security: element('security').value,
    }
    element('password').value = ''
    try {
      show('正在将网络设置发给盒子…')
      const status = await api.provision(credentials, legacy ? 'CONFIRM_LEGACY_PLAINTEXT_WIFI' : '')
      if (epoch !== operationEpoch) return
      if (status?.state === 'connected') {
        show('盒子已联网成功。请回到主窗口刷新盒子列表；如尚未绑定，请输入盒子上的 10 位认领码（大写字母和数字 2–7，兼容旧 20 位码）。')
      } else {
        const code = typeof status?.code === 'string' && Object.hasOwn(ERROR_MESSAGES, status.code)
          ? status.code : 'PROVISIONING_DEVICE_REJECTED'
        show(safeError({ code }), true)
      }
    } finally {
      credentials.password = ''
      element('password').value = ''
    }
  })
})

element('confirm-ownership').addEventListener('click', () => {
  void run(async epoch => {
    if (!formalMode || formalState !== 'awaitingOwnershipConfirmation') {
      throw Object.assign(new Error(), { code: 'PROVISIONING_NOT_STARTED' })
    }
    show('正在确认绑定…')
    const snapshot = await api.confirmOwnership()
    if (epoch !== operationEpoch) return
    renderFormalSnapshot(snapshot && typeof snapshot === 'object' ? snapshot : { state: 'waitingCloud' })
  })
})

element('refresh-state').addEventListener('click', () => {
  if (!formalMode || !formalSessionActive) return
  void run(async epoch => {
    const snapshot = await api.refresh()
    if (epoch === operationEpoch && snapshot && typeof snapshot === 'object') renderFormalSnapshot(snapshot)
  })
})
