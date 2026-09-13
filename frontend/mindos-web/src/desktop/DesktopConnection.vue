<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue'
import { useRoute } from 'vue-router'
import type { Phase } from '../../../shared/desktop-contract'
import { isValidClaimToken, normalizeClaimToken } from './claimToken'
import { useDesktopWorkspace } from './workspace'
import SecureConnectionProgress from './SecureConnectionProgress.vue'

const props = withDefaults(defineProps<{ embedded?: boolean }>(), { embedded: false })
const { controller, state } = useDesktopWorkspace()
const route = useRoute()
const phone = ref('')
const password = ref('')
const confirmPassword = ref('')
const registrationCode = ref('')
const authMode = ref<'login' | 'register' | 'reset'>('login')
const formError = ref('')
const codeSeconds = ref(0)
const claimToken = ref('')
const rememberedPhone = ref('')
const passwordSaved = ref(false)
const rememberPassword = ref(true)
const rejectedSavedPhone = ref('')
let rememberedRevision = 0
let codeTimer: ReturnType<typeof setInterval> | undefined
const phase = computed(() => state.value.snapshot?.phase)
const environment = computed(() => state.value.snapshot?.environment ?? 'unconfigured')
const labels: Record<Phase, string> = {
  signed_out: '尚未登录', authenticating: '正在登录', selecting_device: '请选择盒子',
  connecting: '正在连接盒子', authorizing: '正在验证访问权限', ready: '已连接',
  disconnecting: '正在断开', failed: '连接未就绪',
}
const pageTitle = computed(() => typeof route.meta.title === 'string' ? route.meta.title : '当前页面')
const showAuth = computed(() => !!phase.value && ['signed_out', 'failed'].includes(phase.value)
  && !state.value.snapshot?.subject)
const canSignIn = computed(() => showAuth.value && !state.value.controlPending)
const canSignOut = computed(() => !!phase.value && phase.value !== 'signed_out'
  && state.value.pendingOperation !== 'signOut')
const canDisconnect = computed(() => !!phase.value && ['ready', 'failed'].includes(phase.value)
  && !!state.value.snapshot?.subject && !state.value.controlPending)
const showConnectionProgress = computed(() => !!state.value.snapshot?.subject && !!phase.value
  && ['connecting', 'authorizing', 'ready', 'disconnecting', 'failed'].includes(phase.value))
const canCancelConnection = computed(() => !!phase.value && ['connecting', 'authorizing'].includes(phase.value)
  && (!state.value.controlPending || state.value.pendingOperation === 'connect'))
const canProvision = computed(() => !!state.value.snapshot?.subject?.accountId
  && !!state.value.snapshot.capabilities.provisioning
  && !!phase.value && ['selecting_device', 'failed'].includes(phase.value))
const displayAccount = computed(() => state.value.snapshot?.subject?.accountId ?? '')
const connectionPathLabel = computed(() => state.value.snapshot?.subject?.selectedPath === 'DIRECT' ? '直连'
  : state.value.snapshot?.subject?.selectedPath === 'RELAY' ? '安全中继' : '')
const savedCredentialRejected = computed(() => rejectedSavedPhone.value !== ''
  && phone.value === rejectedSavedPhone.value)
const savedPasswordUsable = computed(() => passwordSaved.value && !savedCredentialRejected.value
  && phone.value === rememberedPhone.value)
const passwordRequired = computed(() => !savedPasswordUsable.value)
const passwordPlaceholder = computed(() => savedPasswordUsable.value ? '已由系统安全保存，留空即可登录' : '')
const savedCredentialMessage = computed(() => savedCredentialRejected.value
  ? '已保存的密码不可用，请重新输入当前密码后登录。' : '')
const connectionDiagnostic = computed(() => {
  if (state.value.error?.detailCode === 'DIRECT_TIMEOUT') return '直连超时'
  if (state.value.error?.detailCode === 'ICE_FAILED') return 'ICE 建链失败'
  return ''
})
async function signIn(): Promise<void> {
  formError.value = ''
  const attemptedPhone = phone.value
  const useSaved = savedPasswordUsable.value && password.value === ''
  const credentials = { phone: phone.value, password: password.value, rememberPassword: rememberPassword.value }
  password.value = ''
  try {
    await controller.control(useSaved ? 'signInWithSavedPassword' : 'signInWithPassword', useSaved ? rememberPassword.value : credentials)
  } finally {
    credentials.password = ''
  }
  if (useSaved && showAuth.value && ['SAVED_CREDENTIAL_REJECTED', 'AUTHENTICATION_FAILED', 'AUTHENTICATION_REQUIRED']
    .includes(state.value.error?.code ?? '')) {
    rejectedSavedPhone.value = attemptedPhone
    passwordSaved.value = false
  }
}
function signOut(): void {
  setAuthMode('login')
  claimToken.value = ''
  void controller.control('signOut')
}
function setAuthMode(mode: 'login' | 'register' | 'reset'): void {
  authMode.value = mode
  password.value = ''
  confirmPassword.value = ''
  registrationCode.value = ''
  formError.value = ''
}
async function sendRegistrationCode(): Promise<void> {
  formError.value = ''
  if (!/^1\d{10}$/.test(phone.value)) { formError.value = '请输入正确的 11 位手机号。'; return }
  const expiresIn = await controller.sendRegistrationCode(phone.value)
  if (!expiresIn) return
  codeSeconds.value = Math.min(60, expiresIn)
  if (codeTimer) clearInterval(codeTimer)
  codeTimer = setInterval(() => {
    codeSeconds.value = Math.max(0, codeSeconds.value - 1)
    if (!codeSeconds.value && codeTimer) { clearInterval(codeTimer); codeTimer = undefined }
  }, 1000)
}
function register(): void {
  formError.value = ''
  if (!/^1\d{10}$/.test(phone.value)) { formError.value = '请输入正确的 11 位手机号。'; return }
  if (!/^\d{6}$/.test(registrationCode.value)) { formError.value = '请输入短信中的 6 位验证码。'; return }
  const passwordBytes = new TextEncoder().encode(password.value).length
  if (passwordBytes < 8 || passwordBytes > 72 || /[\r\n\0]/u.test(password.value)) {
    formError.value = '密码需为 8–72 个 UTF-8 字节，且不能包含换行符。'; return
  }
  if (password.value !== confirmPassword.value) { formError.value = '两次输入的密码不一致。'; return }
  const credentials = { phone: phone.value, password: password.value, code: registrationCode.value,
    rememberPassword: rememberPassword.value }
  password.value = ''
  confirmPassword.value = ''
  registrationCode.value = ''
  void controller.control('registerWithPassword', credentials)
}
async function resetPassword(): Promise<void> {
  formError.value = ''
  if (!/^1\d{10}$/.test(phone.value)) { formError.value = '请输入正确的 11 位手机号。'; return }
  if (!/^\d{6}$/.test(registrationCode.value)) { formError.value = '请输入短信中的 6 位验证码。'; return }
  const passwordBytes = new TextEncoder().encode(password.value).length
  if (passwordBytes < 8 || passwordBytes > 72 || /[\r\n\0]/u.test(password.value)) {
    formError.value = '密码需为 8–72 个 UTF-8 字节，且不能包含换行符。'; return
  }
  if (password.value !== confirmPassword.value) { formError.value = '两次输入的密码不一致。'; return }
  const resetPhone = phone.value
  const credentials = { phone: resetPhone, code: registrationCode.value, password: password.value }
  password.value = ''
  confirmPassword.value = ''
  try {
    if (await controller.resetPassword(credentials)) {
      // The remembered password is stale after a reset. Require explicit entry
      // of the new password and never start a login from the reset action.
      rejectedSavedPhone.value = ''
      passwordSaved.value = false
      setAuthMode('login')
    } else if (state.value.error?.code === 'VERIFICATION_CODE_INVALID') {
      registrationCode.value = ''
    }
  } finally {
    credentials.password = ''
    credentials.code = ''
  }
}
async function claimDevice(): Promise<void> {
  formError.value = ''
  const value = normalizeClaimToken(claimToken.value)
  if (!isValidClaimToken(value)) { formError.value = '请输入管理员生成的 6 位数字设备认领码。'; return }
  if (await controller.claimDevice(value)) claimToken.value = ''
}
async function loadRememberedLogin(): Promise<void> {
  const revision = ++rememberedRevision
  const value = await controller.getRememberedLogin()
  if (revision !== rememberedRevision || !showAuth.value) return
  rememberedPhone.value = value?.phone ?? ''
  phone.value = value?.phone ?? ''
  passwordSaved.value = !!value?.passwordSaved
  rememberPassword.value = value?.passwordSaved ?? true
}
watch(showAuth, next => {
  password.value = ''
  if (next) void loadRememberedLogin()
  else if (state.value.snapshot?.subject) rejectedSavedPhone.value = ''
}, { immediate: true })
watch(phone, next => {
  if (next !== rememberedPhone.value) {
    passwordSaved.value = false
    rejectedSavedPhone.value = ''
  }
})
onBeforeUnmount(() => {
  rememberedRevision++
  if (codeTimer) clearInterval(codeTimer)
  password.value = ''; confirmPassword.value = ''; registrationCode.value = ''; claimToken.value = ''
  phone.value = ''; rememberedPhone.value = ''; passwordSaved.value = false; rejectedSavedPhone.value = ''
})
</script>

<template>
  <div class="desktop-shell" :class="{ embedded: props.embedded }" :data-testid="props.embedded ? 'workspace-unavailable' : undefined">
    <header v-if="!props.embedded" class="app-header">
      <a class="brand" href="#/materials" aria-label="知君桌面首页"><span class="brand-mark">知</span><span>知君<small>桌面工作区</small></span></a>
      <div class="header-status" role="status"><span class="status-dot" :class="{ connected: phase === 'ready' }"></span>{{ !state.hostAvailable ? '桌面服务未就绪' : phase ? labels[phase] : '正在初始化' }}</div>
    </header>

    <div v-if="!props.embedded || environment === 'simulation'" class="environment-banner" :class="{ simulation: environment === 'simulation' }" data-testid="environment">
      <strong>{{ environment === 'simulation' ? '模拟环境 · 合成数据' : environment === 'production' ? '账号服务已配置' : '正式连接尚未配置' }}</strong>
      <span>{{ environment === 'simulation' ? '用于验证桌面操作流程，当前展示的账号、盒子和资料均为模拟内容。' : environment === 'production' ? '连接已绑定的盒子，验证访问权限后进入知君。' : '完成正式登录、设备授权和访问配置后，才能连接真实盒子。' }}</span>
    </div>

    <component :is="props.embedded ? 'section' : 'main'">
      <p v-if="props.embedded" class="workspace-notice" role="status">{{ pageTitle }}需要连接盒子后使用。连接就绪前不会加载此页面的数据。</p>
      <section v-if="!state.hostAvailable" class="welcome-card" data-testid="missing-host">
        <p class="eyebrow">知君桌面</p><h1>请从桌面应用打开</h1>
        <p>当前页面没有桌面连接服务。请启动知君桌面应用后，再登录并选择盒子。</p>
      </section>
      <template v-else>
        <SecureConnectionProgress v-if="showConnectionProgress" :snapshot="state.snapshot"
          :can-cancel="canCancelConnection" @cancel="controller.control('disconnect')" />
        <section v-if="!showConnectionProgress" class="connection-card" aria-labelledby="connection-title">
          <div class="connection-copy"><p class="eyebrow">你的盒子，你的资料</p><h1 id="connection-title">{{ phase === 'ready' ? '正在打开工作区' : phase === 'failed' ? '连接暂未就绪' : '连接到你的盒子' }}</h1>
            <p v-if="phase === 'signed_out' || !phase">登录后，选择已绑定的盒子，打开知君工作区。</p>
            <p v-else-if="phase === 'authenticating'">正在完成登录，你可以随时退出。</p>
            <p v-else-if="phase === 'selecting_device'">选择下方已绑定的盒子，开始安全连接。</p>
            <p v-else-if="phase === 'connecting'">正在建立连接，请稍候。</p>
            <p v-else-if="phase === 'authorizing'">盒子已连通，正在确认资料访问权限。</p>
            <p v-else-if="phase === 'disconnecting'">正在结束当前连接并清理临时状态。</p>
            <p v-else-if="phase === 'ready'">盒子已连通，正在确认工作区是否就绪。</p>
            <p v-else>连接尚未就绪，请查看提示后重新选择盒子或登录。</p>
            <p v-if="displayAccount" class="account" data-testid="account">账号：{{ displayAccount }}<span v-if="state.snapshot?.subject?.deviceId"> · 盒子：{{ state.snapshot.subject.deviceId }}</span><span v-if="connectionPathLabel"> · {{ connectionPathLabel }}</span></p>
          </div>
          <div class="connection-actions">
            <button v-if="canSignIn && environment !== 'production'" class="primary" data-testid="sign-in" @click="controller.control('beginSignIn')">登录知君</button>
            <button v-if="canProvision" :disabled="state.controlPending" data-testid="open-provisioning" @click="controller.openProvisioning()">{{ state.pendingOperation === 'openProvisioning' ? '正在打开…' : '扫描附近盒子' }}</button>
            <button v-if="canDisconnect" data-testid="disconnect" @click="controller.control('disconnect')">{{ phase === 'failed' ? '重新选择盒子' : '断开连接' }}</button>
            <button v-if="canSignOut && !props.embedded" class="text-button" data-testid="sign-out" @click="signOut">退出登录</button>
          </div>
        </section>

        <div v-if="showConnectionProgress && (canDisconnect || canProvision || (canSignOut && !props.embedded))" class="connection-recovery-actions">
          <button v-if="canProvision" :disabled="state.controlPending" data-testid="open-provisioning" @click="controller.openProvisioning()">{{ state.pendingOperation === 'openProvisioning' ? '正在打开…' : '扫描附近盒子' }}</button>
          <button v-if="canDisconnect" data-testid="disconnect" @click="controller.control('disconnect')">重新选择盒子</button>
          <button v-if="canSignOut && !props.embedded" class="text-button" data-testid="sign-out" @click="signOut">退出登录</button>
        </div>

        <section v-if="showAuth && environment === 'production'" class="auth-panel" aria-labelledby="auth-title">
          <div class="auth-mode" role="tablist" aria-label="账号入口">
            <button id="auth-title" type="button" role="tab" :aria-selected="authMode === 'login'" :class="{ active: authMode === 'login' }" data-testid="show-login" @click="setAuthMode('login')">已有账号登录</button>
            <button type="button" role="tab" :aria-selected="authMode === 'register'" :class="{ active: authMode === 'register' }" data-testid="show-register" @click="setAuthMode('register')">注册新用户</button>
            <button type="button" role="tab" :aria-selected="authMode === 'reset'" :class="{ active: authMode === 'reset' }" data-testid="show-reset-password" @click="setAuthMode('reset')">忘记密码</button>
          </div>
          <form v-if="authMode === 'login'" class="login-form" data-testid="password-login" @submit.prevent="signIn">
            <label>手机号<input v-model="phone" data-testid="login-phone" type="tel" inputmode="numeric" autocomplete="username" pattern="1[0-9]{10}" maxlength="11" required /></label>
            <label>密码<input v-model="password" data-testid="login-password" type="password" autocomplete="current-password" minlength="8" maxlength="72" :required="passwordRequired" :placeholder="passwordPlaceholder" /></label>
            <button class="primary" type="submit" :disabled="state.controlPending" data-testid="sign-in">登录知君</button>
            <label class="remember-password"><input v-model="rememberPassword" data-testid="remember-password" type="checkbox" /> 使用系统安全存储记住密码</label>
            <p v-if="savedCredentialRejected" class="form-error" role="alert" data-testid="saved-credential-rejected">{{ savedCredentialMessage }}</p>
            <p v-else>{{ savedPasswordUsable ? '已记住上次账号和密码；密码不会显示在页面中。取消勾选并登录后，只保留账号。' : '会记住上次成功登录的账号；勾选后，密码由系统加密保存且不会显示在页面中。' }}</p>
          </form>
          <form v-else-if="authMode === 'register'" class="login-form registration-form" data-testid="registration" @submit.prevent="register">
            <label>手机号<input v-model="phone" data-testid="registration-phone" type="tel" inputmode="numeric" autocomplete="username" pattern="1[0-9]{10}" maxlength="11" required /></label>
            <label class="code-field">短信验证码<span class="code-control"><input v-model="registrationCode" data-testid="registration-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /><button type="button" :disabled="state.controlPending || codeSeconds > 0" data-testid="send-registration-code" @click="sendRegistrationCode">{{ codeSeconds > 0 ? `${codeSeconds} 秒后重发` : '发送验证码' }}</button></span></label>
            <label>设置密码<input v-model="password" data-testid="registration-password" type="password" autocomplete="new-password" minlength="8" maxlength="72" required /></label>
            <label>确认密码<input v-model="confirmPassword" data-testid="registration-confirm-password" type="password" autocomplete="new-password" minlength="8" maxlength="72" required /></label>
            <button class="primary" type="submit" :disabled="state.controlPending" data-testid="register">注册并继续</button>
            <label class="remember-password"><input v-model="rememberPassword" type="checkbox" /> 使用系统安全存储记住密码</label>
            <p>注册成功后将直接进入盒子选择页；密码只会在勾选时由操作系统加密保存。</p>
          </form>
          <form v-else class="login-form registration-form" data-testid="password-reset" @submit.prevent="resetPassword">
            <label>手机号<input v-model="phone" data-testid="reset-phone" type="tel" inputmode="numeric" autocomplete="username" pattern="1[0-9]{10}" maxlength="11" required /></label>
            <label class="code-field">短信验证码<span class="code-control"><input v-model="registrationCode" data-testid="reset-code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" required /><button type="button" :disabled="state.controlPending || codeSeconds > 0" data-testid="send-reset-code" @click="sendRegistrationCode">{{ codeSeconds > 0 ? `${codeSeconds} 秒后重发` : '发送验证码' }}</button></span></label>
            <label>新密码<input v-model="password" data-testid="reset-password" type="password" autocomplete="new-password" minlength="8" maxlength="72" required /></label>
            <label>确认新密码<input v-model="confirmPassword" data-testid="reset-confirm-password" type="password" autocomplete="new-password" minlength="8" maxlength="72" required /></label>
            <button class="primary" type="submit" :disabled="state.controlPending" data-testid="reset-password-submit">{{ state.pendingOperation === 'resetPassword' ? '正在提交…' : '重置密码' }}</button>
            <p>提交后的提示不会透露该手机号是否已注册；请使用新密码手动登录。</p>
          </form>
          <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
        </section>

        <section v-if="state.error" class="error-card" role="alert" data-testid="error">
          <strong>暂时无法完成操作</strong><p>{{ state.error.message }}</p>
          <small>{{ state.error.code }}<template v-if="connectionDiagnostic"> · {{ connectionDiagnostic }}</template><template v-if="state.error.traceId"> · 关联编号 {{ state.error.traceId }}</template></small>
        </section>
        <p v-if="state.notice" class="notice" role="status">{{ state.notice }}</p>

        <section v-if="phase === 'selecting_device'" class="device-section" aria-labelledby="devices-title">
          <form class="claim-card" data-testid="device-claim" @submit.prevent="claimDevice">
            <div><h2>认领新盒子</h2><p>输入管理员生成的 6 位一次性认领码。认领成功后立即失效；如需更换归属，请联系管理员处理。</p></div>
            <label><span class="sr-only">设备认领码</span><input v-model="claimToken" data-testid="claim-token" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" maxlength="6" placeholder="请输入 6 位认领码" required /></label>
            <button class="primary" type="submit" :disabled="state.controlPending" data-testid="claim-device">{{ state.pendingOperation === 'claimDevice' ? '正在认领…' : '认领盒子' }}</button>
          </form>
          <p v-if="formError" class="form-error" role="alert">{{ formError }}</p>
          <div class="section-heading"><h2 id="devices-title">已绑定的盒子</h2><button :disabled="state.devicesLoading || state.controlPending" data-testid="refresh-devices" @click="controller.loadDevices()">刷新设备</button></div>
          <p v-if="state.devicesLoading" role="status" class="empty-state">正在获取盒子列表…</p>
          <p v-else-if="!state.devices.length" class="empty-state">暂无可选择的盒子。请确认当前账号已绑定设备，然后刷新列表。</p>
          <ul v-else class="device-grid">
            <li v-for="device in state.devices" :key="device.deviceId" class="device-card">
              <div><h3>{{ device.displayName }}</h3><p class="device-id">{{ device.deviceId }}</p><p>{{ device.availability === 'online' ? '在线' : device.availability === 'offline' ? '离线' : '状态待确认' }}</p></div>
              <button class="primary" :disabled="state.controlPending || device.availability === 'offline'" :data-testid="`connect-${device.deviceId}`" :aria-label="`连接 ${device.displayName}`" @click="controller.control('connect', device.deviceId)">连接</button>
            </li>
          </ul>
        </section>

        <p v-if="phase === 'ready' && !state.snapshot?.capabilities.product" class="notice">盒子已连接，知君工作区尚未就绪。请重新连接以加载完整产品服务。</p>
      </template>
    </component>
  </div>
</template>

<style scoped src="./desktop.css" />

<style scoped>
.embedded { width:100%; }
.workspace-notice { margin:0 0 18px; font-size:14px; color:var(--ws-text-secondary-color); }
.embedded .connection-card { padding:24px; }
.embedded .connection-copy h1 { font-size:24px; }
.connection-recovery-actions { display:flex; flex-wrap:wrap; gap:8px; margin-top:16px; }
</style>
