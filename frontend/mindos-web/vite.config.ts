import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath, URL } from 'node:url'

// 开发环境由 Vite dev server 提供 /mindos，并将 /api 代理到 FastAPI。
// 生产环境构建产物由后端 server.py 在 /mindos 路径下提供。
export default defineConfig({
  plugins: [vue()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  base: '/mindos/',
  server: {
    host: '127.0.0.1',
    port: 5173,
    strictPort: true,
    proxy: {
      '/api': {
        target: 'http://127.0.0.1:8618',
        changeOrigin: true,
      },
    },
  },
})
