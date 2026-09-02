// 模型通道的人话：用户看到的是「演示模型 / 本机模型 / DeepSeek（外部）」，不是 provider 枚举与模型 id。
export interface ChannelMeta {
  provider: string
  model: string
  external: boolean
}

export function providerName(provider: string | null | undefined, model?: string | null): string {
  const m = (model || '').toLowerCase()
  if (provider === 'fake') return '演示模型'
  if (provider === 'ollama') return '本机模型'
  if (provider === 'anthropic') return 'Claude'
  if (m.includes('deepseek')) return 'DeepSeek'
  if (m.includes('gpt') || /^o[1-9]/.test(m)) return 'OpenAI'
  if (m.includes('qwen')) return '通义千问'
  if (m.includes('kimi') || m.includes('moonshot')) return 'Kimi'
  if (m.includes('glm')) return '智谱'
  if (m.includes('doubao')) return '豆包'
  return provider === 'openai' ? '外部模型' : model || '模型'
}

/** 页头 / 出处条上的短标签：演示模型 · 本机模型 · DeepSeek（外部） */
export function channelShort(meta: ChannelMeta | null | undefined): string {
  if (!meta) return ''
  if (meta.provider === 'fake') return '演示模型'
  if (!meta.external) return '本机模型'
  return `${providerName(meta.provider, meta.model)}（外部）`
}

/** 出处条展开后的完整一句 */
export function channelLine(meta: ChannelMeta | null | undefined): string {
  if (!meta) return ''
  if (meta.provider === 'fake') return '这是演示模型：没有调用真实模型，回复来自脚本。'
  if (meta.external) return `这轮用的是外部模型 ${providerName(meta.provider, meta.model)}：你的问题和上面列出的片段发到了它的服务器。`
  return `这轮用的是本机模型：数据没有离开这台设备。`
}

/** 模型没配置或当前不可用：页头与输入区都要降级成「还没配置模型 · 去偏好」。 */
export function modelUnavailable(status: { configured?: boolean; error?: string | null } | null | undefined): boolean {
  if (!status) return false
  return status.configured === false || !!status.error
}

export const MODEL_UNAVAILABLE_TEXT = '还没配置模型'
