'use strict';

const DEFINITIONS = Object.freeze({
  CONFIGURATION_REQUIRED: ['连接配置尚未就绪。', 'none'],
  AUTHENTICATION_REQUIRED: ['请先登录。', 'user_sign_in'],
  AUTHENTICATION_FAILED: ['登录或账号请求未通过，请检查输入后重试。', 'user_sign_in'],
  SAVED_CREDENTIAL_REJECTED: ['已保存的密码不可用，请重新输入当前密码。', 'user_sign_in'],
  VERIFICATION_CODE_INVALID: ['验证码错误或已过期，请重新获取后重试。', 'none'],
  APPLICATION_AUTHORIZATION_DENIED: ['桌面应用或所申请的权限未获账号服务批准，请联系管理员核对应用登记和授权。', 'none'],
  ACCOUNT_SERVICE_UNAVAILABLE: ['账号服务暂时无法完成请求，请稍后重试。', 'user_read'],
  SECURE_STORAGE_UNAVAILABLE: ['系统加密存储不可用，无法安全保存登录凭据。', 'none'],
  BUSINESS_BRIDGE_REQUIRED: ['盒子尚未启用安全资料访问，请完成盒端服务升级和配置。', 'none'],
  SESSION_NOT_READY: ['设备连接尚未就绪。', 'user_reconnect'],
  STALE_GENERATION: ['连接已变化，请使用当前页面。', 'none'],
  INVALID_REQUEST: ['请求参数不符合要求。', 'none'],
  CLAIM_CODE_INVALID: ['认领码无效或已撤销，请检查后重试。', 'none'],
  CLAIM_CODE_EXPIRED: ['认领码已过期，请向管理员获取新的 6 位认领码。', 'none'],
  DEVICE_ALREADY_CLAIMED: ['这台盒子或认领码已被认领，不能再次认领。如需更换归属，请联系管理员处理。', 'none'],
  OPERATION_NOT_ALLOWED: ['当前操作不可用。', 'none'],
  ACCESS_DENIED: ['当前身份无权访问该资源，请核验权限。', 'user_reconnect'],
  SESSION_EXPIRED: ['登录会话已过期，请重新登录。', 'user_sign_in'],
  CONNECTIVITY_SESSION_EXPIRED: ['盒子连接已失效，请重新连接盒子。', 'user_reconnect'],
  TRANSPORT_UNAVAILABLE: ['设备连接暂不可用。', 'user_reconnect'],
  DIRECT_CONNECTION_UNAVAILABLE: ['盒子在线，但当前两端网络未能建立直连。请更换网络或检查路由器后重试。', 'user_reconnect'],
  WRITE_OUTCOME_UNKNOWN: ['连接中断，尚不能确认本次操作是否已在盒端完成。请先检查结果，再决定是否重试。', 'none'],
  REQUEST_TIMEOUT: ['请求超时，可手动检查任务状态。', 'user_read'],
  RESOURCE_EXHAUSTED: ['读取请求过多，请稍后重试。', 'user_read'],
  RATE_LIMITED: ['请求暂时过于频繁，本次请求未执行，请稍后重试。', 'user_read'],
  SESSION_QUOTA_EXHAUSTED: ['当前连接的请求或数据额度已用尽，或不足以完成本次传输。请先检查未完成操作，再断开并重新连接。', 'user_reconnect'],
  CONTRACT_MISMATCH: ['设备返回的数据不符合接口合同。', 'none'],
  RESPONSE_TOO_LARGE: ['设备返回的数据超过允许大小。', 'none'],
  REMOTE_ERROR: ['设备未能完成请求。', 'user_read'],
  READ_CANCELLED: ['已停止本次读取结果的投递。', 'none'],
});

const DIAGNOSTIC_PHASES = new Set(['account_service', 'ticket', 'native']);
const SDK_DIAGNOSTIC_CODES = new Set(['SDK_INVALID_CONFIG', 'SDK_INVALID_SESSION', 'SDK_SESSION_PROVIDER_FAILED',
  'SDK_SIGNALING_FAILED', 'SDK_PROTOCOL_VIOLATION', 'SDK_CONNECT_TIMEOUT', 'SDK_DIRECT_UNAVAILABLE', 'SDK_PATH_POLICY_FAILED',
  'SDK_CONNECTION_CLOSED', 'SDK_REQUEST_INVALID', 'SDK_REQUEST_TOO_LARGE', 'SDK_TOO_MANY_REQUESTS', 'SDK_REQUEST_LIMIT_REACHED',
  'SDK_RESPONSE_TOO_LARGE', 'SDK_RESPONSE_BUFFER_FULL', 'SDK_REQUEST_TIMEOUT', 'REQUEST_TARGET_NOT_ALLOWED', 'SESSION_RESOURCE_EXHAUSTED',
  'IPC_INVALID_MESSAGE', 'IPC_MESSAGE_TOO_LARGE', 'IPC_PROTOCOL_MISMATCH', 'IPC_SIDECAR_CLOSED', 'IPC_SIDECAR_EXITED',
  'IPC_SIDECAR_SPAWN_FAILED', 'IPC_SIDECAR_TIMEOUT']);

class DesktopError extends Error {
  constructor(code, metadata = {}) {
    const safeCode = Object.hasOwn(DEFINITIONS, code) ? code : 'REMOTE_ERROR';
    super(DEFINITIONS[safeCode][0]);
    this.name = 'DesktopError';
    this.code = safeCode;
    // Internal evidence only. Never copied to renderer error objects.
    if (metadata.definitelyNotSent === true) this.definitelyNotSent = true;
    if (DIAGNOSTIC_PHASES.has(metadata.phase)) this.phase = metadata.phase;
    if (SDK_DIAGNOSTIC_CODES.has(metadata.sdkCode)) this.sdkCode = metadata.sdkCode;
    if (this.sdkCode === 'SDK_DIRECT_UNAVAILABLE' && ['DIRECT_TIMEOUT', 'ICE_FAILED'].includes(metadata.detailCode)) this.detailCode = metadata.detailCode;
    if (Number.isInteger(metadata.httpStatus) && metadata.httpStatus >= 100 && metadata.httpStatus <= 599) {
      this.httpStatus = metadata.httpStatus;
    }
    // Only trusted adapters may supply these fields, never raw bodies/messages.
    for (const field of ['remoteCode', 'traceId']) {
      if (typeof metadata[field] === 'string' && /^[A-Za-z0-9_-]{1,128}$/.test(metadata[field])) {
        this[field] = metadata[field];
      }
    }
  }
}

function toPublicError(error) {
  const code = error instanceof DesktopError && Object.hasOwn(DEFINITIONS, error.code)
    ? error.code : 'REMOTE_ERROR';
  const [message, recovery] = DEFINITIONS[code];
  const result = { code, message, recovery };
  if (error instanceof DesktopError) {
    // Revalidate in case a caller modified an Error after construction.
    const safe = new DesktopError(code, error);
    for (const field of ['httpStatus', 'remoteCode', 'traceId', 'phase', 'sdkCode', 'detailCode']) {
      if (safe[field] !== undefined) result[field] = safe[field];
    }
  }
  return result;
}

module.exports = { DesktopError, toPublicError };
