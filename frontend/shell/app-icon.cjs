'use strict'
const path = require('node:path')

// The PNG, ICNS and in-app mark share assets/zhijun-mark.json vector paths.
const APP_ICON = path.join(__dirname, 'assets', 'zhijun.png')

function installDockIcon(app, platform = process.platform) {
  // Electron's Dock API is macOS-only and must run after app.whenReady().
  if (platform === 'darwin') app.dock.setIcon(APP_ICON)
}

module.exports = { APP_ICON, installDockIcon }
