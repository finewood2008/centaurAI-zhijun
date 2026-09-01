"use strict";
/**
 * Electron BLE Adapter（WP N）。
 *
 * 指南约束：
 * - requestDevice 只能由用户手势触发；禁止后台扫描、自动选择与 acceptAllDevices；
 * - 候选过滤只接受 GATT v2 Service 匹配的候选（chooser handoff 经
 *   'select-bluetooth-device' 事件把决策交还用户显式选择）；
 * - 每个 GATT 分片按向量逐字节通过，异常分片/断线/超时不解析半帧；
 * - Renderer 不持有 Consumer Token、Client 私钥、rootSecret 或 Wi-Fi 明文。
 *
 * 骨架阶段：Discovery Gate（electronWebBluetoothDiscoveryV1）默认关闭，discover()
 * 一律拒绝；GATT 传输（connect/read/write/notify）以依赖注入的 transport 抽象占位，
 * 阶段 4 接入真实 Electron Web Bluetooth transport。本模块逻辑部分（候选过滤、
 * 命令分片、Gate 守卫）可在 node 环境单测。
 */
const {
  GATT_V2_SERVICE_UUID,
  GATT_V2_CHARACTERISTICS,
  BLE_EVENTS,
  isCandidateSupported,
} = require("./ble-contracts.js");
const { frameCommand } = require("./command-framing.js");

class BleAdapter {
  /**
   * @param {object} deps
   * @param {boolean} deps.discoveryEnabled   Discovery Gate 开关
   * @param {object} [deps.transport]         底层 GATT 传输（阶段 4 注入）
   * @param {object} [deps.claimCoordinator]  ClaimCoordinator（WP L 契约）
   */
  constructor({ discoveryEnabled, transport = null, claimCoordinator = null } = {}) {
    this.discoveryEnabled = discoveryEnabled === true;
    this.transport = transport;
    this.claimCoordinator = claimCoordinator;
    this._connectedDevice = null;
  }

  /** 仅用户手势触发的发现：gate 关闭、acceptAllDevices、后台扫描一律拒绝。 */
  async discover({ acceptAllDevices = false } = {}) {
    if (!this.discoveryEnabled) {
      throw new Error("设备发现未开放（electronWebBluetoothDiscoveryV1 关闭）");
    }
    if (acceptAllDevices) {
      throw new Error("禁止 acceptAllDevices：必须由用户在 chooser 中明确选择候选");
    }
    if (!this.transport) {
      throw new Error("BLE transport 未接入（阶段 4）");
    }
    const device = await this.transport.requestDevice({
      filters: [{ services: [GATT_V2_SERVICE_UUID] }],
      optionalServices: Object.values(GATT_V2_CHARACTERISTICS),
    });
    if (!isCandidateSupported(device, GATT_V2_SERVICE_UUID)) {
      throw new Error("候选不匹配 GATT v2 Service，拒绝连接");
    }
    return device;
  }

  async connect(device) {
    if (!this.transport) throw new Error("BLE transport 未接入（阶段 4）");
    this._connectedDevice = device;
    return this.transport.connect(device);
  }

  async disconnect() {
    if (this._connectedDevice && this.transport) {
      await this.transport.disconnect(this._connectedDevice);
    }
    this._connectedDevice = null;
  }

  /** 读取 Device Info characteristic（骨架：返回原始 hex，阶段 4 按向量解析）。 */
  async readDeviceInfo(server) {
    if (!this.transport) throw new Error("BLE transport 未接入（阶段 4）");
    return this.transport.read(server, GATT_V2_CHARACTERISTICS.deviceInfo);
  }

  /** 命令分片发送：先 frameCommand 校验（长度/hex/上限），再逐帧下发。 */
  async sendCommand(server, payloadHex, { onFrameSent, chunkSize, maxTotal } = {}) {
    if (!this.transport) throw new Error("BLE transport 未接入（阶段 4）");
    const frames = frameCommand(payloadHex, {
      ...(chunkSize !== undefined ? { chunkSize } : {}),
      ...(maxTotal !== undefined ? { maxTotal } : {}),
    });
    for (const frame of frames) {
      await this.transport.write(server, GATT_V2_CHARACTERISTICS.command, frame);
      if (onFrameSent) onFrameSent(frame);
    }
    return frames.length;
  }

  /** Status notify 订阅：只透传事件枚举，不做任何业务分支。 */
  subscribeStatus(server, onStatus) {
    if (!this.transport) throw new Error("BLE transport 未接入（阶段 4）");
    this.transport.subscribe(server, GATT_V2_CHARACTERISTICS.status, (raw) => {
      onStatus({ event: BLE_EVENTS.status, raw });
    });
  }

  /** 把 chooser handoff 处理器接到 Setup 窗口的 webContents（Electron 事件）。 */
  installChooserHandoff(webContents) {
    if (!webContents || typeof webContents.on !== "function") {
      throw new Error("需要 Electron WebContents 安装 chooser handoff");
    }
    // Electron 在 renderer 发起 requestDevice（用户手势）时触发该事件；
    // 主进程在此只做候选收口：不支持 GATT v2 的候选一律从 chooser 移除。
    webContents.on("select-bluetooth-device", (_event, deviceList, callback) => {
      const supported = deviceList.filter((device) => isCandidateSupported(device, GATT_V2_SERVICE_UUID));
      if (supported.length === 0) {
        callback("");
        return;
      }
      if (supported.length === 1) {
        callback(supported[0].deviceId);
        return;
      }
      callback(""); // 多个候选：不自动选择，等待用户在 chooser 中明确选择
    });
  }
}

module.exports = { BleAdapter };
