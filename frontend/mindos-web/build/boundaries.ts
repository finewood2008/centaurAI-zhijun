import type { Plugin } from 'vite'

/** Validate the actual bundled module graph, not just package script names. */
export function entryBoundary(target: 'web' | 'desktop'): Plugin {
  return {
    name: `zhijun-${target}-boundary`,
    apply: 'build',
    generateBundle() {
      for (const raw of this.getModuleIds()) {
        const id = raw.replace(/\\/g, '/')
        const native = id.includes('/frontend/shell/') || id.includes('/node_modules/@nexusaos/')
          || id.includes('/node_modules/electron/')
          || /(?:^|:)electron(?:$|\/)/.test(id)
        const desktop = id.includes('/src/desktop/') || id.includes('/src/main-desktop.ts')
        const legacyWeb = /\/src\/(?:main\.ts|router\/index\.ts)$/.test(id)
        if (target === 'desktop' && /\/src\/services\/(?:api|sse|taskRouting)\.ts$/.test(id)) {
          const code = this.getModuleInfo(raw)?.code ?? ''
          if (/\bfetch\s*\(/.test(code)) this.error(`Direct renderer networking in product service: ${id}`)
        }
        if (native || (target === 'web' ? desktop : legacyWeb)) {
          this.error(`Unexpected module in ${target} entry: ${id}`)
        }
      }
    },
  }
}
