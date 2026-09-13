'use strict';
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs/promises');
const os = require('node:os');
const path = require('node:path');
const { createFileSecurity, validateWindowsAcl } = require('../production/file-security.cjs');

const sid = 'S-1-5-21-100-200-300-1001';
const safeAcl = () => ({ currentSid: sid, owner: sid, reparse: false,
  access: [{ sid, rights: 2032127, allow: true }, { sid: 'S-1-5-18', rights: 2032127, allow: true },
    { sid: 'S-1-1-0', rights: 1179817, allow: true }] });
const stat = mode => ({ mode, isSymbolicLink: () => false });

test('Windows ACL accepts read-only public access and trusted service writes, not DOS stat.mode', async () => {
  let calls = 0;
  const security = createFileSecurity({ platform: 'win32', run: async () => { calls++; return { stdout: JSON.stringify(safeAcl()) }; } });
  await security.assertSafe('C:\\Users\\synthetic\\config.json', stat(0o666));
  assert.equal(calls, 1);
  const value = safeAcl();
  value.access.push({ sid: 'S-1-5-80-956008885-3418522649-1831038044-1853292631-2271478464', rights: 2032127, allow: true });
  validateWindowsAcl(value);
});

test('Windows ACL rejects untrusted writers, delete-child, ACL/owner changes and reparse/invalid output', () => {
  for (const rights of [2, 4, 16, 64, 256, 65536, 262144, 524288, 0x10000000, 0x40000000]) {
    const value = safeAcl(); value.access.push({ sid: 'S-1-5-32-545', rights, allow: true });
    assert.throws(() => validateWindowsAcl(value));
  }
  for (const change of [{ reparse: true }, { owner: 'S-1-5-21-100-200-300-1002' }, { access: [] }, { currentSid: null }]) {
    assert.throws(() => validateWindowsAcl({ ...safeAcl(), ...change }));
  }
  const value = safeAcl(); value.access.push({ sid: 'S-1-1-0', rights: 2032127, allow: false });
  assert.doesNotThrow(() => validateWindowsAcl(value));
});

test('Windows permission process is bounded, no shell or interpolated path executable text', async () => {
  const filename = "C:\\Users\\synthetic\\a';Write-Error injected;#\\config.json";
  const security = createFileSecurity({ platform: 'win32', systemRoot: 'C:\\Windows', run: async (executable, args, options) => {
    assert.equal(executable, 'C:\\Windows\\System32\\WindowsPowerShell\\v1.0\\powershell.exe');
    assert.deepEqual(args.slice(0, 4), ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand']);
    const script = Buffer.from(args[4], 'base64').toString('utf16le');
    assert.ok(!script.includes(filename));
    assert.ok(script.includes(Buffer.from(filename, 'utf16le').toString('base64')));
    assert.ok(script.includes('Unsafe parent permissions'));
    assert.ok(!script.includes('Set-Acl'));
    assert.equal(options.timeout, 10000); assert.equal(options.maxBuffer, 65536);
    assert.equal(options.shell, undefined); assert.equal(options.windowsHide, true);
    return { stdout: JSON.stringify(safeAcl()) };
  } });
  await security.assertSafe(filename, stat(0o666));
});

test('Windows permission probe failures, malformed output and symlinks fail closed', async () => {
  for (const run of [async () => { throw new Error('unavailable'); }, async () => ({ stdout: 'not JSON' }), async () => ({ stdout: '{}' })]) {
    const security = createFileSecurity({ platform: 'win32', run });
    await assert.rejects(security.assertSafe('C:\\fixture', stat(0o444)));
  }
  const security = createFileSecurity({ platform: 'win32', run: () => { throw new Error('must not execute'); } });
  await assert.rejects(security.assertSafe('C:\\fixture', { isSymbolicLink: () => true }));
});

test('POSIX still enforces group/world write prohibition without PowerShell', async () => {
  const security = createFileSecurity({ platform: 'darwin', run: () => { throw new Error('must not execute'); } });
  await security.assertSafe('/fixture', stat(0o644));
  for (const mode of [0o664, 0o646, 0o666]) await assert.rejects(security.assertSafe('/fixture', stat(mode)));
});

test('synchronous release-config ACL probe shares the fixed read-only script and process limits', () => {
  let calls = 0;
  const security = createFileSecurity({ platform: 'win32', runSync: (executable, args, options) => {
    calls++;
    assert.ok(executable.endsWith('\\System32\\WindowsPowerShell\\v1.0\\powershell.exe'));
    assert.deepEqual(args.slice(0, 4), ['-NoLogo', '-NoProfile', '-NonInteractive', '-EncodedCommand']);
    const script = Buffer.from(args[4], 'base64').toString('utf16le');
    assert.ok(script.includes('Get-Acl -LiteralPath $p'));
    assert.ok(script.includes('Unsafe parent permissions'));
    assert.ok(!script.includes('Set-Acl'));
    assert.equal(options.timeout, 10000); assert.equal(options.maxBuffer, 65536);
    assert.equal(options.shell, undefined); assert.equal(options.encoding, 'utf8');
    return JSON.stringify(safeAcl());
  } });
  assert.equal(security.assertSafeSync('C:\\Users\\synthetic\\trust.json', stat(0o666)), undefined);
  assert.equal(calls, 1);
  for (const runSync of [() => { throw new Error('unavailable'); }, () => 'bad JSON', () => '{}',
    () => JSON.stringify({ ...safeAcl(), access: [{ sid: 'S-1-1-0', rights: 2, allow: true }] })]) {
    assert.throws(() => createFileSecurity({ platform: 'win32', runSync }).assertSafeSync('C:\\fixture', stat(0o444)));
  }
});

test('synchronous POSIX release-config check preserves strict mode and rejects symlinks', () => {
  const security = createFileSecurity({ platform: 'darwin', runSync: () => { throw new Error('must not execute'); } });
  assert.doesNotThrow(() => security.assertSafeSync('/fixture', stat(0o600)));
  assert.throws(() => security.assertSafeSync('/fixture', stat(0o666)));
  assert.throws(() => security.assertSafeSync('/fixture', { isSymbolicLink: () => true }));
});

test('private Windows records disable inherited ACEs before writing and retain only trusted principals', async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-permissions-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const filename = path.join(directory, 'empty-record.tmp'); await fs.writeFile(filename, '');
  const security = createFileSecurity({ platform: 'win32', run: async (_executable, args) => {
    const script = Buffer.from(args[4], 'base64').toString('utf16le');
    assert.ok(script.includes('SetAccessRuleProtection($true,$false)'));
    assert.ok(script.includes('RemoveAccessRuleSpecific'));
    assert.ok(script.includes('Set-Acl -LiteralPath $p -AclObject $acl'));
    assert.ok(script.indexOf('Unsafe parent permissions') < script.indexOf('Set-Acl'));
    return { stdout: JSON.stringify(safeAcl()) };
  } });
  await security.protectPrivate(filename, 0o600);
});

test('actual Windows PowerShell can protect and verify a synthetic private record', { skip: process.platform !== 'win32' }, async t => {
  const directory = await fs.mkdtemp(path.join(os.tmpdir(), 'zhijun-permissions-'));
  t.after(() => fs.rm(directory, { recursive: true, force: true }));
  const security = createFileSecurity();
  await security.protectPrivate(directory, 0o700);
  const filename = path.join(directory, 'synthetic.tmp'); await fs.writeFile(filename, 'synthetic');
  await security.protectPrivate(filename, 0o600);
  await security.assertSafe(filename, await fs.lstat(filename));
  security.assertSafeSync(filename, await fs.lstat(filename));
});
