"use strict";
/**
 * 阶段 3 功能开关（Feature Gate，WP M）。
 *
 * 指南要求：Feature Gate 是硬边界——关闭时，UI、深链、Repository 调用和生产埋点
 * 必须同时不可达，不能保留隐藏入口或不安全回退。本地添加入口在任何平台打开前，
 * 必须由阶段 4/5 的真实设备、vectors、自动化测试与真机验收证据把关。
 *
 * 当前骨架阶段全部默认 false；真实接入（BLE GATT v2）交付前不得手工置 true。
 * 本文件只做纯逻辑开关，不 import electron，可在 node 环境直接单测。
 */
const FEATURE_GATES = Object.freeze({
  // 阶段 3：Electron Web Bluetooth 发现（阶段 4 真实验证通过前保持关闭）
  electronWebBluetoothDiscoveryV1: false,
  // 阶段 3：Electron BLE 本地认领（阶段 4 真实验证通过前保持关闭）
  electronBleProvisioningV2: false,
  // 阶段 3：本地添加流程骨架（Mock 状态机可用性；默认关闭，生产不展示入口）
  localDeviceAddSkeleton: false,
});

// 运行时覆写表：仅允许开发/联调显式开启本地添加骨架（生产 app.isPackaged 时不可用）。
const runtimeOverrides = new Map();

/** 读取开关；未知开关一律视为关闭（fail-closed）。 */
function isEnabled(name) {
  if (runtimeOverrides.has(name)) return runtimeOverrides.get(name) === true;
  return FEATURE_GATES[name] === true;
}

/**
 * 运行时覆写（仅开发/联调验收用）。生产包中必须保持默认关闭：
 * 调用方负责先校验 !app.isPackaged，禁止经 UI/查询参数/请求头开启。
 */
function setOverride(name, value) {
  runtimeOverrides.set(name, value === true);
}

/** 列出当前开启的开关；供装配与埋点使用，未开启时返回空数组。 */
function enabledGates() {
  const names = new Set([...Object.keys(FEATURE_GATES), ...runtimeOverrides.keys()]);
  return [...names].filter((name) => isEnabled(name));
}

module.exports = { FEATURE_GATES, isEnabled, setOverride, enabledGates };
