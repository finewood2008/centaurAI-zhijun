import { createApp } from 'vue'
import App from './App.vue'
import router from './router'
import { pushToast } from './composables/toastStore'
import { provisionMindosSession } from './services/api'
import './styles/tokens.css'
import './styles/base.css'
import './styles/main.css'

const app = createApp(App)

// 未捕获异常统一经 Toast 反馈，避免静默失败
app.config.errorHandler = (err) => {
  const message = err instanceof Error && err.message ? err.message : '应用发生未捕获异常'
  pushToast({ type: 'error', message })
}

window.addEventListener('unhandledrejection', (event) => {
  const message =
    event.reason instanceof Error && event.reason.message
      ? event.reason.message
      : '异步请求未处理异常'
  pushToast({ type: 'error', message })
})

// 阶段 2：非本机调试（票据模式）时，用 App/Electron Consumer Client 经受控通道
// 投放的一次性票据建立 MindOS 会话；登录/认领/设备管理均不由前端承担。
// 无可用票据（本地调试/浏览器独立运行）时静默跳过，不阻塞页面挂载。
async function bootstrapConnectivity(): Promise<void> {
  try {
    await provisionMindosSession()
  } catch (err) {
    const message = err instanceof Error ? err.message : '连接会话初始化失败'
    pushToast({ type: 'error', message })
  }
}

void bootstrapConnectivity().finally(() => {
  app.use(router).mount('#app')
})
