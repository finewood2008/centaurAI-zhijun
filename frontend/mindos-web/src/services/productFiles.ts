import { hasProductScope, isDesktopProduct, trackProductObjectUrl, releaseProductObjectUrl } from '../shared/productScope.ts'

export interface ProductFiles {
  requestMicrophone(): Promise<boolean>
  saveText(name: string, text: string, contentType: string): Promise<void>
  preview(path: string, signal?: AbortSignal): Promise<string>
  releasePreview(url: string): void
  saveResource(path: string, fileName: string): Promise<void>
}
let files: ProductFiles | null = null
export function installProductFiles(driver: ProductFiles): () => void {
  files = driver
  return () => { if (files === driver) files = null }
}
export async function saveProductText(name: string, text: string, contentType = 'text/plain;charset=utf-8'): Promise<void> {
  if (isDesktopProduct()) {
    if (!files || !hasProductScope()) throw new Error('请先连接盒子，再保存文件。')
    return files.saveText(name, text, contentType)
  }
  const url = trackProductObjectUrl(URL.createObjectURL(new Blob([text], { type: contentType })))
  const anchor = document.createElement('a')
  anchor.href = url; anchor.download = name; anchor.click()
  setTimeout(() => releaseProductObjectUrl(url), 1000)
}
export async function productPreview(path: string, signal?: AbortSignal): Promise<string> {
  if (!isDesktopProduct()) return path
  if (!files || !hasProductScope()) throw new Error('请先连接盒子，再查看原件。')
  return files.preview(path, signal)
}
export function releaseProductPreview(url: string): void { if (isDesktopProduct()) files?.releasePreview(url) }
export async function saveProductResource(path: string, fileName: string): Promise<void> {
  if (isDesktopProduct()) {
    if (!files || !hasProductScope()) throw new Error('请先连接盒子，再保存原件。')
    return files.saveResource(path, fileName)
  }
  const link = document.createElement('a')
  link.href = path; link.download = fileName; link.rel = 'noopener'; link.click()
}

export async function requestProductMicrophone(): Promise<boolean> {
  if (!isDesktopProduct() || !files || !hasProductScope()) throw new Error('请先连接盒子，再使用语音输入。')
  return files.requestMicrophone()
}
