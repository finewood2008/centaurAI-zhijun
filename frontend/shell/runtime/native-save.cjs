'use strict';
const fs = require('node:fs/promises');
const path = require('node:path');
const crypto = require('node:crypto');
const { DesktopError } = require('./public-error.cjs');
// Only main owns the save dialog, destination and temporary file. Renderer gets
// a boolean and never a host path. Publish only a complete, validated stream.
function createNativeSave({ dialog, getWindow, filesystem = fs }) {
  let busy = false;
  return async ({ fileName, source, isCurrent }) => {
    if (busy) throw new DesktopError('RESOURCE_EXHAUSTED');
    busy = true;
    try {
    if (!isCurrent()) throw new DesktopError('STALE_GENERATION');
    const options = { title: '保存文件', defaultPath: fileName, buttonLabel: '保存',
      properties: ['createDirectory', 'showOverwriteConfirmation'] };
    const window = getWindow();
    const result = window && !window.isDestroyed() ? await dialog.showSaveDialog(window, options) : await dialog.showSaveDialog(options);
    if (result.canceled || !result.filePath) return false;
    if (!isCurrent()) throw new DesktopError('STALE_GENERATION');
    const target = result.filePath;
    const temporary = path.join(path.dirname(target), `.zhijun-save-${crypto.randomBytes(16).toString('hex')}.tmp`);
    let file;
    try {
      file = await filesystem.open(temporary, 'wx', 0o600);
      for await (const chunk of source) {
        if (!isCurrent()) throw new DesktopError('STALE_GENERATION');
        let offset = 0;
        while (offset < chunk.length) { const result = await file.write(chunk, offset, chunk.length - offset); offset += result.bytesWritten; }
      }
      if (!isCurrent()) throw new DesktopError('STALE_GENERATION');
      await file.sync(); await file.close(); file = null;
      if (!isCurrent()) throw new DesktopError('STALE_GENERATION');
      await filesystem.rename(temporary, target);
      return true;
    } finally { await file?.close().catch(() => {}); await filesystem.unlink(temporary).catch(() => {}); }
    } finally { busy = false; }
  };
}
module.exports = { createNativeSave };
