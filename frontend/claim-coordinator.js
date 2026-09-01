"use strict";
/**
 * ClaimCoordinator 接口（WP L public API 契约，WP N 消费侧）。
 *
 * 指南：Local Provisioning Core 只允许一个实现来源；Android 与 Electron 只实现
 * 各自的 Discovery/Transport Adapter。本文件定义 Electron 侧对 ClaimCoordinator
 * 的调用契约与 fail-closed 占位——阶段 4 绑定本地协议 Core 的真实实现（含唯一
 * codec 与 vectors runner），未绑定前任何调用一律抛错，绝不复制状态机。
 *
 * 契约方法（全部返回 Promise）：
 *   start({deviceInfo})            预检（蓝牙关闭/服务缺失等前置条件）
 *   selectCandidate(candidateId)   明确选择候选后进入认证
 *   authenticate()                 设备 Attestation / Challenge / Key Confirm
 *   provisionWifi(ssid, password)  身份验证后的 AEAD Wi-Fi 配网
 *   appProof()                     App Proof / Device Proof 双证
 *   acknowledge()                  Owner ACK / 权威 Pairing 状态
 *   cancel(reason)                 取消（可恢复）
 *   resume()                       从取消/失败恢复
 *   snapshot()                     -> ClaimCoordinatorSnapshot（纯数据，供 ViewState）
 */
const COORDINATOR_METHODS = Object.freeze([
  "start",
  "selectCandidate",
  "authenticate",
  "provisionWifi",
  "appProof",
  "acknowledge",
  "cancel",
  "resume",
  "snapshot",
]);

class ClaimCoordinator {
  constructor() {
    this._impl = null;
  }

  get isBound() {
    return this._impl !== null;
  }

  bind(impl) {
    for (const name of COORDINATOR_METHODS) {
      if (typeof impl[name] !== "function") {
        throw new Error(`ClaimCoordinator 实现缺少方法：${name}`);
      }
    }
    if (this.isBound) {
      throw new Error("ClaimCoordinator 已被绑定，禁止热替换");
    }
    this._impl = impl;
  }

  _requireBound() {
    if (!this.isBound) throw new Error("ClaimCoordinator 未接入（阶段 4）");
  }

  async start(options) {
    this._requireBound();
    return this._impl.start(options);
  }

  async selectCandidate(candidateId) {
    this._requireBound();
    return this._impl.selectCandidate(candidateId);
  }

  async authenticate() {
    this._requireBound();
    return this._impl.authenticate();
  }

  async provisionWifi(ssid, password) {
    this._requireBound();
    return this._impl.provisionWifi(ssid, password);
  }

  async appProof() {
    this._requireBound();
    return this._impl.appProof();
  }

  async acknowledge() {
    this._requireBound();
    return this._impl.acknowledge();
  }

  async cancel(reason) {
    this._requireBound();
    return this._impl.cancel(reason);
  }

  async resume() {
    this._requireBound();
    return this._impl.resume();
  }

  snapshot() {
    this._requireBound();
    return this._impl.snapshot();
  }
}

module.exports = { ClaimCoordinator, COORDINATOR_METHODS };
