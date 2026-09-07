'use strict';

const { TextDecoder } = require('node:util');
const { DesktopError } = require('./public-error.cjs');
const TYPES = new Set(['document', 'image', 'audio']);
const STATUSES = new Set(['uploaded', 'queued', 'processing', 'available', 'failed']);
const QUERY_KEYS = new Set(['limit', 'offset', 'keyword', 'type', 'status']);
const MAX_RESPONSE_BYTES = 256 * 1024;
const CONTROLS = /[\u0000-\u001f\u007f-\u009f]/u;
const isRecord = (value) => value !== null && typeof value === 'object'
  && (Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
const fail = (code = 'CONTRACT_MISMATCH') => { throw new DesktopError(code); };

function normalizeMaterialsQuery(query) {
  if (!isRecord(query) || Reflect.ownKeys(query).some((key) => !QUERY_KEYS.has(key))) fail('INVALID_REQUEST');
  if (!Number.isInteger(query.limit) || query.limit < 1 || query.limit > 50
      || !Number.isInteger(query.offset) || query.offset < 0 || query.offset > 10000) fail('INVALID_REQUEST');
  const result = { limit: query.limit, offset: query.offset };
  if (Object.hasOwn(query, 'keyword')) {
    if (typeof query.keyword !== 'string' || CONTROLS.test(query.keyword)) fail('INVALID_REQUEST');
    const keyword = query.keyword.trim();
    if (!keyword.length || keyword.length > 100) fail('INVALID_REQUEST');
    result.keyword = keyword;
  }
  for (const [key, choices] of [['type', TYPES], ['status', STATUSES]]) {
    if (Object.hasOwn(query, key)) {
      if (!choices.has(query[key])) fail('INVALID_REQUEST');
      result[key] = query[key];
    }
  }
  return result;
}

function buildMaterialsRequest(query) {
  const normalized = normalizeMaterialsQuery(query);
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(normalized)) search.set(key, String(value));
  return { method: 'GET', path: `/api/mindos/materials?${search}`, headers: { Accept: 'application/json' } };
}

function safeString(value, max) {
  if (typeof value !== 'string' || !value.trim() || value.length > max || CONTROLS.test(value)) fail();
  return value;
}

function safeTimestamp(value) {
  safeString(value, 64);
  // Python isoformat (including microseconds) and RFC3339 UTC/offset timestamps.
  const parts = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,9})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!parts || !Number.isFinite(Date.parse(value))) fail();
  const [, year, month, day, hour, minute, second, zone] = parts;
  const days = new Date(Date.UTC(Number(year), Number(month), 0)).getUTCDate();
  if (+month < 1 || +month > 12 || +day < 1 || +day > days || +hour > 23 || +minute > 59 || +second > 59) fail();
  if (zone !== 'Z' && (+zone.slice(1, 3) > 23 || +zone.slice(4) > 59)) fail();
  return value;
}

function projectMaterialsResponse(response, query) {
  const normalized = normalizeMaterialsQuery(query);
  if (!isRecord(response) || !(response.body instanceof Uint8Array)
      || !Number.isInteger(response.status) || response.status < 100 || response.status > 599) fail();
  if (response.body.byteLength > MAX_RESPONSE_BYTES) fail('RESPONSE_TOO_LARGE');
  if (response.status !== 200) {
    // A 401 alone does not prove expiry/revocation; never interpret remote error text.
    const code = response.status === 401 || response.status === 403 ? 'ACCESS_DENIED'
      : response.status === 429 ? 'RESOURCE_EXHAUSTED' : 'REMOTE_ERROR';
    throw new DesktopError(code, { httpStatus: response.status });
  }
  if (!isRecord(response.headers)) fail();
  const contentTypes = Object.entries(response.headers).filter(([key]) => key.toLowerCase() === 'content-type');
  if (contentTypes.length !== 1 || typeof contentTypes[0][1] !== 'string'
      || !/^application\/json(?:\s*;\s*charset\s*=\s*utf-8)?\s*$/i.test(contentTypes[0][1])) fail();
  let payload;
  try {
    payload = JSON.parse(new TextDecoder('utf-8', { fatal: true }).decode(response.body));
  } catch { fail(); }
  if (!isRecord(payload) || !Array.isArray(payload.items) || payload.items.length > normalized.limit
      || !Number.isSafeInteger(payload.total) || payload.total < 0
      || payload.limit !== normalized.limit || payload.offset !== normalized.offset
      || payload.total < payload.items.length
      || (payload.items.length > 0 && normalized.offset + payload.items.length > payload.total)) fail();
  const hasMore = normalized.offset + payload.items.length < payload.total;
  if (typeof payload.hasMore !== 'boolean' || payload.hasMore !== hasMore) fail();
  const seenIds = new Set();
  const items = payload.items.map((item) => {
    if (!isRecord(item) || !TYPES.has(item.fileType) || !STATUSES.has(item.status)) fail();
    const materialId = safeString(item.materialId, 256);
    if (seenIds.has(materialId)) fail();
    seenIds.add(materialId);
    return {
      materialId,
      fileName: safeString(item.fileName, 512),
      fileType: item.fileType,
      status: item.status,
      createdAt: safeTimestamp(item.createdAt),
    };
  });
  return { items, total: payload.total, limit: payload.limit, offset: payload.offset, hasMore };
}

module.exports = { normalizeMaterialsQuery, buildMaterialsRequest, projectMaterialsResponse, MAX_RESPONSE_BYTES };
