"use strict";
/**
 * BLE GATT v2 合同常量（WP N）。
 *
 * 阶段 4 前 UUID 与分片参数以本地协议 Core（local-provisioning-contracts，WP L）
 * 为准；此处为骨架占位，任何破坏性变更必须先提升合同版本再改实现。
 * 本文件只做纯常量，不 import electron，可在 node 环境直接单测。
 */
const GATT_V2_SERVICE_UUID = "5d5e1a02-0002-4000-8000-000000000000";

const GATT_V2_CHARACTERISTICS = Object.freeze({
  deviceInfo: "5d5e1a02-1001-4000-8000-000000000000",
  command: "5d5e1a02-1002-4000-8000-000000000000",
  status: "5d5e1a02-1003-4000-8000-000000000000",
  attestation: "5d5e1a02-1004-4000-8000-000000000000",
  keyConfirm: "5d5e1a02-1005-4000-8000-000000000000",
});

// Command 分片上限（按典型 BLE 20 字节 MTU 载荷留余量；合同变更走版本化）。
const COMMAND_MAX_FRAME_PAYLOAD = 16;
const COMMAND_MAX_TOTAL_FRAMES = 64;

// Status notify 状态（骨架阶段与阶段 4 向量化前仅作枚举；不得在 UI 按文本分支）。
const BLE_EVENTS = Object.freeze({
  connected: "connected",
  disconnected: "disconnected",
  status: "status",
  error: "error",
});

/** 候选过滤（chooser 只展示服务 UUID 匹配的候选；acceptAllDevices 一律禁止）。 */
function isCandidateSupported(device, serviceUuid = GATT_V2_SERVICE_UUID) {
  if (!device || typeof device !== "object") return false;
  if (!device.isSupported) return false;
  const uuids = Array.isArray(device.uuids) ? device.uuids : [];
  return uuids.some((uuid) => String(uuid).toLowerCase() === String(serviceUuid).toLowerCase());
}

module.exports = {
  GATT_V2_SERVICE_UUID,
  GATT_V2_CHARACTERISTICS,
  COMMAND_MAX_FRAME_PAYLOAD,
  COMMAND_MAX_TOTAL_FRAMES,
  BLE_EVENTS,
  isCandidateSupported,
};
