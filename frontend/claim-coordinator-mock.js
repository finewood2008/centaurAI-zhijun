"use strict";
/**
 * ClaimCoordinator deterministic fake（WP L，阶段 3 Mock 流程状态机）。
 *
 * 指南要求：Mock 可覆盖 chooser 取消、权限拒绝、蓝牙关闭、两个候选、GATT 断线、
 * 窗口关闭、Renderer crash、取消和权威恢复；页面只消费 ClaimCoordinatorSnapshot
 * 经 deriveElectronViewState 派生状态，不得复制状态机。本 fake 是「唯一实现来源」
 * 的联调替身：确定输入 → 确定输出，供 Electron/QA 在阶段 4 真实验证前完成
 * 全流程 Mock 验收。阶段 4 用真实本地协议 Core 替换，本文件不进入生产依赖图。
 *
 * 场景注入（构造选项）：
 *   bluetoothAvailable  false → start 预检失败（蓝牙关闭）
 *   candidates          候选列表（默认两个确定性候选）
 *   permissionDenied    true → selectCandidate 阶段失败（权限拒绝/chooser 取消）
 *   gattDisconnectAt    阶段名 → 该阶段模拟 GATT 断线失败
 *   ackLost             true → acknowledge 阶段 ACK 丢失失败
 *   deviceInfo 缺失      → start 预检失败
 */
const { SETUP_STAGES } = require("./setup-view-state.js");

const DEFAULT_CANDIDATES = Object.freeze([
  { id: "mock-ai-box-0001", name: "AI Box (Mock #1)", rssi: -52 },
  { id: "mock-ai-box-0002", name: "AI Box (Mock #2)", rssi: -63 },
]);

const TRANSITIONS = Object.freeze({
  [SETUP_STAGES.idle]: [SETUP_STAGES.precheck],
  [SETUP_STAGES.precheck]: [SETUP_STAGES.candidate_selection, SETUP_STAGES.failed, SETUP_STAGES.cancelled],
  [SETUP_STAGES.candidate_selection]: [SETUP_STAGES.authentication, SETUP_STAGES.failed, SETUP_STAGES.cancelled],
  [SETUP_STAGES.authentication]: [SETUP_STAGES.wifi, SETUP_STAGES.failed, SETUP_STAGES.cancelled],
  [SETUP_STAGES.wifi]: [SETUP_STAGES.app_proof, SETUP_STAGES.failed, SETUP_STAGES.cancelled],
  [SETUP_STAGES.app_proof]: [SETUP_STAGES.owner_ack, SETUP_STAGES.failed, SETUP_STAGES.cancelled],
  [SETUP_STAGES.owner_ack]: [SETUP_STAGES.done, SETUP_STAGES.failed, SETUP_STAGES.cancelled],
  [SETUP_STAGES.done]: [],
  [SETUP_STAGES.cancelled]: [SETUP_STAGES.precheck],
  [SETUP_STAGES.failed]: [SETUP_STAGES.precheck],
});

class MockClaimCoordinator {
  constructor({
    candidates = DEFAULT_CANDIDATES,
    bluetoothAvailable = true,
    permissionDenied = false,
    gattDisconnectAt = null,
    ackLost = false,
  } = {}) {
    this._candidates = candidates;
    this._bluetoothAvailable = bluetoothAvailable;
    this._permissionDenied = permissionDenied;
    this._gattDisconnectAt = gattDisconnectAt;
    this._ackLost = ackLost;
    this._stage = SETUP_STAGES.idle;
    this._selectedCandidate = null;
    this._error = null;
    this._cancelledReason = null;
    this._wifi = null;
  }

  _transition(next) {
    if (!TRANSITIONS[this._stage] || !TRANSITIONS[this._stage].includes(next)) {
      throw new Error(`非法状态转换：${this._stage} → ${next}`);
    }
    this._stage = next;
  }

  _fail(reason) {
    this._error = reason;
    this._transition(SETUP_STAGES.failed);
  }

  snapshot() {
    return {
      stage: this._stage,
      candidates: this._candidates,
      selectedCandidate: this._selectedCandidate,
      error: this._error,
      cancelledReason: this._cancelledReason,
      wifiConfigured: this._wifi !== null,
    };
  }

  async start({ deviceInfo } = {}) {
    this._transition(SETUP_STAGES.precheck);
    if (!this._bluetoothAvailable) {
      this._fail("bluetooth_unavailable");
      return this.snapshot();
    }
    if (!deviceInfo || typeof deviceInfo !== "object" || !deviceInfo.serviceUuid) {
      this._fail("device_info_missing");
      return this.snapshot();
    }
    this._transition(SETUP_STAGES.candidate_selection);
    return this.snapshot();
  }

  async selectCandidate(candidateId) {
    const candidate = this._candidates.find((item) => item.id === candidateId);
    if (!candidate) {
      this._fail("candidate_not_found");
      return this.snapshot();
    }
    if (this._permissionDenied) {
      this._fail("permission_denied");
      return this.snapshot();
    }
    this._selectedCandidate = candidate;
    this._transition(SETUP_STAGES.authentication);
    return this.snapshot();
  }

  async authenticate() {
    if (this._gattDisconnectAt === SETUP_STAGES.authentication) {
      this._fail("gatt_disconnected");
      return this.snapshot();
    }
    this._transition(SETUP_STAGES.wifi);
    return this.snapshot();
  }

  async provisionWifi(ssid, password) {
    if (!ssid || !password) {
      this._fail("wifi_credentials_missing");
      return this.snapshot();
    }
    if (this._gattDisconnectAt === SETUP_STAGES.wifi) {
      this._fail("gatt_disconnected");
      return this.snapshot();
    }
    this._wifi = { ssid };
    this._transition(SETUP_STAGES.app_proof);
    return this.snapshot();
  }

  async appProof() {
    if (this._gattDisconnectAt === SETUP_STAGES.app_proof) {
      this._fail("gatt_disconnected");
      return this.snapshot();
    }
    this._transition(SETUP_STAGES.owner_ack);
    return this.snapshot();
  }

  async acknowledge() {
    if (this._ackLost) {
      this._fail("ack_timeout");
      return this.snapshot();
    }
    this._transition(SETUP_STAGES.done);
    return this.snapshot();
  }

  async cancel(reason = "user_cancelled") {
    if (this._stage === SETUP_STAGES.done) {
      throw new Error("已完成认领，不可取消");
    }
    this._cancelledReason = reason;
    this._transition(SETUP_STAGES.cancelled);
    return this.snapshot();
  }

  async resume() {
    if (this._stage !== SETUP_STAGES.cancelled && this._stage !== SETUP_STAGES.failed) {
      throw new Error("仅取消/失败状态可恢复");
    }
    this._error = null;
    this._cancelledReason = null;
    this._transition(SETUP_STAGES.precheck);
    this._transition(SETUP_STAGES.candidate_selection);
    return this.snapshot();
  }
}

module.exports = { MockClaimCoordinator, DEFAULT_CANDIDATES };
