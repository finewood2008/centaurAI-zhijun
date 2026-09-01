"use strict";
/**
 * ProvisioningCryptoProvider 接口（WP M）。
 *
 * 阶段 3 只定义契约与 fail-closed 占位；真实实现由阶段 4 接入（设备 Attestation /
 * Challenge / Key Confirm / Device Proof / Owner ACK 的向量化验证）。占位实现
 * 一律抛错，保证任何未接入的调用路径不可达、不回退。
 *
 * 接口契约（JS 环境以 JSDoc + 鸭子类型约定；阶段 4 真实验证前不得被业务分支依赖）：
 *   createClientKeyPair() -> Promise<{publicKeyPem, privateKeyPem}>
 *     生成 Client 密钥对（阶段 3 仅 Main 持有，经 SecureStore 落盘）。
 *   signChallenge(challenge, clientKey) -> Promise<string>
 *     对设备 Challenge 签名，产出 App Proof 素材。
 *   verifyDeviceProof(proof, deviceInfo) -> Promise<boolean>
 *     验证设备返回的 Device Proof 与信息，失败即拒绝继续认领。
 *   deriveRootSecret(challenge, clientKey) -> Promise<string>
 *     派生 key-confirm 后的 rootSecret；仅 Main 短暂持有，用后即清。
 */
const PROVISIONING_METHODS = Object.freeze([
  "createClientKeyPair",
  "signChallenge",
  "verifyDeviceProof",
  "deriveRootSecret",
]);

class ProvisioningCryptoProvider {
  constructor() {
    this._bound = new Set();
  }

  /** 绑定真实实现（阶段 4 注入）；重复绑定或绑定后替换均拒绝。 */
  bind(impl) {
    for (const name of PROVISIONING_METHODS) {
      if (typeof impl[name] !== "function") {
        throw new Error(`ProvisioningCryptoProvider 实现缺少方法：${name}`);
      }
    }
    if (this._bound.size > 0) {
      throw new Error("ProvisioningCryptoProvider 已被绑定，禁止热替换");
    }
    this._impl = impl;
    for (const name of PROVISIONING_METHODS) {
      this._bound.add(name);
    }
  }

  get isBound() {
    return this._bound.size === PROVISIONING_METHODS.length;
  }

  async createClientKeyPair() {
    if (!this.isBound) throw new Error("Client 密钥对生成未接入（阶段 4）");
    return this._impl.createClientKeyPair();
  }

  async signChallenge(challenge, clientKey) {
    if (!this.isBound) throw new Error("Challenge 签名未接入（阶段 4）");
    return this._impl.signChallenge(challenge, clientKey);
  }

  async verifyDeviceProof(proof, deviceInfo) {
    if (!this.isBound) throw new Error("Device Proof 验证未接入（阶段 4）");
    return this._impl.verifyDeviceProof(proof, deviceInfo);
  }

  async deriveRootSecret(challenge, clientKey) {
    if (!this.isBound) throw new Error("rootSecret 派生未接入（阶段 4）");
    return this._impl.deriveRootSecret(challenge, clientKey);
  }
}

module.exports = { ProvisioningCryptoProvider, PROVISIONING_METHODS };
