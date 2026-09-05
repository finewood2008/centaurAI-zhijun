<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue'
import type { MaterialStatus, MaterialType, Phase, ZhijunDesktopV1 } from '../../../shared/desktop-contract'
import { DesktopController } from './controller'

const bridge = (window as Window & { zhijunDesktop?: ZhijunDesktopV1 }).zhijunDesktop
const controller = new DesktopController(bridge)
const state = shallowRef(controller.state)
const stopObserving = controller.observe(next => { state.value = next })
const keyword = ref('')
const type = ref<MaterialType | ''>('')
const status = ref<MaterialStatus | ''>('')
const unsupportedRoute = ref(false)
const phase = computed(() => state.value.snapshot?.phase)
const environment = computed(() => state.value.snapshot?.environment ?? 'unconfigured')
const labels: Record<Phase, string> = {
  signed_out: '尚未登录', authenticating: '正在登录', selecting_device: '请选择盒子',
  connecting: '正在连接盒子', authorizing: '正在验证访问权限', ready: '已连接',
  disconnecting: '正在断开', failed: '连接未就绪',
}
const typeLabels: Record<MaterialType, string> = { document: '文档', image: '图片', audio: '音频' }
const statusLabels: Record<MaterialStatus, string> = {
  uploaded: '已上传', queued: '排队中', processing: '处理中', available: '可用', failed: '处理失败',
}
const canSignIn = computed(() => !!phase.value && ['signed_out', 'failed'].includes(phase.value)
  && !state.value.snapshot?.subject && !state.value.controlPending)
const canSignOut = computed(() => !!phase.value && phase.value !== 'signed_out'
  && state.value.pendingOperation !== 'signOut')
const canDisconnect = computed(() => !!phase.value && ['ready', 'failed'].includes(phase.value)
  && !!state.value.snapshot?.subject && !state.value.controlPending)
const pageNumber = computed(() => Math.floor(state.value.query.offset / state.value.query.limit) + 1)
const displayAccount = computed(() => state.value.snapshot?.subject?.accountId ?? '')

watch(() => state.value.snapshot?.generation, () => {
  keyword.value = ''; type.value = ''; status.value = ''
})
function filtersChanged(): void {
  void controller.setFilters({ keyword: keyword.value, type: type.value, status: status.value })
}
function checkRoute(): void { unsupportedRoute.value = !['', '#', '#/', '#/materials'].includes(window.location.hash) }
function goHome(): void { window.location.hash = '/materials'; checkRoute() }
function displayDate(value: string): string {
  const date = new Date(value)
  return Number.isNaN(date.getTime()) ? '时间未知' : date.toLocaleString('zh-CN', { hour12: false })
}
onMounted(() => { checkRoute(); window.addEventListener('hashchange', checkRoute); void controller.start() })
onBeforeUnmount(() => { window.removeEventListener('hashchange', checkRoute); stopObserving(); controller.dispose() })
</script>

<template>
  <div class="desktop-shell">
    <header class="app-header">
      <a class="brand" href="#/materials" aria-label="知君桌面首页"><span class="brand-mark">知</span><span>知君<small>桌面资料</small></span></a>
      <div class="header-status" role="status"><span class="status-dot" :class="{ connected: phase === 'ready' }"></span>{{ !state.hostAvailable ? '桌面服务未就绪' : phase ? labels[phase] : '正在初始化' }}</div>
    </header>

    <div class="environment-banner" :class="{ simulation: environment === 'simulation' }" data-testid="environment">
      <strong>{{ environment === 'simulation' ? '模拟环境 · 合成数据' : environment === 'production' ? '正式环境' : '正式连接尚未配置' }}</strong>
      <span>{{ environment === 'simulation' ? '用于验证桌面操作流程，当前展示的账号、盒子和资料均为模拟内容。' : environment === 'production' ? '资料来自当前授权盒子。' : '完成正式登录、设备授权和访问配置后，才能连接真实盒子。' }}</span>
    </div>

    <main>
      <section v-if="!state.hostAvailable" class="welcome-card" data-testid="missing-host">
        <p class="eyebrow">知君桌面</p><h1>请从桌面应用打开</h1>
        <p>当前页面没有桌面连接服务。请启动知君桌面应用后，再登录并选择盒子。</p>
      </section>
      <section v-else-if="unsupportedRoute" class="welcome-card">
        <p class="eyebrow">当前桌面版本</p><h1>此功能暂未开放</h1><p>当前支持登录、选择盒子和查看资料列表。</p>
        <button class="primary" @click="goHome">返回资料首页</button>
      </section>
      <template v-else>
        <section class="connection-card" aria-labelledby="connection-title">
          <div class="connection-copy"><p class="eyebrow">你的盒子，你的资料</p><h1 id="connection-title">{{ phase === 'ready' ? '连接已就绪' : '连接到你的盒子' }}</h1>
            <p v-if="phase === 'signed_out' || !phase">登录后，选择已绑定的盒子，查看其中的资料。</p>
            <p v-else-if="phase === 'authenticating'">正在完成登录，你可以随时退出。</p>
            <p v-else-if="phase === 'selecting_device'">选择下方已绑定的盒子，开始安全连接。</p>
            <p v-else-if="phase === 'connecting'">正在建立连接，请稍候。</p>
            <p v-else-if="phase === 'authorizing'">盒子已连通，正在确认资料访问权限。</p>
            <p v-else-if="phase === 'disconnecting'">正在结束当前连接并清理临时状态。</p>
            <p v-else-if="phase === 'ready'">资料仅在当前连接中展示，断开后将清空列表。</p>
            <p v-else>连接尚未就绪，请查看提示后重新选择盒子或登录。</p>
            <p v-if="displayAccount" class="account" data-testid="account">账号：{{ displayAccount }}<span v-if="state.snapshot?.subject?.deviceId"> · 盒子：{{ state.snapshot.subject.deviceId }}</span></p>
          </div>
          <div class="connection-actions">
            <button v-if="canSignIn" class="primary" data-testid="sign-in" @click="controller.control('beginSignIn')">登录知君</button>
            <button v-if="canDisconnect" data-testid="disconnect" @click="controller.control('disconnect')">{{ phase === 'failed' ? '重新选择盒子' : '断开连接' }}</button>
            <button v-if="canSignOut" class="text-button" data-testid="sign-out" @click="controller.control('signOut')">退出登录</button>
          </div>
        </section>

        <section v-if="state.error" class="error-card" role="alert" data-testid="error">
          <strong>暂时无法完成操作</strong><p>{{ state.error.message }}</p>
          <small>{{ state.error.code }}<template v-if="state.error.traceId"> · 关联编号 {{ state.error.traceId }}</template></small>
        </section>
        <p v-if="state.notice" class="notice" role="status">{{ state.notice }}</p>

        <section v-if="phase === 'selecting_device'" class="device-section" aria-labelledby="devices-title">
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

        <section v-if="phase === 'ready'" class="materials-section" aria-labelledby="materials-title" :aria-busy="state.loading">
          <div class="section-heading"><div><h2 id="materials-title">盒中资料</h2><p>查看文档、图片和音频的处理状态</p></div><div class="read-actions"><button v-if="state.loading" data-testid="cancel-read" @click="controller.cancelRead()">取消读取</button><button data-testid="refresh-materials" @click="controller.readPage()">刷新资料</button></div></div>
          <form class="filters" @submit.prevent="filtersChanged">
            <label class="search-field">搜索资料<div class="search-control"><input v-model="keyword" data-testid="keyword" type="search" maxlength="100" placeholder="输入文件名关键词" /><button type="submit" class="primary">搜索</button></div></label>
            <label>资料类型<select v-model="type" data-testid="type-filter" @change="filtersChanged"><option value="">全部类型</option><option value="document">文档</option><option value="image">图片</option><option value="audio">音频</option></select></label>
            <label>处理状态<select v-model="status" data-testid="status-filter" @change="filtersChanged"><option value="">全部状态</option><option v-for="(label, key) in statusLabels" :key="key" :value="key">{{ label }}</option></select></label>
          </form>
          <p v-if="state.loading" class="empty-state" role="status">正在读取资料…</p>
          <p v-else-if="state.page && !state.page.items.length" class="empty-state" data-testid="empty-materials">没有符合条件的资料。试试其他关键词或筛选条件。</p>
          <div v-else-if="state.page" class="table-scroll">
            <table data-testid="materials-table"><thead><tr><th scope="col">文件名</th><th scope="col">类型</th><th scope="col">状态</th><th scope="col">创建时间</th></tr></thead><tbody><tr v-for="item in state.page.items" :key="item.materialId"><td class="file-name">{{ item.fileName }}</td><td>{{ typeLabels[item.fileType] }}</td><td><span class="material-status" :class="`material-status-${item.status}`">{{ statusLabels[item.status] }}</span></td><td class="date">{{ displayDate(item.createdAt) }}</td></tr></tbody></table>
          </div>
          <p v-else-if="!state.error && !state.notice" class="empty-state">点击“刷新资料”开始读取。</p>
          <div v-if="state.page" class="pagination"><span>共 {{ state.page.total }} 条 · 第 {{ pageNumber }} 页 · 每页 20 条</span><div><button :disabled="state.loading || state.query.offset === 0" data-testid="previous-page" @click="controller.changePage(-1)">上一页</button><button :disabled="state.loading || !state.page.hasMore || state.query.offset + 20 > 10000" data-testid="next-page" @click="controller.changePage(1)">下一页</button></div></div>
        </section>
        <footer>当前支持资料列表；聊天、事项、上传和资料预览将在后续版本开放。</footer>
      </template>
    </main>
  </div>
</template>
