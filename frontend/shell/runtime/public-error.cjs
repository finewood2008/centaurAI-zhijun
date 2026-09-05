'use strict';

const DEFINITIONS = Object.freeze({
  CONFIGURATION_REQUIRED: ['连接配置尚未就绪。', 'none'],
  AUTHENTICATION_REQUIRED: ['请先登录。', 'user_sign_in'],
  AUTHENTICATION_FAILED: ['登录或账号请求未通过，请检查输入后重试。', 'user_sign_in'],
  SECURE_STORAGE_UNAVAILABLE: ['系统加密存储不可用，无法安全保存登录凭据。', 'none'],
  BUSINESS_BRIDGE_REQUIRED: ['账号已登录，盒子的资料访问授权通道尚未配置完成。', 'none'],
  SESSION_NOT_READY: ['设备连接尚未就绪。', 'user_reconnect'],
  STALE_GENERATION: ['连接已变化，请使用当前页面。', 'none'],
  INVALID_REQUEST: ['请求参数不符合要求。', 'none'],
  OPERATION_NOT_ALLOWED: ['当前操作不可用。', 'none'],
  ACCESS_DENIED: ['当前身份无权访问该资源，请核验权限。', 'user_reconnect'],
  SESSION_EXPIRED: ['业务会话已失效，请重新连接。', 'user_reconnect'],
  TRANSPORT_UNAVAILABLE: ['设备连接暂不可用。', 'user_reconnect'],
  REQUEST_TIMEOUT: ['读取超时，可手动重试。', 'user_read'],
  RESOURCE_EXHAUSTED: ['读取请求过多，请稍后重试。', 'user_read'],
  CONTRACT_MISMATCH: ['设备返回的数据不符合接口合同。', 'none'],
  RESPONSE_TOO_LARGE: ['设备返回的数据超过允许大小。', 'none'],
  REMOTE_ERROR: ['设备未能完成请求。', 'user_read'],
  READ_CANCELLED: ['已停止本次读取结果的投递。', 'none'],
});

class DesktopError extends Error {
  constructor(code, metadata = {}) {
    const safeCode = Object.hasOwn(DEFINITIONS, code) ? code : 'REMOTE_ERROR';
    super(DEFINITIONS[safeCode][0]);
    this.name = 'DesktopError';
    this.code = safeCode;
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
    for (const field of ['httpStatus', 'remoteCode', 'traceId']) {
      if (safe[field] !== undefined) result[field] = safe[field];
    }
  }
  return result;
}

module.exports = { DesktopError, toPublicError };
