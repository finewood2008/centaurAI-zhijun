'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const { normalizeMaterialsQuery, buildMaterialsRequest, projectMaterialsResponse, MAX_RESPONSE_BYTES } = require('../runtime/materials.cjs');
const { DesktopError, toPublicError } = require('../runtime/public-error.cjs');
const query = { limit: 20, offset: 0 };
const item = { materialId: 'material-1', fileName: '本地合同.txt', fileType: 'document', status: 'queued', createdAt: '2026-09-06T01:02:03.123456+00:00' };
const page = (changes = {}) => ({ items: [{ ...item }], total: 1, limit: 20, offset: 0, hasMore: false, ...changes });
const response = (payload) => ({ status: 200, headers: { 'content-type': 'application/json; charset=utf-8' }, body: Buffer.from(JSON.stringify(payload)) });
const hasCode = (code) => (error) => error instanceof DesktopError && error.code === code;

test('query normalization, queued filtering and fixed request shape', () => {
  const normalized = normalizeMaterialsQuery({ ...query, keyword: '  资料 & #  ', type: 'audio', status: 'queued' });
  assert.deepEqual(normalized, { ...query, keyword: '资料 & #', type: 'audio', status: 'queued' });
  const request = buildMaterialsRequest(normalized);
  assert.deepEqual(Object.keys(request), ['method', 'path', 'headers']);
  assert.equal(request.method, 'GET');
  const url = new URL(request.path, 'https://synthetic.invalid');
  assert.equal(url.pathname, '/api/mindos/materials');
  assert.equal(url.searchParams.get('keyword'), '资料 & #');
  assert.equal(url.searchParams.get('status'), 'queued');
  assert.deepEqual(request.headers, { Accept: 'application/json' });
});

test('request rejects unknown keys, implicit defaults, invalid integers and enums', () => {
  for (const bad of [null, [], {}, { ...query, folderId: 1 }, { ...query, headers: {} },
    { ...query, limit: '20' }, { ...query, limit: 0 }, { ...query, limit: 51 }, { ...query, limit: 1.5 },
    { ...query, offset: -1 }, { ...query, offset: 10001 }, { ...query, offset: Infinity },
    { ...query, keyword: '  ' }, { ...query, keyword: 'a'.repeat(101) }, { ...query, keyword: 'a\nb' },
    { ...query, keyword: undefined }, { ...query, type: 'video' }, { ...query, status: 'unknown' },
    { ...query, [Symbol('hidden')]: true }, Object.assign(Object.create({ limit: 20 }), { offset: 0 })]) {
    assert.throws(() => normalizeMaterialsQuery(bad), hasCode('INVALID_REQUEST'));
  }
  assert.deepEqual(normalizeMaterialsQuery({ limit: 50, offset: 10000 }), { limit: 50, offset: 10000 });
});

test('projection removes global folders, per-item paths and all unapproved fields', () => {
  const payload = page({ folders: [{ name: 'secret-folder' }], diagnostic: 'secret', items: [{ ...item,
    folder: 'secret-folder', folderId: 42, body: 'private', previewUrl: 'file:///private', hostPath: '/private' }] });
  assert.deepEqual(projectMaterialsResponse(response(payload), query), page());
  assert.deepEqual(projectMaterialsResponse(response(page({ items: [], total: 0 })), query), page({ items: [], total: 0 }));
  assert.equal(projectMaterialsResponse(response(page({ total: 21, hasMore: true })), query).hasMore, true);
});

test('all supported status/type combinations project and unknown values fail closed', () => {
  for (const status of ['uploaded', 'queued', 'processing', 'available', 'failed']) {
    for (const fileType of ['document', 'image', 'audio']) {
      assert.equal(projectMaterialsResponse(response(page({ items: [{ ...item, status, fileType }] })), query).items[0].status, status);
    }
  }
  for (const change of [{ status: 'new_future_status' }, { fileType: 'video' }, { fileName: '' },
    { fileName: 'a'.repeat(513) }, { materialId: 'a'.repeat(257) }, { materialId: '\u0000' },
    { createdAt: '2026-02-30T00:00:00Z' }, { createdAt: '2026-09-06' }, { createdAt: '2026-09-06T24:00:00Z' },
    { createdAt: '2026-09-06T00:00:00+01:60' }, { createdAt: 'secret' }]) {
    assert.throws(() => projectMaterialsResponse(response(page({ items: [{ ...item, ...change }] })), query), hasCode('CONTRACT_MISMATCH'));
  }
});

test('pagination validates echo, counters, duplicate ids and hasMore', () => {
  for (const changes of [{ items: null }, { total: -1 }, { total: 1.1 }, { total: Number.MAX_SAFE_INTEGER + 1 },
    { total: 0 }, { limit: 19 }, { offset: 1 }, { hasMore: true }, { hasMore: undefined },
    { items: [item, item], total: 2 }, { items: Array.from({ length: 21 }, (_, i) => ({ ...item, materialId: String(i) })), total: 21 }]) {
    assert.throws(() => projectMaterialsResponse(response(page(changes)), query), hasCode('CONTRACT_MISMATCH'));
  }
  assert.throws(() => projectMaterialsResponse(response(page({ offset: 2, total: 2 })), { ...query, offset: 2 }), hasCode('CONTRACT_MISMATCH'));
});

test('body bytes capped before decoding, UTF-8 fatal and JSON/content type validated', () => {
  const base = response(page());
  assert.throws(() => projectMaterialsResponse({ ...base, body: new Uint8Array(MAX_RESPONSE_BYTES + 1) }, query), hasCode('RESPONSE_TOO_LARGE'));
  const encoded = Buffer.from(JSON.stringify(page()));
  const exact = Buffer.concat([encoded, Buffer.alloc(MAX_RESPONSE_BYTES - encoded.byteLength, ' ')]);
  assert.equal(projectMaterialsResponse({ ...base, body: exact }, query).total, 1);
  for (const changes of [{ body: Buffer.from([0xff]) }, { body: Buffer.from('{secret') },
    { body: 'not bytes' }, { headers: {} }, { headers: { 'content-type': 'text/html' } },
    { headers: { 'content-type': 'application/json', 'Content-Type': 'application/json' } }]) {
    assert.throws(() => projectMaterialsResponse({ ...base, ...changes }, query), hasCode('CONTRACT_MISMATCH'));
  }
});

test('HTTP errors and native errors never expose response, exception or tokens', () => {
  for (const [status, code] of [[401, 'ACCESS_DENIED'], [403, 'ACCESS_DENIED'], [429, 'RESOURCE_EXHAUSTED'], [500, 'REMOTE_ERROR']]) {
    let caught;
    try { projectMaterialsResponse({ status, headers: {}, body: Buffer.from('super-secret-token') }, query); } catch (error) { caught = error; }
    assert.equal(caught.code, code);
    const safe = toPublicError(caught);
    assert.equal(safe.httpStatus, status);
    assert.ok(!JSON.stringify(safe).includes('super-secret'));
    assert.ok(!Object.hasOwn(safe, 'stack'));
  }
  assert.equal(toPublicError(new Error('super-secret-token')).code, 'REMOTE_ERROR');
  assert.equal(toPublicError({ code: 'SESSION_EXPIRED', message: 'super-secret-token' }).code, 'REMOTE_ERROR');
  assert.ok(!JSON.stringify(toPublicError(new DesktopError('UNKNOWN', { traceId: 'secret\nheader', httpStatus: 999 }))).includes('secret'));
});
