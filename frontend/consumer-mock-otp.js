"use strict";
/**
 * Mock Consumer Client —— 仅联调使用（Mock OTP）。
 *
 * 构建期分离：本文件专放 Mock 联调逻辑，仅在 consumer-client.js 显式开启联调开关
 * （NEXUS_MOCK_OTP=1）时按需加载，绝不进入生产 Electron 制品。
 * 发布守卫（scripts/check_release_guard.py）复核生产构建时，要求制品内不得出现
 * 本文件内容标记（devOnlyCode / 本机 Mock 地址 / 固定验证码），确保生产不携带并执行 Mock OTP。
 *
 * 联调配置（环境变量）：
 *   NEXUS_CONSUMER_BASE    本机 Mock Consumer 服务基址，默认 http://127.0.0.1:8801
 *   NEXUS_DEVICE_PHONE     Owner 手机号（Mock 联调用）
 */
const MOCK_CONSUMER_BASE = process.env.NEXUS_CONSUMER_BASE || "http://127.0.0.1:8801";
const MOCK_CONSUMER_PREFIX = `${MOCK_CONSUMER_BASE}/api/consumer/v1`;
const OWNER_PHONE = process.env.NEXUS_DEVICE_PHONE || "";

async function jsonFetch(url, init = {}) {
  const res = await fetch(url, {
    ...init,
    headers: { "Content-Type": "application/json", ...(init.headers || {}) },
  });
  let data = {};
  try {
    data = await res.json();
  } catch {
    /* 非 JSON 响应 */
  }
  if (!res.ok) {
    const message =
      (data && data.error && data.error.message) || `Consumer 请求失败（${res.status}）`;
    const err = new Error(message);
    err.status = res.status;
    err.code = data && data.error && data.error.code ? data.error.code : undefined;
    throw err;
  }
  return data;
}

/** Mock 联调：登录（固定验证码）→ 认领设备 → 创建连接票据，返回一次性票据 nonce。 */
async function provisionMock({ deviceId }) {
  if (!OWNER_PHONE) {
    throw new Error("Mock OTP 联调需要配置 NEXUS_DEVICE_PHONE");
  }
  if (!deviceId) throw new Error("缺少设备标识（MINDOS_DEVICE_ID）");
  const codeResult = await jsonFetch(`${MOCK_CONSUMER_PREFIX}/auth/phone-code`, {
    method: "POST",
    body: JSON.stringify({ phone: OWNER_PHONE }),
  });
  const login = await jsonFetch(`${MOCK_CONSUMER_PREFIX}/auth/login`, {
    method: "POST",
    body: JSON.stringify({
      phone: OWNER_PHONE,
      code: codeResult && codeResult.devOnlyCode ? codeResult.devOnlyCode : "123456",
      clientName: "Desktop",
    }),
  });
  await jsonFetch(`${MOCK_CONSUMER_PREFIX}/devices/${encodeURIComponent(deviceId)}/claim`, {
    method: "POST",
    headers: { Authorization: `Bearer ${login.accessToken}` },
    body: JSON.stringify({ idempotencyKey: `claim-${Date.now()}` }),
  });
  const session = await jsonFetch(`${MOCK_CONSUMER_PREFIX}/connectivity/sessions`, {
    method: "POST",
    headers: { Authorization: `Bearer ${login.accessToken}` },
    body: JSON.stringify({ deviceId, idempotencyKey: `sess-${Date.now()}` }),
  });
  return session.ticket;
}

module.exports = { provisionMock };