import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'
import { fileURLToPath } from 'node:url'
import { entryBoundary } from './build/boundaries'

// An isolated, built desktop entry: no Web router, API proxy or public assets.
export default defineConfig({
  root: fileURLToPath(new URL('.', import.meta.url)),
  base: './',
  publicDir: false,
  plugins: [vue(), entryBoundary('desktop')],
  build: {
    outDir: 'dist-desktop',
    emptyOutDir: true,
    rollupOptions: { input: fileURLToPath(new URL('./desktop.html', import.meta.url)) },
  },
})
