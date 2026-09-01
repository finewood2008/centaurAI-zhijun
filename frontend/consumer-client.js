"use strict";
/**
 * 阶段 2 Consumer Client（Electron 宿主侧）。
 *
 * 账号登录、设备认领、连接票据创建归属 App/Electron，不再由 MindOS 前端承担
 * （MindOS 不承担账号/Owner/认领控制面）。本模块把原来放在 mindos-web 里的
 * Mock Consumer 登录/认领/票据创建逻辑迁到宿主主进程，仅产出短期一次性连通
 * 票据，经 IPC 受控通道投放给渲染端由 MindOS 后端交换为会话。
 *
 * 与 MindOS 前端 api.ts 的受控桥契约对齐：window.__MINDOS_ACCESS__.getTicket()。
 * 票据 nonce 单次使用，取走即从内存清除，重放即被后端拒绝。
 *
 * 生产/联调分离：本文件只承载「生产连接路径」，不含任何 Mock 凭证、固定验证码或
 * 本机 Mock 默认地址。Mock OTP 联调逻辑隔离在 consumer-mock-otp.js，仅当显式
 * 开启联调开关时按需加载；发布守卫（scripts/check_release_guard.py）复核生产
 * 构建，保证 Mock 模块及其内容标记绝不进入 Electron 制品。
 *
 * 运行配置（环境变量）：
 *   NEXUS_CONSUMER_BASE  Consumer 服务基址（生产必填）
 *   NEXUS_DEVICE_PHONE   Owner 手机号（联调用）
 */
const CONSUMER_BASE = (process.env.NEXUS_CONSUMER_BASE || "").replace(/\/+$/, "");
const CONSUMER_PREFIX = CONSUMER_BASE ? `${CONSUMER_BASE}/api/consumer/v1` : "";
const OWNER_PHONE = process.env.NEXUS_DEVICE_PHONE || "";

let cachedTicket = null;
let mockConsumer = undefined;

/** 按需加载隔离的 Mock OTP 联调模块；未开启联调开关或模块缺失时返回 null。 */
function loadMockConsumer() {
  if (mockConsumer !== undefined) return mockConsumer;
  mockConsumer = null;
  if (process.env.NEXUS_MOCK_OTP !== "1") return mockConsumer;
  try {
    // eslint-disable-next-line global-require
    mockConsumer = require("./consumer-mock-otp");
  } catch {
    mockConsumer = null;
  }
  return mockConsumer;
}

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

async function provision({ deviceId }) {
  if (!deviceId) throw new Error("缺少设备标识（MINDOS_DEVICE_ID）");
  // 联调开关：仅在显式开启时走隔离的 Mock OTP 模块；生产关闭时走真实消费侧。
  const mock = loadMockConsumer();
  if (mock && typeof mock.provisionMock === "function") {
    return mock.provisionMock({ deviceId });
  }
  if (!CONSUMER_BASE) {
    throw new Error("正式连接需配置 NEXUS_CONSUMER_BASE 并经 App 授权完成设备配对");
  }
  // 真实消费侧：由外部 Consumer 客户端注入一次性连接授权后创建票据。
  // 真实 Consumer OpenAPI 未交付前 fail-closed，绝不回退到任何 Mock 凭证。
  throw new Error(`正式消费侧尚未交付连接协议（${CONSUMER_PREFIX}），等待 App 授权通道`);
}

class ConsumerClient {
  /** 渲染端经 IPC 取当前可用票据；一次性消费，取走即清除。 */
  async getConnectivityTicket() {
    if (cachedTicket) {
      const ticket = cachedTicket;
      cachedTicket = null;
      return ticket;
    }
    return null;
  }

  /** 宿主侧执行登录→认领→票据创建，缓存票据供渲染端一次性消费。 */
  async provision({ deviceId }) {
    const ticket = await provision({ deviceId });
    cachedTicket = ticket;
    return ticket;
  }
}

module.exports = { ConsumerClient, consumerBase: CONSUMER_BASE };