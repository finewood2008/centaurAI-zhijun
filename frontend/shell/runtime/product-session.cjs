'use strict';
const crypto = require('node:crypto');
const { DesktopError } = require('./public-error.cjs');
const P = require('./product-policy.cjs');
const { assert, exact, integer, ID, REQUEST_ID, SHA, LIMITS, STATES, TERMINAL } = P;
const MEDIA = new Set(['application/pdf', 'image/png', 'image/jpeg', 'image/webp', 'image/gif', 'image/avif',
  'audio/mpeg', 'audio/mp3', 'audio/wav', 'audio/x-wav', 'audio/ogg', 'audio/flac', 'audio/mp4', 'audio/aac', 'audio/webm', 'video/mp4', 'video/webm', 'video/ogg']);
const productMethods = ['start', 'poll', 'cancel', 'uploadCreate', 'uploadChunk', 'uploadComplete', 'uploadStatus', 'uploadCancel', 'blobRead', 'save', 'openMedia', 'closeMedia', 'requestMicrophone'];
const TERMINAL_CONNECTION_ERRORS = ['SESSION_EXPIRED', 'AUTHENTICATION_REQUIRED', 'CONNECTIVITY_SESSION_EXPIRED',
  'TRANSPORT_UNAVAILABLE', 'SESSION_NOT_READY', 'SESSION_QUOTA_EXHAUSTED'];
const UNCERTAIN_WRITE_ERRORS = [...TERMINAL_CONNECTION_ERRORS, 'REQUEST_TIMEOUT'];
function createProductSession({ session, isCurrent, host = {}, timeoutMs = 12000, onTerminal = () => {}, budget = { active: new Set() } }) {
  let closed = false;
  const jobs = new Map(), uploads = new Map(), blobs = new Map(), media = new Map();
  const active = budget.active;
  const streams = new Map();
  let pendingStarts = 0, pendingUploads = 0, pendingMedia = 0;
  const guards = new Set();
  const requestControllers = new Set();
  let terminalReported = false;
  const microphoneOwner = () => !closed && isCurrent();
  let mediaStreams = 0, saves = 0;
  function current() { assert(!closed && isCurrent(), 'STALE_GENERATION'); }
  function reportTerminal(error) {
    const terminal = error?.sessionTerminal || (TERMINAL_CONNECTION_ERRORS.includes(error?.code) ? error : undefined);
    if (terminal && !terminalReported) { terminalReported = true; onTerminal(terminal); }
  }
  function trimJobs() {
    // Pending starts reserve a place before the remote acknowledgement arrives.
    // Retire terminal history against the same total used by admission, so a
    // page's concurrent reads are not blocked by completed jobs kept for lookup.
    for (const [id, job] of jobs) if (TERMINAL.includes(job.state) && jobs.size + pendingStarts >= LIMITS.jobs) jobs.delete(id);
  }
  async function wire(request, mutation = false, reservation) {
    const managed = session.managesRequestQueue === true;
    const control = request.method === 'DELETE' || request.path.endsWith('/cancel');
    current(); assert(active.size < (managed && control ? 9 : 8), 'RESOURCE_EXHAUSTED');
    const controller = new AbortController(); requestControllers.add(controller);
    const task = Promise.resolve().then(() => { current(); return session.request(request, { signal: controller.signal, reservation,
      onWork(work) { if (managed) { active.add(work); active.delete(task); work.then(() => active.delete(work), () => active.delete(work)); } },
    }); });
    active.add(task); task.then(() => active.delete(task), () => active.delete(task));
    let timer, rejectGuard;
    try {
      const response = await Promise.race([task, new Promise((_, reject) => {
        rejectGuard = reject; guards.add(reject);
        // The bridge owns bounded queue time and starts the native deadline only
        // on dispatch. Unmanaged fixtures/v1 retain the original local deadline.
        if (!managed) timer = setTimeout(() => reject(new DesktopError('REQUEST_TIMEOUT')), timeoutMs);
      })]);
      current(); return P.decodeJson(response);
    } catch (error) {
      if (mutation && error instanceof DesktopError && !error.definitelyNotSent
          && UNCERTAIN_WRITE_ERRORS.includes(error.code)) {
        const unknown = new DesktopError('WRITE_OUTCOME_UNKNOWN');
        if (TERMINAL_CONNECTION_ERRORS.includes(error.code)) unknown.sessionTerminal = error;
        throw unknown;
      }
      throw error;
    } finally { clearTimeout(timer); guards.delete(rejectGuard); requestControllers.delete(controller); }
  }
  function validateStart(value) {
    assert(exact(value, ['id', 'state', 'cursor']) && ID.test(value.id) && STATES.includes(value.state)
      && integer(value.cursor, 0, 1000000), 'CONTRACT_MISMATCH'); return { ...value };
  }
  function validateUpload(value, expectedId) {
    assert(exact(value, ['id', 'state', 'size', 'received', 'nextIndex'], ['sha256']) && ID.test(value.id)
      && (!expectedId || value.id === expectedId) && ['open', 'complete', 'cancelled', 'failed'].includes(value.state)
      && integer(value.size, 0, LIMITS.file) && integer(value.received, 0, value.size)
      && integer(value.nextIndex, 0, Math.ceil(LIMITS.file / LIMITS.chunk))
      && (value.sha256 === undefined || SHA.test(value.sha256)), 'CONTRACT_MISMATCH');
    if (value.state === 'complete') assert(value.received === value.size && SHA.test(value.sha256), 'CONTRACT_MISMATCH');
    return { ...value };
  }
  function rememberBlob(value) {
    const blob = P.blobDescriptor(value);
    const previous = blobs.get(blob.id);
    assert(!previous || JSON.stringify(previous) === JSON.stringify(blob), 'CONTRACT_MISMATCH');
    assert(previous || blobs.size < 64, 'RESOURCE_EXHAUSTED');
    blobs.set(blob.id, blob); return { ...blob };
  }
  function jobInput(value, required) {
    assert(exact(value, required) && typeof value.id === 'string' && ID.test(value.id));
    assert(jobs.has(value.id), 'OPERATION_NOT_ALLOWED'); return jobs.get(value.id);
  }
  async function start(input) {
    const normalized = P.operationRequest(input, uploads);
    const reservation = normalized.operation.body === 'multipart'
      ? uploads.get(normalized.value.body.files[0]?.uploadId)?.reservation : undefined;
    trimJobs(); assert(jobs.size + pendingStarts < LIMITS.jobs, 'RESOURCE_EXHAUSTED');
    const digest = crypto.createHash('sha256').update(P.jsonBytes(normalized.value)).digest('hex');
    const prior = [...jobs.values()].find(job => job.requestId === input.requestId);
    assert(!prior || prior.digest === digest);
    pendingStarts++;
    let result;
    try { result = validateStart(await wire(P.request('POST', '/operations', normalized.value), normalized.operation.mutating, reservation)); }
    finally { pendingStarts--; }
    const existing = jobs.get(result.id);
    assert(!existing || existing.digest === digest, 'CONTRACT_MISMATCH');
    if (!existing) jobs.set(result.id, { ...result, operation: normalized.operation, digest, reservation,
      requestId: input.requestId, bytes: 0, lastSeq: 0, headers: false, ended: false, polling: false });
    return result;
  }
  async function poll(input) {
    const job = jobInput(input, ['id', 'after', 'waitMs']);
    assert(integer(input.after, 0, 1000000) && integer(input.waitMs, 0, LIMITS.wait));
    assert(!job.polling, 'OPERATION_NOT_ALLOWED'); job.polling = true;
    try {
      const value = await wire(P.request('GET', `/operations/${input.id}?after=${input.after}&waitMs=${input.waitMs}`), false, job.reservation);
      assert(exact(value, ['id', 'state', 'cursor', 'events', 'hasMore']) && value.id === input.id
        && STATES.includes(value.state) && integer(value.cursor, input.after, 1000000)
        && typeof value.hasMore === 'boolean' && Array.isArray(value.events) && value.events.length <= 32, 'CONTRACT_MISMATCH');
      let last = input.after, pageBytes = 0;
      const events = value.events.map(event => {
        assert(P.plain(event) && integer(event.seq, last + 1, 1000000), 'CONTRACT_MISMATCH'); last = event.seq;
        const fresh = event.seq > job.lastSeq;
        let result;
        if (event.kind === 'headers') {
          assert(exact(event, ['seq', 'kind', 'status', 'headers']) && integer(event.status, 100, 599)
            && P.plain(event.headers) && Object.keys(event.headers).length <= 16, 'CONTRACT_MISMATCH');
          const headers = {};
          for (const [key, item] of Object.entries(event.headers)) {
            assert(['content-type', 'content-disposition', 'content-length', 'cache-control', 'etag', 'last-modified', 'retry-after', 'x-request-id'].includes(key)
              && P.text(item, 2048), 'CONTRACT_MISMATCH'); headers[key] = item;
          }
          if (fresh) { assert(!job.headers && !job.ended, 'CONTRACT_MISMATCH'); job.headers = true; }
          result = { seq: event.seq, kind: 'headers', status: event.status, headers };
        } else if (event.kind === 'chunk') {
          assert(exact(event, ['seq', 'kind', 'data']), 'CONTRACT_MISMATCH');
          const bytes = P.base64(event.data, LIMITS.eventPage); pageBytes += bytes.length;
          if (fresh) { assert(job.headers && !job.ended, 'CONTRACT_MISMATCH'); job.bytes += bytes.length; }
          result = { seq: event.seq, kind: 'chunk', data: new Uint8Array(bytes) };
        } else if (event.kind === 'blob') {
          assert(exact(event, ['seq', 'kind', 'blob']), 'CONTRACT_MISMATCH');
          const blob = P.blobDescriptor(event.blob);
          if (fresh) { assert(job.headers && !job.ended, 'CONTRACT_MISMATCH'); job.bytes += blob.size; }
          result = { seq: event.seq, kind: 'blob', blob: rememberBlob(blob) };
        } else if (event.kind === 'end') {
          assert(exact(event, ['seq', 'kind']), 'CONTRACT_MISMATCH');
          if (fresh) { assert(job.headers && !job.ended, 'CONTRACT_MISMATCH'); job.ended = true; }
          result = { seq: event.seq, kind: 'end' };
        } else {
          assert(exact(event, ['seq', 'kind', 'code', 'message']) && event.kind === 'error'
            && typeof event.code === 'string' && /^[A-Za-z][A-Za-z0-9_]{0,99}$/.test(event.code) && P.text(event.message, 512), 'CONTRACT_MISMATCH');
          if (fresh) job.ended = true;
          // Error envelopes are infrastructure failures, not domain HTTP bodies.
          result = { seq: event.seq, kind: 'error', code: event.code, message: '盒端任务未能完成。' };
        }
        assert(pageBytes <= LIMITS.eventPage && job.bytes <= job.operation.maxResponseBytes, 'RESPONSE_TOO_LARGE');
        if (fresh) job.lastSeq = event.seq; return result;
      });
      assert(value.cursor === last && (!value.hasMore || events.length > 0), 'CONTRACT_MISMATCH');
      job.state = value.state;
      return { id: input.id, state: value.state, cursor: value.cursor, hasMore: value.hasMore, events };
    } finally { job.polling = false; }
  }
  async function cancel(input) {
    const job = jobInput(input, ['id', 'requestId']); assert(typeof input.requestId === 'string' && REQUEST_ID.test(input.requestId));
    const result = await wire(P.request('POST', `/operations/${input.id}/cancel`, { requestId: input.requestId }), true, job.reservation);
    assert(exact(result, ['id', 'state', 'cancelRequested']) && result.id === input.id && STATES.includes(result.state)
      && typeof result.cancelRequested === 'boolean', 'CONTRACT_MISMATCH');
    job.state = result.state; return { ...result };
  }
  async function uploadCreate(input) {
    assert(exact(input, ['requestId', 'fileName', 'contentType', 'size']) && typeof input.requestId === 'string' && REQUEST_ID.test(input.requestId)
      && P.filename(input.fileName) && P.mime(input.contentType) && integer(input.size, 0, LIMITS.file));
    assert(uploads.size + pendingUploads < LIMITS.uploads, 'RESOURCE_EXHAUSTED');
    pendingUploads++;
    let result, reservation, retained = false;
    try {
      const chunks = Math.ceil(input.size / LIMITS.chunk);
      reservation = session.reserveTransfer?.({ requests: chunks + 4, bytes: Math.ceil(input.size / 3) * 4 + (chunks + 4) * 4096 });
      result = validateUpload(await wire(P.request('POST', '/uploads', input), true, reservation));
      assert(result.size === input.size, 'CONTRACT_MISMATCH');
      if (!uploads.has(result.id)) {
        uploads.set(result.id, { ...result, reservation, hash: crypto.createHash('sha256'), localBytes: 0, localIndex: 0, busy: false }); retained = true;
      }
      return result;
    } finally { pendingUploads--; if (!retained) session.releaseTransfer?.(reservation); }
  }
  async function uploadChunk(input) {
    assert(exact(input, ['id', 'index', 'bytes']) && typeof input.id === 'string' && ID.test(input.id)
      && integer(input.index, 0, Math.ceil(LIMITS.file / LIMITS.chunk) - 1) && input.bytes instanceof Uint8Array
      && input.bytes.length > 0 && input.bytes.length <= LIMITS.chunk);
    const upload = uploads.get(input.id); assert(upload && upload.state === 'open' && !upload.busy, 'OPERATION_NOT_ALLOWED');
    const bytes = Buffer.from(input.bytes); const sha256 = crypto.createHash('sha256').update(bytes).digest('hex');
    const duplicate = input.index === upload.localIndex - 1 && sha256 === upload.lastSha;
    assert(duplicate || (input.index === upload.localIndex && bytes.length === Math.min(LIMITS.chunk, upload.size - upload.localBytes)));
    upload.busy = true;
    try {
      const result = validateUpload(await wire(P.request('POST', `/uploads/${input.id}/chunks`, { index: input.index, data: bytes.toString('base64'), sha256 }), true, upload.reservation), input.id);
      assert(result.size === upload.size && result.state === 'open' && result.nextIndex === (duplicate ? upload.localIndex : upload.localIndex + 1)
        && result.received === upload.localBytes + (duplicate ? 0 : bytes.length), 'CONTRACT_MISMATCH');
      if (!duplicate) { upload.hash.update(bytes); upload.localBytes += bytes.length; upload.localIndex++; upload.lastSha = sha256; }
      Object.assign(upload, result); return result;
    } finally { upload.busy = false; }
  }
  async function uploadComplete(input) {
    assert(exact(input, ['id']) && typeof input.id === 'string' && ID.test(input.id));
    const upload = uploads.get(input.id);
    if (upload?.state === 'complete' && !upload.hash) return validateUpload(upload, input.id);
    assert(upload && !upload.busy && upload.localBytes === upload.size, 'OPERATION_NOT_ALLOWED');
    assert(['open', 'complete'].includes(upload.state), 'OPERATION_NOT_ALLOWED'); upload.busy = true;
    try {
      const sha256 = upload.hash.copy().digest('hex');
      const result = validateUpload(await wire(P.request('POST', `/uploads/${input.id}/complete`, { sha256 }), true, upload.reservation), input.id);
      assert(result.state === 'complete' && result.size === upload.size && result.sha256 === sha256, 'CONTRACT_MISMATCH');
      Object.assign(upload, result); return result;
    } finally { upload.busy = false; }
  }
  async function uploadStatus(input) {
    assert(exact(input, ['id']) && typeof input.id === 'string' && ID.test(input.id));
    const result = validateUpload(await wire(P.request('GET', `/uploads/${input.id}`), false, uploads.get(input.id)?.reservation), input.id);
    // Completed files may be reattached only after the server reauthorizes this
    // account/client/workspace. Partial files need the original local hash state.
    if (!uploads.has(input.id) && result.state === 'complete') {
      assert(uploads.size < LIMITS.uploads, 'RESOURCE_EXHAUSTED'); uploads.set(input.id, { ...result });
    }
    return result;
  }
  async function uploadCancel(input) {
    assert(exact(input, ['id']) && typeof input.id === 'string' && ID.test(input.id));
    const reservation = uploads.get(input.id)?.reservation;
    const result = validateUpload(await wire(P.request('DELETE', `/uploads/${input.id}`), true, reservation), input.id);
    if (result.state === 'cancelled') { session.releaseTransfer?.(reservation); uploads.delete(input.id); } return result;
  }
  async function blobRead(input) {
    assert(exact(input, ['id', 'offset', 'limit']) && typeof input.id === 'string' && ID.test(input.id)
      && integer(input.offset, 0, LIMITS.file) && integer(input.limit, 1, LIMITS.chunk));
    const blob = blobs.get(input.id); assert(blob, 'OPERATION_NOT_ALLOWED');
    assert(input.offset <= blob.size);
    const result = await wire(P.request('GET', `/blobs/${input.id}?offset=${input.offset}&limit=${input.limit}`));
    assert(exact(result, ['id', 'offset', 'size', 'contentType', 'sha256', 'data', 'hasMore'], ['fileName'])
      && result.id === input.id && result.offset === input.offset && result.size === blob.size
      && result.sha256 === blob.sha256 && result.contentType === blob.contentType && typeof result.hasMore === 'boolean', 'CONTRACT_MISMATCH');
    const data = P.base64(result.data, input.limit);
    assert(data.length === Math.min(input.limit, blob.size - input.offset)
      && result.hasMore === (input.offset + data.length < blob.size), 'CONTRACT_MISMATCH');
    return { ...blob, offset: input.offset, data: new Uint8Array(data), hasMore: result.hasMore };
  }
  async function* blobStream(id, start = 0, end) {
    const blob = blobs.get(id); assert(blob, 'OPERATION_NOT_ALLOWED'); end ??= blob.size;
    const hash = crypto.createHash('sha256'); let offset = start;
    while (offset < end) {
      current(); const result = await blobRead({ id, offset, limit: Math.min(LIMITS.chunk, end - offset) });
      hash.update(result.data); offset += result.data.length; yield result.data;
    }
    current(); if (start === 0 && end === blob.size) assert(hash.digest('hex') === blob.sha256, 'CONTRACT_MISMATCH');
  }
  async function save(input) {
    assert(exact(input, ['fileName', 'contentType', 'source']) && P.filename(input.fileName) && typeof input.contentType === 'string');
    const contentType = input.contentType.replace(/;\s*charset=utf-8$/i, '').toLowerCase();
    assert(P.mime(contentType));
    const src = input.source;
    assert(P.plain(src) && ['bytes', 'blob'].includes(src.kind));
    let source;
    if (src.kind === 'bytes') {
      assert(exact(src, ['kind', 'bytes']) && src.bytes instanceof Uint8Array && src.bytes.length <= LIMITS.job);
      const bytes = new Uint8Array(src.bytes); source = (async function* () { current(); yield bytes; current(); })();
    } else {
      assert(exact(src, ['kind', 'id']) && typeof src.id === 'string' && ID.test(src.id));
      const blob = blobs.get(src.id); assert(blob && blob.contentType === contentType, 'OPERATION_NOT_ALLOWED'); source = blobStream(src.id);
    }
    assert(typeof host.save === 'function', 'OPERATION_NOT_ALLOWED'); assert(saves < 1, 'RESOURCE_EXHAUSTED'); saves++;
    try { const saved = await host.save({ fileName: input.fileName, contentType, source, isCurrent: () => !closed && isCurrent() });
      current(); assert(typeof saved === 'boolean', 'CONTRACT_MISMATCH'); return { saved }; }
    finally { saves--; }
  }
  async function openMedia(input) {
    const validated = P.operationRequest(input, uploads); assert(validated.operation.response === 'bytes' && !validated.operation.mutating, 'OPERATION_NOT_ALLOWED');
    assert(media.size + pendingMedia < 8, 'RESOURCE_EXHAUSTED');
    pendingMedia++;
    try {
    const started = await start(input); let after = 0, blob;
    const deadline = Date.now() + 600000;
    for (;;) {
      current(); assert(Date.now() < deadline, 'REQUEST_TIMEOUT');
      const page = await poll({ id: started.id, after, waitMs: LIMITS.wait }); after = page.cursor;
      for (const event of page.events) {
        if (event.kind === 'headers') assert(event.status === 200, 'REMOTE_ERROR');
        if (event.kind === 'blob') { assert(!blob, 'CONTRACT_MISMATCH'); blob = event.blob; }
        if (event.kind === 'error') throw new DesktopError('REMOTE_ERROR');
      }
      if (TERMINAL.includes(page.state) && !page.hasMore) {
        assert(page.state === 'succeeded' && blob && MEDIA.has(blob.contentType) && blob.size > 0, 'OPERATION_NOT_ALLOWED'); break;
      }
    }
    const handle = crypto.randomBytes(16).toString('hex'); media.set(handle, blob);
    return { handle, url: `zhijun-media://session/${handle}`, contentType: blob.contentType, size: blob.size };
    } finally { pendingMedia--; }
  }
  function closeMedia(input) {
    assert(exact(input, ['handle']) && typeof input.handle === 'string' && ID.test(input.handle));
    const result = media.delete(input.handle);
    for (const stream of [...streams.values()]) if (stream.handle === input.handle) stream.stop();
    return { closed: result };
  }
  async function mediaResponse(request) {
    // Media is consumed by native image/audio elements or the bundled PDF.js
    // canvas renderer. The capability URL is unguessable and generation-bound;
    // the strict MIME allowlist plus nosniff prevents binary data becoming script.
    const headers = { 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
      'Access-Control-Allow-Origin': 'zhijun://desktop', 'Vary': 'Origin' };
    try {
      current(); const url = new URL(request.url);
      assert(url.protocol === 'zhijun-media:' && url.host === 'session' && !url.search && !url.hash && !url.username && !url.password
        && /^\/[a-f0-9]{32}$/.test(url.pathname) && ['GET', 'HEAD'].includes(request.method));
      const handle = url.pathname.slice(1), blob = media.get(handle); assert(blob && MEDIA.has(blob.contentType), 'ACCESS_DENIED');
      let start = 0, end = blob.size, status = 200;
      const range = request.headers.get('range');
      if (range) {
        const match = range.match(/^bytes=(?:(0|[1-9][0-9]*)-(0|[1-9][0-9]*)?|-([1-9][0-9]*))$/);
        const suffix = match?.[3] === undefined ? null : Number(match[3]);
        if (!match || (suffix !== null ? !integer(suffix, 1, Number.MAX_SAFE_INTEGER)
          : !integer(Number(match[1]), 0, blob.size - 1) || (match[2] && !integer(Number(match[2]), Number(match[1]), Number.MAX_SAFE_INTEGER)))) {
          return new Response(null, { status: 416, headers: { ...headers, 'Content-Range': `bytes */${blob.size}` } });
        }
        start = suffix === null ? Number(match[1]) : Math.max(0, blob.size - suffix);
        end = suffix !== null || !match[2] ? blob.size : Math.min(blob.size, Number(match[2]) + 1); status = 206;
        headers['Content-Range'] = `bytes ${start}-${end - 1}/${blob.size}`;
      }
      Object.assign(headers, { 'Content-Type': blob.contentType, 'Content-Length': String(end - start), 'Accept-Ranges': 'bytes' });
      if (request.method === 'HEAD') return new Response(null, { status, headers });
      assert(mediaStreams < 4, 'RESOURCE_EXHAUSTED'); mediaStreams++;
      const iterator = blobStream(blob.id, start, end); let done = false;
      const release = () => { if (!done) { done = true; mediaStreams--; streams.delete(iterator); } };
      const stream = new ReadableStream({
        start(controller) { streams.set(iterator, { handle, stop() { release(); controller.error(new DesktopError('READ_CANCELLED')); void iterator.return(); } }); },
        async pull(controller) {
          try {
            current(); assert(media.has(handle), 'READ_CANCELLED'); const next = await iterator.next();
            current(); assert(media.has(handle), 'READ_CANCELLED');
            if (next.done) { release(); controller.close(); } else controller.enqueue(next.value);
          } catch (error) { const wasDone = done; release(); await iterator.return(); reportTerminal(error); if (!wasDone) controller.error(error); }
        },
        async cancel() { release(); await iterator.return(); },
      });
      return new Response(stream, { status, headers });
    } catch { return new Response(null, { status: 403, headers }); }
  }
  async function requestMicrophone(input) {
    assert(input === undefined);
    assert(typeof host.requestMicrophone === 'function', 'OPERATION_NOT_ALLOWED');
    const result = await host.requestMicrophone(microphoneOwner);
    current(); assert(typeof result === 'boolean', 'CONTRACT_MISMATCH'); return { allowed: result };
  }
  const methods = { requestMicrophone, start, poll, cancel, uploadCreate, uploadChunk, uploadComplete, uploadStatus, uploadCancel, blobRead, save, openMedia, closeMedia };
  return {
    async invoke(method, input) {
      current(); assert(Object.hasOwn(methods, method));
      let rejectGuard;
      // A heartbeat can close the session while a write is awaiting its reply.
      // The outer cancellation guard must retain the same uncertainty as wire().
      const mutation = method === 'start' ? P.operationRequest(input, uploads).operation.mutating
        : ['cancel', 'uploadCreate', 'uploadChunk', 'uploadComplete', 'uploadCancel'].includes(method);
      try {
        const task = methods[method](input);
        const value = await Promise.race([task, new Promise((_, reject) => { rejectGuard = reject; guards.add(reject); })]);
        current(); return value;
      } catch (error) {
        if (mutation && error instanceof DesktopError && !error.definitelyNotSent && UNCERTAIN_WRITE_ERRORS.includes(error.code)) {
          const unknown = new DesktopError('WRITE_OUTCOME_UNKNOWN');
          if (TERMINAL_CONNECTION_ERRORS.includes(error.code)) unknown.sessionTerminal = error;
          error = unknown;
        }
        reportTerminal(error); throw error;
      }
      finally { guards.delete(rejectGuard); }
    },
    mediaResponse,
    close(reason = new DesktopError('STALE_GENERATION')) {
      if (closed) return; closed = true;
      host.revokeMicrophone?.(microphoneOwner);
      for (const reject of guards) reject(reason);
      for (const controller of requestControllers) controller.abort();
      for (const upload of uploads.values()) session.releaseTransfer?.(upload.reservation);
      // Local suppression is immediate. Session teardown and the box lease own
      // remote cleanup; no fabricated remote-cancel acknowledgement is returned.
      for (const stream of [...streams.values()]) stream.stop();
      jobs.clear(); uploads.clear(); blobs.clear(); media.clear();
    },
  };
}
module.exports = { createProductSession, productMethods, MEDIA };
