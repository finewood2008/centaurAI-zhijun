const { app, BrowserWindow, Tray, Menu, ipcMain, dialog } = require('electron');
const path = require('path');
const fs = require('fs/promises');
const { BackendRpc } = require('./backend-rpc');
const { ConsumerClient } = require('./consumer-client');
const { IPC } = require('./ipc-channels');
const { FEATURE_GATES, isEnabled, setOverride } = require('./feature-gates');
const { ProvisioningCryptoProvider } = require('./provisioning-crypto-provider');
const { SecureStore } = require('./secure-store');
const { createSetupWindow, hardenSetupSession } = require('./setup-window');
const { BleAdapter } = require('./ble-adapter');
const { ClaimCoordinator } = require('./claim-coordinator');

let mainWindow;
let setupWindow = null;
let tray = null;
const hasSingleInstanceLock = app.requestSingleInstanceLock();

// 主进程托管的 Python 后端（stdio 点对点宿主），桌面渲染端经 'rpc' 走它。
const backendRpc = new BackendRpc();
// 阶段 2：Consumer Client（账号/Owner/认领控制面只在本宿主侧执行）。
const consumerClient = new ConsumerClient();
// 阶段 3（WP M/N）：本地添加骨架装配——Feature Gate 默认全关，任何本地添加入口不可达。
// SecureStore 仅在 whenReady 后初始化；ProvisioningCryptoProvider/ClaimCoordinator 由
// 阶段 4 绑定真实实现；BLE Adapter 的 Discovery Gate 默认关闭。
const provisioningServices = {
  gates: FEATURE_GATES,
  crypto: new ProvisioningCryptoProvider(),
  secureStore: null,
  bleAdapter: null,
  claimCoordinator: null,
};

// 本地添加骨架仅限开发/联调验收：未打包 + 显式环境变量才允许开启
// localDeviceAddSkeleton（生产包 app.isPackaged=true 时该分支不可达）。
if (!app.isPackaged && process.env.MINDOS_ENABLE_LOCAL_ADD_SKELETON === '1') {
  setOverride('localDeviceAddSkeleton', true);
}

// 渲染端 RPC：把 {method,uri,headers,body,form,file} 转发到已托管后端，返回协议帧
// {status, body, bodyBase64, error}。后端不可用 / 启动中会自动排队等待或报错。
ipcMain.handle('rpc', async (_event, req) => backendRpc.rpc(req || {}));

// 阶段 2 受控连通票据：在票据模式下由本宿主侧的 Consumer Client 完成
// 登录→认领→创建连接票据，投放给渲染端一次性消费（nonce 单次使用）。
// 由后端 access-context 判定运行模式并取本机设备标识；本地调试模式返回 null。
ipcMain.handle('mindos:connectivity-ticket', async () => {
  await backendRpc.ready.catch(() => undefined);
  let ctx = null;
  try {
    const frame = await backendRpc.rpc({ method: 'GET', uri: '/api/mindos/access-context' });
    try {
      ctx = JSON.parse(frame.body || '{}');
    } catch {
      ctx = null;
    }
  } catch {
    ctx = null;
  }
  if (!ctx || ctx.mode !== 'connectivity_ticket_required' || !ctx.deviceId) return null;
  try {
    return await consumerClient.provision({ deviceId: ctx.deviceId });
  } catch (err) {
    console.error('[connectivity] Consumer 连接闭环失败：', err && err.message);
    return null;
  }
});

// 渲染端构建媒体 URL / 判断后端地址用（图片、视频这类流式预览仍走 loopback HTTP 更稳）
ipcMain.handle('backend:base-url', () => backendRpc.baseUrl);

ipcMain.handle('save-mcp-ca', async () => {
  const response = await fetch('http://127.0.0.1:8618/api/mcp/ca.crt');
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  const result = await dialog.showSaveDialog({
    title: '保存 CentaurAI MCP 根证书',
    defaultPath: 'centaurai-memory-ca.crt',
    filters: [{ name: 'CA Certificate', extensions: ['crt'] }],
  });
  if (result.canceled || !result.filePath) return { saved: false };
  await fs.writeFile(result.filePath, Buffer.from(await response.arrayBuffer()), { mode: 0o644 });
  return { saved: true, path: result.filePath };
});

function createWindow() {
  mainWindow = new BrowserWindow({
    width: 1000,
    height: 700,
    minWidth: 520,
    minHeight: 360,
    resizable: true,
    movable: true,
    minimizable: true,
    maximizable: true,
    fullscreenable: true,
    autoHideMenuBar: true,
    title: '半人马AI 个人记忆库',
    icon: path.join(__dirname, 'assets', 'icon-256.png'),
    webPreferences: {
      preload: path.join(__dirname, 'preload.js'),
      contextIsolation: true,
      // 显式钉死安全默认值，防后续误改放大攻击面（webSecurity:false 已关同源策略，
      // 这些必须保持收紧；渲染端只载本地内容、不开 node 集成、保留沙箱）。
      nodeIntegration: false,
      sandbox: true,
      // 渲染端只加载本地 index.html、只与 127.0.0.1:8618 通信；关掉 webSecurity 让它
      // 直读 loopback 响应、不依赖后端发宽松 CORS——后端遂可去掉 allow_origins:"*"，
      // 真实浏览器(强制同源策略)便无法跨域读取本地库内容。所有后端来源字符串经 escapeHtml
      // (含引号)入 DOM，避免渲染端注入。
      webSecurity: false,
    },
  });

  // 不使用 Electron 默认的 File / Edit / View 菜单，保留原生窗口边框供拖拽缩放。
  mainWindow.setMenu(null);
  mainWindow.setMenuBarVisibility(false);
  mainWindow.setResizable(true);
  mainWindow.loadFile(path.join(__dirname, 'renderer', 'index.html'));

  // 关闭窗口 → 最小化到托盘
  mainWindow.on('close', (event) => {
    if (tray) {
      event.preventDefault();
      mainWindow.hide();
    }
  });
}

function createTray() {
  const iconPath = path.join(__dirname, 'tray-icon.png');
  tray = new Tray(iconPath);
  tray.setToolTip('半人马AI 私有记忆库');

  const contextMenu = Menu.buildFromTemplate([
    {
      label: '显示窗口',
      click: () => {
        mainWindow.show();
        mainWindow.focus();
      },
    },
    { type: 'separator' },
    {
      label: '退出',
      click: () => {
        tray = null; // 防止 close 事件拦截
        app.quit();
      },
    },
  ]);

  tray.setContextMenu(contextMenu);

  // 点击托盘图标显示窗口
  tray.on('click', () => {
    if (mainWindow.isVisible()) {
      mainWindow.hide();
    } else {
      mainWindow.show();
      mainWindow.focus();
    }
  });
}

// 阶段 3（WP M）：Setup 窗口受控通道。Feature Gate 未开启时，任何调用
// （打开/关闭/取状态/开始/取消/恢复）一律拒绝，不保留隐藏入口。
function registerSetupIpc() {
  const gateOpen = () => {
    if (!isEnabled('localDeviceAddSkeleton')) {
      throw new Error('本地添加功能尚未开放');
    }
    if (!provisioningServices.claimCoordinator) {
      throw new Error('认领流程未装配');
    }
  };

  // 状态变更推送：main → setup renderer 的受控事件（snapshot 纯数据）。
  const pushSetupState = (snapshot) => {
    if (setupWindow && !setupWindow.isDestroyed()) {
      setupWindow.webContents.send(IPC.setup.stateChanged, snapshot);
    }
    return snapshot;
  };

  ipcMain.handle(IPC.setup.open, async () => {
    gateOpen();
    return openSetupWindow();
  });

  ipcMain.handle(IPC.setup.close, async () => {
    gateOpen();
    if (setupWindow && !setupWindow.isDestroyed()) setupWindow.close();
    return { closed: true };
  });

  ipcMain.handle(IPC.setup.state, async () => {
    gateOpen();
    return provisioningServices.claimCoordinator.snapshot();
  });

  ipcMain.handle(IPC.setup.start, async (_event, deviceInfo) => {
    gateOpen();
    return pushSetupState(await provisioningServices.claimCoordinator.start({ deviceInfo }));
  });

  ipcMain.handle(IPC.setup.selectCandidate, async (_event, candidateId) => {
    gateOpen();
    return pushSetupState(await provisioningServices.claimCoordinator.selectCandidate(candidateId));
  });

  ipcMain.handle(IPC.setup.authenticate, async () => {
    gateOpen();
    return pushSetupState(await provisioningServices.claimCoordinator.authenticate());
  });

  ipcMain.handle(IPC.setup.provisionWifi, async (_event, credentials) => {
    gateOpen();
    const { ssid, password } = credentials || {};
    return pushSetupState(await provisioningServices.claimCoordinator.provisionWifi(ssid, password));
  });

  ipcMain.handle(IPC.setup.appProof, async () => {
    gateOpen();
    return pushSetupState(await provisioningServices.claimCoordinator.appProof());
  });

  ipcMain.handle(IPC.setup.acknowledge, async () => {
    gateOpen();
    return pushSetupState(await provisioningServices.claimCoordinator.acknowledge());
  });

  ipcMain.handle(IPC.setup.cancel, async (_event, reason) => {
    gateOpen();
    return pushSetupState(await provisioningServices.claimCoordinator.cancel(reason));
  });

  ipcMain.handle(IPC.setup.resume, async () => {
    gateOpen();
    return pushSetupState(await provisioningServices.claimCoordinator.resume());
  });

  // BLE（WP N）：Discovery Gate 未开启时，扫描/选择/状态/断开一律拒绝。
  const bleGate = () => {
    if (!isEnabled('electronWebBluetoothDiscoveryV1')) {
      throw new Error('设备发现未开放（electronWebBluetoothDiscoveryV1 关闭）');
    }
    if (!provisioningServices.bleAdapter) {
      throw new Error('BLE Adapter 未就绪');
    }
  };

  ipcMain.handle(IPC.setup.ble.scan, async () => {
    bleGate();
    return provisioningServices.bleAdapter.discover();
  });

  ipcMain.handle(IPC.setup.ble.select, async () => {
    bleGate();
    throw new Error('候选选择/认领流程尚未接入（阶段 4）');
  });

  ipcMain.handle(IPC.setup.ble.status, async () => {
    bleGate();
    return { connected: false, status: 'idle' };
  });

  ipcMain.handle(IPC.setup.ble.disconnect, async () => {
    bleGate();
    await provisioningServices.bleAdapter.disconnect();
    return { disconnected: true };
  });
}

/** 打开（或聚焦）Setup 窗口；窗口关闭时对未完成流程执行取消。 */
function openSetupWindow() {
  if (setupWindow && !setupWindow.isDestroyed()) {
    setupWindow.focus();
    return { opened: true, reused: true };
  }
  hardenSetupSession(require('electron').session.defaultSession);
  setupWindow = createSetupWindow();
  setupWindow.on('closed', () => {
    const coordinator = provisioningServices.claimCoordinator;
    if (coordinator) {
      try {
        const stage = coordinator.snapshot().stage;
        if (stage !== 'done' && stage !== 'cancelled' && stage !== 'idle') {
          coordinator.cancel('window-closed').catch(() => {});
        }
      } catch {}
    }
    setupWindow = null;
  });
  setupWindow.webContents.on('render-process-gone', (_event, details) => {
    console.error('[setup] renderer crash：', details && details.reason);
    if (setupWindow && !setupWindow.isDestroyed()) setupWindow.destroy();
  });
  return { opened: true, reused: false };
}

if (!hasSingleInstanceLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    if (!mainWindow) return;
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
  });

  app.whenReady().then(() => {
    Menu.setApplicationMenu(null);
    // 阶段 3：初始化 Main 安全存储（safeStorage 不可用时仅禁用本地添加骨架，
    // 不影响阶段 2 票据桥等主流程）。
    try {
      provisioningServices.secureStore = new SecureStore({
        userDataDir: app.getPath('userData'),
        safeStorage: require('electron').safeStorage,
      });
      provisioningServices.secureStore.init();
    } catch (err) {
      console.error('[secure-store] 初始化失败：', err && err.message);
      provisioningServices.secureStore = null;
    }
    provisioningServices.bleAdapter = new BleAdapter({
      discoveryEnabled: isEnabled('electronWebBluetoothDiscoveryV1'),
    });
    // 阶段 3：本地添加骨架开启时装配 MockClaimCoordinator（仅联调；生产 gate 关闭
    // 时保持 null，任何 setup 调用经 gateOpen 拒绝）。阶段 4 以真实 Core 替换。
    if (isEnabled('localDeviceAddSkeleton')) {
      const { MockClaimCoordinator } = require('./claim-coordinator-mock');
      const coordinator = new ClaimCoordinator();
      coordinator.bind(new MockClaimCoordinator());
      provisioningServices.claimCoordinator = coordinator;
    }
    registerSetupIpc();
    // 启动/托管后端子进程；就绪由渲染端的首个 health 探活统一等待。
    backendRpc.start().catch((err) => {
      console.error('[backend] 启动失败：', err && err.message);
    });
    createWindow();
    createTray();
    // 验收模式：本地添加骨架 + 显式环境变量时自动打开 Setup 窗口（开发机 CDP 验收用）。
    if (isEnabled('localDeviceAddSkeleton') && process.env.MINDOS_OPEN_SETUP_ON_START === '1') {
      openSetupWindow();
    }
  });

  // 退出前优雅停掉后端子进程，让 ChromaDB 干净关闭、锁及时释放；
  // 同时清除安全存储内存中的密钥句柄（不落普通日志/Storage）。
  app.on('before-quit', () => {
    if (provisioningServices.secureStore) provisioningServices.secureStore.clearMemory();
    backendRpc.stop();
  });

  app.on('window-all-closed', () => {
    // 不退出，托盘保持运行
  });
}
