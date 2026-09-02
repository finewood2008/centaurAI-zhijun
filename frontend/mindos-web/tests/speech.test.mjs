// 语音输入纯逻辑回归：合并 base / final / interim，分离识别结果。
// 运行：node --experimental-strip-types tests/speech.test.mjs
import assert from 'node:assert/strict'
import { mergeTranscript, splitResults } from '../src/shared/speech.ts'

// 1) 空 base：final 与 interim 直接拼接
{
  assert.equal(mergeTranscript('', '我在做远川项目', '压力很'), '我在做远川项目压力很')
  assert.equal(mergeTranscript('', '', '压力很'), '压力很')
  assert.equal(mergeTranscript('', '', ''), '')
}

// 2) 有 base：识别内容另起一行追加，base 尾部空白被去掉
{
  assert.equal(mergeTranscript('先前打的字  ', '我在做远川项目', ''), '先前打的字\n我在做远川项目')
  assert.equal(mergeTranscript('先前打的字', '', ''), '先前打的字')
}

// 3) interim 被后续刷新替换，final 保留
{
  const a = mergeTranscript('', '我在做', '远川')
  const b = mergeTranscript('', '我在做远川项目', '')
  assert.equal(a, '我在做远川')
  assert.equal(b, '我在做远川项目')
}

// 4) splitResults 分离 final / interim
{
  const results = [
    { isFinal: true, 0: { transcript: '我在做' }, length: 1 },
    { isFinal: false, 0: { transcript: '远川' }, length: 1 },
  ]
  assert.deepEqual(splitResults(results), { finalText: '我在做', interimText: '远川' })
  assert.deepEqual(splitResults([]), { finalText: '', interimText: '' })
}

console.log('speech: 4 groups OK')
