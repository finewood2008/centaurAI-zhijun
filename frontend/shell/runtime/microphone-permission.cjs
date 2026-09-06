'use strict';
const path = require('node:path');
const fs = require('node:fs/promises');
const { DesktopError } = require('./public-error.cjs');
const ENTRY = 'zhijun://desktop/desktop.html';
const DESCRIPTION = '在你点击录音时使用麦克风，停止后将录音发送到已连接的盒子转为文字。转写结果仅填入输入框，不会自动发送消息。';
function entry(value) { try { const url = new URL(value); url.hash = ''; return url.href === ENTRY; } catch { return false; } }
function origin(value) { return value === 'zhijun://desktop' || value === 'zhijun://desktop/'; }
async function verifyUsageDescription(executable = process.execPath) {
  const filename = path.resolve(path.dirname(executable), '../Info.plist');
  try {
    const stat = await fs.stat(filename);
    if (!stat.isFile() || stat.size > 256 * 1024) throw new DesktopError('CONFIGURATION_REQUIRED');
    const plist = await fs.readFile(filename, 'utf8');
    if (!/<key>NSMicrophoneUsageDescription<\/key>\s*<string>[^<\s][^<]*<\/string>/.test(plist)) throw new DesktopError('CONFIGURATION_REQUIRED');
  } catch { throw new DesktopError('CONFIGURATION_REQUIRED'); }
}
// This controller never starts capture. It authorizes only a subsequent, short
// audio request from the exact main frame after an explicit narrow IPC action.
function createMicrophonePermission({ getContents, systemPreferences, platform = process.platform,
  clock = () => performance.now(), grantMs = 15000, verifyUsage = verifyUsageDescription }) {
  let grant = null, pending = false, disposed = false, serial = 0, timer;
  function usableContents(contents) {
    return Boolean(contents && contents === getContents() && !contents.isDestroyed()
      && contents.mainFrame && entry(contents.getURL()) && entry(contents.mainFrame.url));
  }
  function revoke(owner) {
    if (owner === undefined) serial++;
    if (owner === undefined || grant?.owner === owner) { grant = null; clearTimeout(timer); }
    // Pending prompts retain their real single-prompt budget; the owner guard
    // discards a late decision after disconnect. No automated prompt dismissal.
  }
  function live(contents, details) {
    if (disposed || !grant) return false;
    if (clock() >= grant.expires || !grant.owner()) { grant = null; clearTimeout(timer); return false; }
    return usableContents(contents) && contents.id === grant.contentsId && contents.mainFrame === grant.frame
      && details?.isMainFrame === true && entry(details.requestingUrl)
      && (details.securityOrigin === undefined || origin(details.securityOrigin))
      && details.embeddingOrigin === undefined;
  }
  async function request(owner) {
    if (disposed || typeof owner !== 'function' || !owner()) throw new DesktopError('STALE_GENERATION');
    const contents = getContents();
    if (!usableContents(contents) || !contents.isFocused()) throw new DesktopError('ACCESS_DENIED');
    if (pending) throw new DesktopError('RESOURCE_EXHAUSTED');
    pending = true; const requestSerial = ++serial;
    // Every button starts a new short grant; a previous request cannot prolong it.
    grant = null; clearTimeout(timer);
    try {
      let allowed = true;
      if (platform === 'darwin') {
        await verifyUsage();
        if (!owner() || disposed || requestSerial !== serial || !usableContents(contents)) throw new DesktopError('STALE_GENERATION');
        const status = systemPreferences.getMediaAccessStatus('microphone');
        allowed = status === 'granted' || (status === 'not-determined' && await systemPreferences.askForMediaAccess('microphone'));
      } else if (platform === 'win32') {
        allowed = !['denied', 'restricted'].includes(systemPreferences.getMediaAccessStatus('microphone'));
      }
      if (!owner() || disposed || requestSerial !== serial || !usableContents(contents)) throw new DesktopError('STALE_GENERATION');
      if (!allowed) return false;
      grant = { owner, contentsId: contents.id, frame: contents.mainFrame, expires: clock() + grantMs };
      timer = setTimeout(() => revoke(owner), grantMs); timer.unref?.();
      return true;
    } finally { pending = false; }
  }
  function check(contents, permission, requestingOrigin, details) {
    return permission === 'media' && origin(requestingOrigin) && details?.mediaType === 'audio' && live(contents, details);
  }
  function permissionRequest(contents, permission, callback, details) {
    const allowed = permission === 'media' && Array.isArray(details?.mediaTypes) && details.mediaTypes.length === 1
      && details.mediaTypes[0] === 'audio' && live(contents, details);
    callback(Boolean(allowed));
  }
  return { request, revoke, check, permissionRequest, dispose() { disposed = true; serial++; revoke(); } };
}
module.exports = { createMicrophonePermission, verifyUsageDescription, DESCRIPTION };
