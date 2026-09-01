"use strict";
/**
 * Discovery Contracts（WP J，device-discovery-contracts 骨架）。
 *
 * 指南要求：Discovery candidate 仅是本地未验证候选，不包含或持久化权威 `device_id`；
 * 候选生命周期、错误码与 conformance fixtures 有唯一解释来源；Android/Electron 只实现
 * 各自的 Discovery/Transport Adapter。本模块是纯契约层（不 import electron），
 * 阶段 4 以 SDK 交付的 conformance vectors 替换/对齐。
 *
 * 候选对象规范（本地视角）：
 *   { id, name, rssi, serviceUuids: string[], state, lastSeenAt }
 *   - id 仅为本地候选标识（如 BluetoothDevice.id），绝不等于 Consumer device_id；
 *   - 候选在 verified 前 UI 必须标记「尚未验证」。
 */
const { GATT_V2_SERVICE_UUID } = require("./ble-contracts.js");

const CANDIDATE_STATES = Object.freeze({
  discovered: "discovered",
  selected: "selected",
  connecting: "connecting",
  verified: "verified",
  claimed: "claimed",
  failed: "failed",
  stale: "stale",
  cancelled: "cancelled",
});

const DISCOVERY_ERRORS = Object.freeze({
  bluetooth_unavailable: "bluetooth_unavailable",
  permission_denied: "permission_denied",
  canceled: "canceled",
  not_supported: "not_supported",
  timeout: "timeout",
  gatt_disconnected: "gatt_disconnected",
});

const CANDIDATE_TRANSITIONS = Object.freeze({
  [CANDIDATE_STATES.discovered]: [CANDIDATE_STATES.selected, CANDIDATE_STATES.stale, CANDIDATE_STATES.cancelled],
  [CANDIDATE_STATES.selected]: [CANDIDATE_STATES.connecting, CANDIDATE_STATES.cancelled],
  [CANDIDATE_STATES.connecting]: [CANDIDATE_STATES.verified, CANDIDATE_STATES.failed, CANDIDATE_STATES.cancelled],
  [CANDIDATE_STATES.verified]: [CANDIDATE_STATES.claimed, CANDIDATE_STATES.failed],
  [CANDIDATE_STATES.claimed]: [],
  [CANDIDATE_STATES.failed]: [],
  [CANDIDATE_STATES.stale]: [],
  [CANDIDATE_STATES.cancelled]: [],
});

const CANDIDATE_STALE_AFTER_MS = 15000;

/** 候选是否支持目标服务（chooser 过滤与 conformance 共用）。 */
function candidateSupportsService(candidate, serviceUuid = GATT_V2_SERVICE_UUID) {
  if (!candidate || !Array.isArray(candidate.serviceUuids)) return false;
  return candidate.serviceUuids.some((uuid) => String(uuid).toLowerCase() === String(serviceUuid).toLowerCase());
}

/** 候选是否已过期（超过 CANDIDATE_STALE_AFTER_MS 未见 → 标记 stale）。 */
function isCandidateStale(candidate, now = Date.now()) {
  if (!candidate || typeof candidate.lastSeenAt !== "number") return false;
  return now - candidate.lastSeenAt > CANDIDATE_STALE_AFTER_MS;
}

/** 生命周期推进：非法转换抛错；候选绝不产生权威 device_id 语义。 */
function transitionCandidate(candidate, nextState) {
  const current = candidate.state;
  const allowed = CANDIDATE_TRANSITIONS[current] || [];
  if (!allowed.includes(nextState)) {
    throw new Error(`非法候选状态转换：${current} → ${nextState}`);
  }
  return { ...candidate, state: nextState };
}

/** 规范化原始设备对象为本地候选；过滤掉不支持服务与过期项。 */
function normalizeCandidate(raw) {
  if (!raw || typeof raw !== "object") return null;
  const serviceUuids = Array.isArray(raw.serviceUuids) ? raw.serviceUuids.map(String) : [];
  if (!candidateSupportsService({ serviceUuids }, GATT_V2_SERVICE_UUID)) return null;
  return {
    id: String(raw.id || raw.deviceId || ""),
    name: String(raw.name || raw.id || "未知设备"),
    rssi: typeof raw.rssi === "number" ? raw.rssi : null,
    serviceUuids,
    state: CANDIDATE_STATES.discovered,
    lastSeenAt: typeof raw.lastSeenAt === "number" ? raw.lastSeenAt : Date.now(),
  };
}

const CONFORMANCE_FIXTURES = Object.freeze({
  supported: Object.freeze([
    Object.freeze({ id: "local-device-0001", name: "AI Box (Fixture #1)", rssi: -52, serviceUuids: [GATT_V2_SERVICE_UUID], lastSeenAt: 0 }),
    Object.freeze({ id: "local-device-0002", name: "AI Box (Fixture #2)", rssi: -63, serviceUuids: [GATT_V2_SERVICE_UUID], lastSeenAt: 0 }),
  ]),
  unsupported: Object.freeze({ id: "local-device-0003", name: "Other BLE", rssi: -70, serviceUuids: ["0000ffff-0000-1000-8000-00805f9b34fb"], lastSeenAt: 0 }),
  stale: Object.freeze({ id: "local-device-0004", name: "Gone Box", rssi: null, serviceUuids: [GATT_V2_SERVICE_UUID], lastSeenAt: -20000 }),
  errorSamples: Object.freeze(DISCOVERY_ERRORS),
});

module.exports = {
  CANDIDATE_STATES,
  DISCOVERY_ERRORS,
  CANDIDATE_TRANSITIONS,
  CANDIDATE_STALE_AFTER_MS,
  candidateSupportsService,
  isCandidateStale,
  transitionCandidate,
  normalizeCandidate,
  CONFORMANCE_FIXTURES,
};
