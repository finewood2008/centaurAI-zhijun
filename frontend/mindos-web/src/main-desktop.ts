import { createApp } from 'vue'
import DesktopApp from './desktop/DesktopApp.vue'
import { createDesktopRouter } from './desktop/router'
import { pushToast } from './composables/toastStore'
import { enableDesktopProduct } from './shared/productScope'
import './styles/tokens.css'
import './styles/base.css'
import './styles/main.css'
import './styles/immersive.css'

enableDesktopProduct()
const app = createApp(DesktopApp)
app.config.errorHandler = error => {
  if (error instanceof DOMException && error.name === 'AbortError') return
  pushToast({ type: 'error', message: error instanceof Error ? error.message : '操作暂未完成，请重试。' })
}
app.use(createDesktopRouter()).mount('#app')
