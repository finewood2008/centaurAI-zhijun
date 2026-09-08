import assert from 'node:assert/strict'
import test from 'node:test'

import { ApiError, throwApiError } from '../src/services/api.ts'

async function capture(body, status = 403) {
  try {
    await throwApiError(new Response(JSON.stringify(body), {
      status,
      headers: { 'Content-Type': 'application/json' },
    }))
  } catch (error) {
    assert.ok(error instanceof ApiError)
    return error
  }
  assert.fail('throwApiError should reject non-success responses')
}

test('403 detail code keeps status and maps a safe actionable message', async () => {
  const error = await capture({ detail: { code: 'remote_model_target_not_allowlisted' } })
  assert.equal(error.status, 403)
  assert.equal(error.code, 'remote_model_target_not_allowlisted')
  assert.equal(error.message, '在线模型地址未通过盒子的网络安全校验，请检查供应商服务地址。')
  assert.notEqual(error.message, '请求失败（403）')
})

test('stable top-level and nested error codes map without masking explicit server detail', async () => {
  const stream = await capture({ code: 'MODEL_STREAM_INCOMPLETE' }, 502)
  assert.equal(stream.message, '在线模型响应提前中断，请稍后重试。')

  const media = await capture({ error: { code: 'UNSUPPORTED_MEDIA_TYPE' } }, 415)
  assert.equal(media.message, '请求的媒体类型不受支持，请检查文件或请求格式。')

  const explicit = await capture({ detail: { code: 'MODEL_STREAM_INCOMPLETE', detail: '服务端已确认中断' } }, 502)
  assert.equal(explicit.message, '服务端已确认中断')
})

test('workspace media-type contract code has a distinct recovery message', async () => {
  const provider = await capture({ detail: { code: 'MODEL_STREAM_MEDIA_TYPE_INVALID' } }, 502)
  assert.equal(provider.message, '在线模型服务地址返回了网页而不是模型数据，请检查地址是否包含正确的 API 路径。')

  const error = await capture({ detail: { code: 'WORKSPACE_MEDIA_TYPE_INVALID' } }, 502)
  assert.equal(error.code, 'WORKSPACE_MEDIA_TYPE_INVALID')
  assert.equal(error.message, '盒子返回的媒体类型不符合接口要求，请更新盒端服务后重试。')
})
