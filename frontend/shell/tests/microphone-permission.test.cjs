'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { createMicrophonePermission, verifyUsageDescription, DESCRIPTION } = require('../runtime/microphone-permission.cjs');
const { createProductSession } = require('../runtime/product-session.cjs');
const ENTRY = 'zhijun://desktop/desktop.html';
const details = extra => ({ isMainFrame: true, requestingUrl: ENTRY + '#/chat', securityOrigin: 'zhijun://desktop', mediaType: 'audio', ...extra });
function fixture(overrides = {}) {
  let generation = 1, time = 100, focused = true, calls = 0;
  const contents = { id: 5, mainFrame: { url: ENTRY + '#/chat' }, getURL: () => ENTRY + '#/chat', isDestroyed: () => false, isFocused: () => focused };
  const host = createMicrophonePermission({ getContents: () => contents, platform: 'darwin', clock: () => time,
    verifyUsage: async () => {}, systemPreferences: { getMediaAccessStatus: () => 'not-determined', askForMediaAccess: async type => { assert.equal(type, 'microphone'); calls++; return true; } }, ...overrides });
  const owner = () => generation === 1;
  return { host, contents, owner, calls: () => calls, expire: () => { time += 15001; }, change: () => { generation++; }, blur: () => { focused = false; } };
}
test('audio is denied by default; explicit system request grants only exact main frame for 15 seconds', async () => {
  const f = fixture();
  assert.equal(f.calls(), 0, 'construction never probes or prompts');
  assert.equal(f.host.check(f.contents, 'media', 'zhijun://desktop', details()), false);
  assert.equal(await f.host.request(f.owner), true); assert.equal(f.calls(), 1);
  assert.equal(f.host.check(f.contents, 'media', 'zhijun://desktop', details()), true);
  let allowed; f.host.permissionRequest(f.contents, 'media', value => { allowed = value; }, details({ mediaTypes: ['audio'] })); assert.equal(allowed, true);
  f.expire(); assert.equal(f.host.check(f.contents, 'media', 'zhijun://desktop', details()), false); f.host.dispose();
});
test('camera, combined capture, unknown media, foreign origins/windows and subframes never get a grant', async () => {
  const f = fixture(); await f.host.request(f.owner);
  for (const input of [details({ mediaType: 'video' }), details({ mediaType: 'unknown' }), details({ isMainFrame: false }),
    details({ requestingUrl: 'zhijun-media://session/' + 'a'.repeat(32) }), details({ securityOrigin: 'https://outside.invalid' }), details({ embeddingOrigin: 'zhijun://desktop' })]) {
    assert.equal(f.host.check(f.contents, 'media', 'zhijun://desktop', input), false);
  }
  for (const origin of ['null', 'https://outside.invalid', 'zhijun://desktop.evil', 'zhijun://user@desktop']) assert.equal(f.host.check(f.contents, 'media', origin, details()), false);
  assert.equal(f.host.check({ ...f.contents, id: 6 }, 'media', 'zhijun://desktop', details()), false);
  for (const mediaTypes of [['video'], ['audio', 'video'], [], ['audio', 'audio'], undefined]) {
    let allowed; f.host.permissionRequest(f.contents, 'media', value => { allowed = value; }, details({ mediaTypes })); assert.equal(allowed, false);
  }
  for (const permission of ['display-capture', 'notifications', 'geolocation', 'fileSystem']) {
    let allowed; f.host.permissionRequest(f.contents, permission, value => { allowed = value; }, details({ mediaTypes: ['audio'] })); assert.equal(allowed, false);
  }
  f.host.dispose();
});
test('denied system access does not grant or trigger repeat prompts', async () => {
  let asks = 0;
  const f = fixture({ systemPreferences: { getMediaAccessStatus: () => 'denied', askForMediaAccess: async () => { asks++; return true; } } });
  assert.equal(await f.host.request(f.owner), false); assert.equal(asks, 0);
  assert.equal(f.host.check(f.contents, 'media', 'zhijun://desktop', details()), false); f.host.dispose();
});
test('generation changes and navigation revoke active grant and suppress a late system prompt result', async () => {
  let finish;
  const f = fixture({ systemPreferences: { getMediaAccessStatus: () => 'not-determined', askForMediaAccess: () => new Promise(resolve => { finish = resolve; }) } });
  const pending = f.host.request(f.owner); await new Promise(resolve => setImmediate(resolve));
  await assert.rejects(f.host.request(f.owner), { code: 'RESOURCE_EXHAUSTED' });
  f.change(); f.host.revoke(f.owner); finish(true); await assert.rejects(pending, { code: 'STALE_GENERATION' });
  assert.equal(f.host.check(f.contents, 'media', 'zhijun://desktop', details()), false); f.host.dispose();
  const navigation = fixture({ systemPreferences: { getMediaAccessStatus: () => 'not-determined', askForMediaAccess: () => new Promise(resolve => { finish = resolve; }) } });
  const waiting = navigation.host.request(navigation.owner); await new Promise(resolve => setImmediate(resolve));
  navigation.host.revoke(); finish(true); await assert.rejects(waiting, { code: 'STALE_GENERATION' }); navigation.host.dispose();
  const granted = fixture(); await granted.host.request(granted.owner); granted.change();
  assert.equal(granted.host.check(granted.contents, 'media', 'zhijun://desktop', details()), false); granted.host.dispose();
});
test('background windows and missing usage description cannot open a system prompt', async () => {
  const f = fixture(); f.blur(); await assert.rejects(f.host.request(f.owner), { code: 'ACCESS_DENIED' }); assert.equal(f.calls(), 0); f.host.dispose();
  const invalid = fixture({ verifyUsage: async () => { throw Object.assign(new Error('configuration'), { code: 'CONFIGURATION_REQUIRED' }); } });
  await assert.rejects(invalid.host.request(invalid.owner), { code: 'CONFIGURATION_REQUIRED' }); assert.equal(invalid.calls(), 0); invalid.host.dispose();
});
test('macOS bundle metadata verifier checks a non-empty usage description without invoking permission APIs', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-microphone-plist-')); t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const macos = path.join(directory, 'Contents/MacOS'); await fs.mkdir(macos, { recursive: true });
  const plist = path.join(directory, 'Contents/Info.plist'), executable = path.join(macos, 'Zhijun');
  await assert.rejects(verifyUsageDescription(executable), { code: 'CONFIGURATION_REQUIRED' });
  await fs.writeFile(plist, '<plist><dict></dict></plist>'); await assert.rejects(verifyUsageDescription(executable), { code: 'CONFIGURATION_REQUIRED' });
  await fs.writeFile(plist, `<plist><dict><key>NSMicrophoneUsageDescription</key><string>${DESCRIPTION}</string></dict></plist>`); await verifyUsageDescription(executable);
  const config = require('../package.json'); assert.equal(config.build.mac.extendInfo.NSMicrophoneUsageDescription, DESCRIPTION);
});
test('product microphone IPC takes no renderer arguments and revokes on product session close', async () => {
  let owner, revocations = 0;
  const manager = createProductSession({ session: { request: async () => { throw Error('unexpected network'); } }, isCurrent: () => true,
    host: { requestMicrophone: async value => { owner = value; return true; }, revokeMicrophone: value => { assert.equal(value, owner); revocations++; } } });
  await assert.rejects(manager.invoke('requestMicrophone', { deviceId: 'forged' }), { code: 'INVALID_REQUEST' });
  assert.deepEqual(await manager.invoke('requestMicrophone', undefined), { allowed: true }); assert.equal(owner(), true);
  manager.close(); assert.equal(owner(), false); assert.equal(revocations, 1);
});
