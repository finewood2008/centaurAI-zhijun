'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { createNativeSave } = require('../runtime/native-save.cjs');
const { DesktopError } = require('../runtime/public-error.cjs');
async function setup(t, choose) {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-native-save-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const filePath = path.join(directory, 'user-selected.md'); const calls = [];
  const save = createNativeSave({ getWindow: () => undefined, dialog: { showSaveDialog: async options => { calls.push(options); return choose ? choose() : { canceled: false, filePath }; } } });
  return { directory, filePath, save, calls };
}
const stream = values => (async function* () { for (const value of values) yield Buffer.from(value); })();
test('native save publishes complete stream atomically and never returns selected path', async t => {
  const { save, directory, filePath, calls } = await setup(t);
  await fs.writeFile(filePath, 'old user file');
  const result = await save({ fileName: 'notes.md', contentType: 'text/markdown', source: stream(['one', 'two']), isCurrent: () => true });
  assert.equal(result, true); assert.equal(calls[0].defaultPath, 'notes.md');
  assert.equal(await fs.readFile(filePath, 'utf8'), 'onetwo'); assert.deepEqual(await fs.readdir(directory), ['user-selected.md']);
});
test('cancel does not consume bytes and returns false', async t => {
  const { save, directory } = await setup(t, () => ({ canceled: true })); let read = false;
  const result = await save({ fileName: 'notes.md', contentType: 'text/markdown', isCurrent: () => true,
    source: (async function* () { read = true; yield Buffer.from('secret'); })() });
  assert.equal(result, false); assert.equal(read, false); assert.deepEqual(await fs.readdir(directory), []);
});
test('failed digest or changed generation preserves old destination and removes temporary bytes', async t => {
  const { save, directory, filePath } = await setup(t); await fs.writeFile(filePath, 'preserve');
  await assert.rejects(save({ fileName: 'notes.md', contentType: 'text/markdown', isCurrent: () => true,
    source: (async function* () { yield Buffer.from('partial'); throw new DesktopError('CONTRACT_MISMATCH'); })() }), { code: 'CONTRACT_MISMATCH' });
  assert.equal(await fs.readFile(filePath, 'utf8'), 'preserve'); assert.deepEqual(await fs.readdir(directory), ['user-selected.md']);
  let current = true;
  await assert.rejects(save({ fileName: 'notes.md', contentType: 'text/markdown', isCurrent: () => current,
    source: (async function* () { yield Buffer.from('partial'); current = false; yield Buffer.from('stale'); })() }), { code: 'STALE_GENERATION' });
  assert.equal(await fs.readFile(filePath, 'utf8'), 'preserve'); assert.deepEqual(await fs.readdir(directory), ['user-selected.md']);
});
test('native dialog budget remains held across generation changes until actual dialog completion', async t => {
  let finish; const { save } = await setup(t, () => new Promise(resolve => { finish = resolve; }));
  const first = save({ fileName: 'one.md', source: stream(['one']), isCurrent: () => true });
  await assert.rejects(save({ fileName: 'two.md', source: stream(['two']), isCurrent: () => true }), { code: 'RESOURCE_EXHAUSTED' });
  finish({ canceled: true }); assert.equal(await first, false);
});
