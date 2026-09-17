'use strict';
const { DesktopError } = require('./public-error.cjs');
const catalog = require('../../shared/product-operations.json');
const OPERATIONS = new Map(catalog.operations.map(value => [value.id, Object.freeze(value)]));
const BASE = '/api/mindos/zhijun';
const LIMITS = Object.freeze({ request: 1048576, chunk: 524288, page: 1048576,
  eventPage: 262144, job: 16777216, file: 209715200, wait: 8000, jobs: 12, uploads: 8 });
const ID = /^[a-f0-9]{32}$/;
const REQUEST_ID = /^[A-Za-z0-9_-]{8,100}$/;
const SHA = /^[a-f0-9]{64}$/;
const STATES = ['queued', 'running', 'succeeded', 'failed', 'cancelled', 'interrupted'];
const TERMINAL = ['succeeded', 'failed', 'cancelled', 'interrupted'];
const plain = value => value !== null && typeof value === 'object' && !Array.isArray(value)
  && [Object.prototype, null].includes(Object.getPrototypeOf(value));
function assert(value, code = 'INVALID_REQUEST') { if (!value) throw new DesktopError(code); }
const exact = (value, required, optional = []) => plain(value)
  && required.every(key => Object.hasOwn(value, key))
  && Reflect.ownKeys(value).every(key => [...required, ...optional].includes(key));
const integer = (value, min, max) => Number.isSafeInteger(value) && value >= min && value <= max;
const text = (value, max = 256) => typeof value === 'string' && value.length > 0 && value.length <= max && !/[\u0000-\u001f\u007f-\u009f]/u.test(value);
const filename = value => text(value, 255) && !/[\\/]/u.test(value) && !['.', '..'].includes(value);
const mime = value => typeof value === 'string' && /^[a-z0-9][a-z0-9!#$&^_.+-]{0,63}\/[a-z0-9][a-z0-9!#$&^_.+-]{0,63}$/.test(value);
function jsonBytes(value, max = LIMITS.request) {
  let nodes = 0;
  function visit(item, depth) {
    assert(++nodes < 60000 && depth <= 32);
    if (item === null || typeof item === 'boolean' || typeof item === 'string') return;
    if (typeof item === 'number') { assert(Number.isFinite(item)); return; }
    assert(Array.isArray(item) || plain(item));
    if (Array.isArray(item)) { assert(item.length <= 30000); for (const next of item) visit(next, depth + 1); }
    else for (const key of Reflect.ownKeys(item)) {
      assert(typeof key === 'string' && key.length <= 256 && !['__proto__', 'constructor', 'prototype'].includes(key));
      assert(Object.getOwnPropertyDescriptor(item, key)?.get === undefined);
      visit(item[key], depth + 1);
    }
  }
  visit(value, 0);
  let result; try { result = Buffer.from(JSON.stringify(value)); } catch { assert(false); }
  assert(result.byteLength <= max);
  return result;
}
function operationRequest(value, uploads) {
  assert(exact(value, ['version', 'requestId', 'operationId', 'params', 'query', 'body']));
  assert(value.version === 1 && typeof value.requestId === 'string' && REQUEST_ID.test(value.requestId));
  const op = OPERATIONS.get(value.operationId); assert(op, 'OPERATION_NOT_ALLOWED');
  assert(exact(value.params, op.pathParams) && plain(value.query));
  for (const item of Object.values(value.params)) assert(text(item) && !/[\\/%?#]/u.test(item) && !['.', '..'].includes(item));
  for (const [key, item] of Object.entries(value.query)) {
    assert(op.query.includes(key) && (typeof item === 'boolean' || (typeof item === 'number' && Number.isSafeInteger(item)) || (typeof item === 'string' && item.length <= 4000 && !/[\u0000-\u001f\u007f-\u009f]/u.test(item))));
  }
  if (op.body === 'none') assert(value.body === null);
  else {
    if (op.body === 'json') assert(value.body === null || plain(value.body) || Array.isArray(value.body));
    jsonBytes(value.body, op.maxRequestBytes);
    if (op.body === 'multipart') {
      assert(exact(value.body, ['kind', 'fields', 'files']) && value.body.kind === 'multipart'
        && plain(value.body.fields) && Object.keys(value.body.fields).length <= 30 && Array.isArray(value.body.files) && value.body.files.length >= 1 && value.body.files.length <= 10);
      for (const [field, item] of Object.entries(value.body.fields)) assert(/^[A-Za-z][A-Za-z0-9_]{0,63}$/.test(field) && typeof item === 'string');
      for (const item of value.body.files) {
        assert(exact(item, ['field', 'uploadId']) && item.field === 'file' && ID.test(item.uploadId));
        if (uploads) assert(uploads.get(item.uploadId)?.state === 'complete', 'OPERATION_NOT_ALLOWED');
      }
    }
  }
  // Snapshot user values before any await; paths and headers are never accepted.
  return { operation: op, value: JSON.parse(jsonBytes(value).toString('utf8')) };
}
function request(method, path, body) {
  return { method, path: BASE + path, headers: { Accept: 'application/json', ...(body === undefined ? {} : { 'Content-Type': 'application/json' }) },
    ...(body === undefined ? {} : { body: jsonBytes(body) }) };
}
function validateWireRequest(value) {
  assert(exact(value, ['method', 'relative_path', 'headers'], ['body']));
  assert(plain(value.headers) && Object.entries(value.headers).every(([key, vals]) => ['Accept', 'Content-Type'].includes(key)
    && Array.isArray(vals) && vals.length === 1 && vals[0] === 'application/json') && value.headers.Accept?.[0] === 'application/json');
  const path = value.relative_path;
  assert(typeof path === 'string' && path.startsWith(BASE + '/') && path.length <= 512);
  const tail = path.slice(BASE.length); const id = '[a-f0-9]{32}'; const num = '(?:0|[1-9][0-9]*)';
  let valid = false;
  if (value.method === 'GET') {
    valid = tail === '/context' || new RegExp(`^/uploads/${id}$`).test(tail);
    const poll = tail.match(new RegExp(`^/operations/${id}\\?after=(${num})&waitMs=(${num})$`));
    const blob = tail.match(new RegExp(`^/blobs/${id}\\?offset=(${num})&limit=(${num})$`));
    valid ||= Boolean(poll && integer(Number(poll[1]), 0, 1000000) && integer(Number(poll[2]), 0, LIMITS.wait));
    valid ||= Boolean(blob && integer(Number(blob[1]), 0, LIMITS.file) && integer(Number(blob[2]), 1, LIMITS.chunk));
  } else if (value.method === 'POST') {
    valid = ['/operations', '/uploads'].includes(tail) || new RegExp(`^/(?:operations/${id}/cancel|uploads/${id}/(?:chunks|complete))$`).test(tail);
  } else if (value.method === 'DELETE') valid = new RegExp(`^/uploads/${id}$`).test(tail);
  assert(valid);
  if (value.method === 'POST') {
    assert(value.headers['Content-Type']?.[0] === 'application/json' && value.body instanceof Uint8Array && value.body.byteLength > 0 && value.body.byteLength <= LIMITS.request);
  } else assert(!Object.hasOwn(value, 'body') && !Object.hasOwn(value.headers, 'Content-Type'));
  return value;
}
// Read only a small, fixed-code gateway envelope. Never expose arbitrary server
// text (which may contain paths, credentials or business content) as diagnostics.
const GATEWAY_CAPACITY_CODES = Object.freeze({
  WORKSPACE_OPERATION_CAPACITY: 'BOX_BUSY',
  WORKSPACE_SESSION_CAPACITY: 'BOX_BUSY',
  WORKSPACE_WORKER_CAPACITY: 'BOX_BUSY',
  WORKSPACE_BACKGROUND_CAPACITY: 'BOX_BUSY',
  WORKSPACE_PREVIEW_CAPACITY: 'BOX_BUSY',
  WORKSPACE_RESULT_CAPACITY: 'BOX_BUSY',
  WORKSPACE_OBJECT_LIMIT: 'WORKSPACE_STORAGE_FULL',
  WORKSPACE_QUOTA_EXCEEDED: 'WORKSPACE_STORAGE_FULL',
});
function gatewayCapacityError(response) {
  let remoteCode;
  if (response.body instanceof Uint8Array && response.body.byteLength <= 8192) {
    try {
      const value = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(response.body));
      if (plain(value) && typeof value.code === 'string' && Object.hasOwn(GATEWAY_CAPACITY_CODES, value.code)) remoteCode = value.code;
    } catch { /* Unknown/non-JSON responses keep a generic, credential-free error. */ }
  }
  return new DesktopError(remoteCode ? GATEWAY_CAPACITY_CODES[remoteCode] : 'REMOTE_RATE_LIMITED',
    { httpStatus: 429, ...(remoteCode ? { remoteCode } : {}) });
}
function decodeJson(response, max = LIMITS.page) {
  assert(plain(response) && integer(response.status, 100, 599) && response.body instanceof Uint8Array, 'CONTRACT_MISMATCH');
  assert(response.body.byteLength <= max, 'RESPONSE_TOO_LARGE');
  if (response.status !== 200 && response.status !== 201 && response.status !== 202) {
    if (response.status === 429) throw gatewayCapacityError(response);
    const code = [401, 403].includes(response.status) ? 'ACCESS_DENIED' : 'REMOTE_ERROR';
    throw new DesktopError(code, { httpStatus: response.status });
  }
  assert(plain(response.headers), 'CONTRACT_MISMATCH');
  const types = Object.entries(response.headers).filter(([key]) => key.toLowerCase() === 'content-type');
  assert(types.length === 1 && typeof types[0][1] === 'string' && /^application\/json(?:\s*;\s*charset\s*=\s*utf-8)?\s*$/i.test(types[0][1]), 'CONTRACT_MISMATCH');
  try { return JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(response.body)); }
  catch { throw new DesktopError('CONTRACT_MISMATCH'); }
}
function base64(value, max) {
  assert(typeof value === 'string' && value.length <= Math.ceil(max / 3) * 4 && /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/.test(value), 'CONTRACT_MISMATCH');
  const bytes = Buffer.from(value, 'base64'); assert(bytes.length <= max && bytes.toString('base64') === value, 'CONTRACT_MISMATCH');
  return bytes;
}
function blobDescriptor(value) {
  assert(exact(value, ['id', 'size', 'sha256', 'contentType'], ['fileName']) && ID.test(value.id)
    && integer(value.size, 0, LIMITS.file) && SHA.test(value.sha256) && mime(value.contentType)
    && (value.fileName === undefined || filename(value.fileName)), 'CONTRACT_MISMATCH');
  return { ...value };
}
module.exports = { OPERATIONS, BASE, LIMITS, ID, REQUEST_ID, SHA, STATES, TERMINAL, plain, exact, integer, text, filename, mime, gatewayCapacityError,
  assert, jsonBytes, operationRequest, request, validateWireRequest, decodeJson, base64, blobDescriptor };
