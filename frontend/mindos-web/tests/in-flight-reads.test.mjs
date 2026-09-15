import assert from 'node:assert/strict'
import { createInFlightReads } from '../src/composables/inFlightReads.ts'

const reads = createInFlightReads()
const cancelled = reads.run('not-dispatched', async () => { throw Error('old view dispatched a read') })
reads.clear()
await assert.rejects(cancelled, { name: 'AbortError' })
let calls = 0, resolve
const first = reads.run('status', () => { calls++; return new Promise(done => { resolve = done }) })
const shared = reads.run('status', () => { throw Error('duplicate read') })
assert.equal(first, shared)
await Promise.resolve()
assert.equal(calls, 1)
resolve({ pending: 1 })
assert.deepEqual(await shared, { pending: 1 })
assert.equal(await reads.run('status', async () => { calls++; return 2 }), 2)
assert.equal(calls, 2, 'settled values are not cached')

await assert.rejects(reads.run('error', async () => { throw Error('busy') }), /busy/)
assert.equal(await reads.run('error', async () => 'recovered'), 'recovered')

let finishOld, finishNew
const old = reads.run('attention:a', () => new Promise(done => { finishOld = done }))
await Promise.resolve()
reads.clear()
const fresh = reads.run('attention:a', () => new Promise(done => { finishNew = done }))
await Promise.resolve()
assert.notEqual(old, fresh, 'navigation does not share old view reads')
finishOld('old'); await old
assert.equal(reads.run('attention:a', async () => 'unexpected'), fresh, 'old completion cannot remove new read')
finishNew('new'); assert.equal(await fresh, 'new')
console.log('in-flight reads: sharing, no result cache, error recovery and navigation isolation passed')
