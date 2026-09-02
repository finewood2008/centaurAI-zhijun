// SSE 帧解析回归：跨分块、多行 data、注释/心跳、CRLF、默认事件名。
// 运行：node --experimental-strip-types tests/sse-parser.test.mjs
import assert from 'node:assert/strict'
import { parseSseChunk } from '../src/shared/sse-parser.ts'

function collect(chunks) {
  const frames = []
  let buffer = ''
  for (const chunk of chunks) buffer = parseSseChunk(buffer + chunk, (f) => frames.push(f))
  return { frames, buffer }
}

// 1) 一帧被切成两块：第一块只能解析出完整帧，尾部留在缓冲区
{
  const first = collect(['event: meta\ndata: {"a":1}\n\nevent: tok'])
  assert.equal(first.frames.length, 1)
  assert.deepEqual(first.frames[0], { event: 'meta', data: '{"a":1}' })
  assert.equal(first.buffer, 'event: tok')

  const second = collect(['event: meta\ndata: {"a":1}\n\nevent: tok', 'en\ndata: {"t":"你好"}\n\n'])
  assert.equal(second.frames.length, 2)
  assert.deepEqual(second.frames[1], { event: 'token', data: '{"t":"你好"}' })
  assert.equal(second.buffer, '')
}

// 2) 多行 data 按 \n 拼接；data 前的单个空格被去掉
{
  const { frames } = collect(['event: x\ndata: line1\ndata:  line2\n\n'])
  assert.deepEqual(frames, [{ event: 'x', data: 'line1\n line2' }])
}

// 3) 注释/心跳与 id/retry 被忽略，不产生帧
{
  const { frames, buffer } = collect([': keepalive\n\n', 'id: 3\nretry: 1000\n\n'])
  assert.equal(frames.length, 0)
  assert.equal(buffer, '')
}

// 4) CRLF 换行也能正确分帧
{
  const { frames } = collect(['event: a\r\ndata: 1\r\n\r\nevent: b\r\ndata: 2\r\n\r\n'])
  assert.deepEqual(frames.map((f) => f.event), ['a', 'b'])
}

// 5) 没有 event 行时事件名默认为 message
{
  const { frames } = collect(['data: hello\n\n'])
  assert.deepEqual(frames, [{ event: 'message', data: 'hello' }])
}

// 6) 分块恰好落在空行中间（\n | \n）
{
  const { frames } = collect(['event: z\ndata: 9\n', '\n'])
  assert.deepEqual(frames, [{ event: 'z', data: '9' }])
}

console.log('sse-parser: 6 tests OK')
