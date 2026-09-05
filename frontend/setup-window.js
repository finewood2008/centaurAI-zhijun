"use strict";
/**
 * 可信 Setup BrowserWindow（WP M）。
 *
 * 指南安全约束：仅独立可信内部 origin 的 Setup 窗口使用 Web Bluetooth；
 * sandbox=true、contextIsolation=true、nodeIntegration=false；禁用任意导航、
 * 新窗口与生产 DevTools；requestDevice 只能由用户手势触发。本窗口只承载
 * renderer/setup 的 ViewState 消费，不持有 Consumer Token、私钥或 Wi-Fi 明文。
 *
 * 安全默认值一律收紧；打开窗口前由调用方（main.js）先过 Feature Gate，未开启时
 * 窗口本身也不可创建。与主窗口不同，Setup 窗口强制保持 webSecurity=true（同源
 * 策略生效），不得为调试放宽。
 */
const { BrowserWindow } = require("electron");
const path = require("path");

const SETUP_WINDOW_WIDTH = 720;
const SETUP_WINDOW_HEIGHT = 900;

function createSetupWindow({ setupHtmlPath = path.join(__dirname, "renderer", "setup.html") } = {}) {
  const win = new BrowserWindow({
    width: SETUP_WINDOW_WIDTH,
    height: SETUP_WINDOW_HEIGHT,
    resizable: false,
    movable: true,
    minimizable: false,
    maximizable: false,
    fullscreenable: false,
    autoHideMenuBar: true,
    title: "添加 AI 盒子",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
      devTools: process.env.NODE_ENV === "development",
    },
  });
  win.setMenu(null);
  win.setMenuBarVisibility(false);
  win.webContents.on("will-navigate", (event) => event.preventDefault());
  win.webContents.setWindowOpenHandler(() => ({ action: "deny" }));
  win.loadFile(setupHtmlPath);
  return win;
}

/** 硬化 Setup 会话权限：默认拒绝一切权限请求（Web Bluetooth 按 Gate 再放行）。 */
function hardenSetupSession(session) {
  session.setPermissionCheckHandler((_webContents, _permission) => {
    // 骨架阶段 Web Bluetooth 未启用（electronWebBluetoothDiscoveryV1 关闭），一律拒绝。
    return false;
  });
  session.setPermissionRequestHandler((_webContents, _permission, callback) => {
    callback(false);
  });
  session.setDevicePermissionHandler(() => false);
}

module.exports = { createSetupWindow, hardenSetupSession, SETUP_WINDOW_WIDTH, SETUP_WINDOW_HEIGHT };
