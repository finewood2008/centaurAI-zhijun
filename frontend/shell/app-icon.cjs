'use strict'
const path = require('node:path')

// The checked-in PNG and packaged ICNS both derive from mindos-web/logo.jpg.
const APP_ICON = path.join(__dirname, 'assets', 'centaur.png')

function installDockIcon(app, platform = process.platform) {
  // Electron's Dock API is macOS-only and must run after app.whenReady().
  if (platform === 'darwin') app.dock.setIcon(APP_ICON)
}

module.exports = { APP_ICON, installDockIcon }
