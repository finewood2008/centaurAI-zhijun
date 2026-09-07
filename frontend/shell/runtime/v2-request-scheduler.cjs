'use strict';
const { DesktopError } = require('./public-error.cjs');
const notSent = code => new DesktopError(code, { definitelyNotSent: true });

// One instance belongs to one native SDK session. Rejections and retries consume
// this budget too; neither idle time nor a new product manager resets it.
function createV2Scheduler({ send, clock = () => performance.now(), timers = { setTimeout, clearTimeout },
  intervalMs = 600, queueTimeoutMs = 15000, requestTimeoutMs = 12000,
  maxQueued = 16, maxInFlight = 8, maxRequests = 1000, maxBytes = 1024 ** 3 - 16 * 1024 ** 2 } = {}) {
  let closed, timer, nextStart = -Infinity, count = 0, bytes = 0, sequence = 0;
  let active = 0, businessActive = 0;
  const queue = [], running = new Set(), reservations = new Set();
  const terminal = error => error?.code === 'SESSION_QUOTA_EXHAUSTED';
  function reserved(field, except) { let sum = 0; for (const value of reservations) if (value !== except) sum += value[field]; return sum; }
  function releaseTransfer(value) { reservations.delete(value); }
  function reserveTransfer(value) {
    if (closed) throw closed;
    if (!value || !Number.isSafeInteger(value.requests) || value.requests < 1 || !Number.isSafeInteger(value.bytes) || value.bytes < 0) throw notSent('INVALID_REQUEST');
    // Keep room for context, task submission/poll, cancellation and error reads.
    if (count + reserved('requests') + value.requests + 32 > maxRequests || bytes + reserved('bytes') + value.bytes > maxBytes) {
      // A second transfer must not terminate another already admitted upload.
      throw notSent(reservations.size ? 'RESOURCE_EXHAUSTED' : 'SESSION_QUOTA_EXHAUSTED');
    }
    const token = { requests: value.requests, bytes: value.bytes }; reservations.add(token); return token;
  }
  function settle(item, error, response) {
    if (item.settled) return;
    item.settled = true; timers.clearTimeout(item.deadline);
    item.signal?.removeEventListener('abort', item.abort);
    error ? item.reject(error) : item.resolve(response);
    if (!item.nativePending) item.completeWork();
  }
  function close(error = notSent('SESSION_NOT_READY')) {
    if (closed) return;
    closed = error; timers.clearTimeout(timer);
    for (const item of queue.splice(0)) settle(item, new DesktopError(error.code, { ...error, definitelyNotSent: true }));
    // One request exhausting a quota does not prove another in-flight write was
    // rejected. Preserve uncertainty separately for every dispatched item.
    for (const item of running) settle(item, new DesktopError(error.code, { ...error, definitelyNotSent: !item.dispatched }));
    reservations.clear();
  }
  function priority(item, now) {
    if (item.priority === 0) return 0;
    return now - item.enqueued >= 3000 ? 1 : item.priority;
  }
  function arm() {
    timers.clearTimeout(timer);
    if (closed || !queue.length) return;
    const now = clock();
    let at = Infinity;
    for (const item of queue) {
      at = Math.min(at, item.expires);
      if (active < maxInFlight && (item.priority === 0 || businessActive < maxInFlight - 1)) at = Math.min(at, Math.max(nextStart, item.ready));
    }
    timer = timers.setTimeout(pump, Math.max(0, at - now));
  }
  function pump() {
    if (closed) return;
    const now = clock();
    for (let i = queue.length - 1; i >= 0; i--) if (queue[i].expires <= now) settle(queue.splice(i, 1)[0], notSent('RATE_LIMITED'));
    const candidates = queue.filter(item => item.ready <= now && active < maxInFlight && (item.priority === 0 || businessActive < maxInFlight - 1));
    candidates.sort((a, b) => priority(a, now) - priority(b, now) || a.sequence - b.sequence);
    const item = candidates[0];
    if (!item || nextStart > now) { arm(); return; }
    queue.splice(queue.indexOf(item), 1);
    const reservation = reservations.has(item.reservation) ? item.reservation : undefined;
    const requestBytes = item.request.body?.byteLength || 0;
    const ownRequest = reservation?.requests > 0 ? 1 : 0;
    const ownBytes = Math.min(reservation?.bytes || 0, requestBytes);
    const controlReserve = reservations.size && !reservation && item.priority > 1 ? 8 : 0;
    if (count + reserved('requests') + 1 - ownRequest + controlReserve > maxRequests || bytes + reserved('bytes') + requestBytes - ownBytes > maxBytes) {
      if (count + 1 <= maxRequests && bytes + requestBytes <= maxBytes) {
        // Available native quota is promised to an admitted transfer. An
        // unrelated read cannot consume it or close that transfer's session.
        settle(item, notSent('RESOURCE_EXHAUSTED')); arm(); return;
      }
      const error = notSent('SESSION_QUOTA_EXHAUSTED'); settle(item, error); close(error); return;
    }
    if (reservation) { reservation.requests -= ownRequest; reservation.bytes -= ownBytes; }
    count++; bytes += requestBytes; active++; if (item.priority !== 0) businessActive++;
    nextStart = now + intervalMs; running.add(item); item.dispatched = true; item.nativePending = true;
    item.onDispatch?.();
    item.deadline = timers.setTimeout(() => settle(item, new DesktopError('REQUEST_TIMEOUT')), requestTimeoutMs);
    let native;
    try { native = Promise.resolve(send(item.request, item.handshake)); } catch (error) { native = Promise.reject(error); }
    native.then(response => {
      const used = response?.body?.byteLength || 0;
      bytes += used;
      if (reservation && reservations.has(reservation)) reservation.bytes -= Math.min(reservation.bytes, used);
      if (bytes > maxBytes) {
        const error = new DesktopError('SESSION_QUOTA_EXHAUSTED'); settle(item, error); close(error);
      } else settle(item, null, response);
    }, error => {
      // Only these adapter errors prove the Agent/SDK did not dispatch HTTP.
      // Unknown mutations and transport errors never enter this retry path.
      if (!closed && !item.settled && error?.code === 'RATE_LIMITED' && error.definitelyNotSent && item.attempt < 2 && queue.length < maxQueued) {
        timers.clearTimeout(item.deadline); item.deadline = undefined;
        item.attempt++; item.dispatched = false;
        item.ready = clock() + item.attempt * 1200; item.expires = item.ready + queueTimeoutMs;
        queue.push(item);
      } else { settle(item, error); if (terminal(error)) close(error); }
    }).finally(() => {
      active--; if (item.priority !== 0) businessActive--; running.delete(item);
      item.nativePending = false; if (item.settled) item.completeWork(); arm();
    });
    arm();
  }
  function request(request, { priority: rank = 2, signal, onDispatch, onWork, reservation, handshake = false } = {}) {
    if (closed) return Promise.reject(closed);
    if (signal?.aborted) return Promise.reject(notSent('STALE_GENERATION'));
    if (queue.length >= maxQueued) return Promise.reject(notSent('RESOURCE_EXHAUSTED'));
    return new Promise((resolve, reject) => {
      const item = { request, priority: rank, signal, onDispatch, reservation, handshake, resolve, reject,
        sequence: ++sequence, enqueued: clock(), ready: clock(), expires: clock() + queueTimeoutMs, attempt: 0 };
      // Public delivery may be cancelled before native work settles. Keep the
      // runtime-wide capacity promise alive until actual native completion.
      const work = new Promise(done => { item.completeWork = done; }); onWork?.(work);
      item.abort = () => {
        const index = queue.indexOf(item); if (index >= 0) queue.splice(index, 1);
        settle(item, new DesktopError('STALE_GENERATION', { definitelyNotSent: !item.dispatched })); arm();
      };
      signal?.addEventListener('abort', item.abort, { once: true });
      queue.push(item); pump();
    });
  }
  return { request, close, reserveTransfer, releaseTransfer };
}
module.exports = { createV2Scheduler };
