'use strict';

const { DesktopError } = require('./public-error.cjs');
const MAX_SUBSCRIBERS = 64;

function createReadScheduler({ maxInFlight = 2, maxQueued = 8, timeoutMs = 10000 } = {}) {
  if (!Number.isInteger(maxInFlight) || maxInFlight < 1 || maxInFlight > 2
      || !Number.isInteger(maxQueued) || maxQueued < 0 || maxQueued > 8
      || !Number.isInteger(timeoutMs) || timeoutMs < 1 || timeoutMs > 120000) {
    throw new DesktopError('INVALID_REQUEST');
  }
  let disposed = false;
  let inFlight = 0;
  let queue = [];
  const jobs = new Map();
  const subscribers = new Map();
  const current = (subscriber) => {
    try { return subscriber.isCurrent() === true; } catch { return false; }
  };
  const forgetJob = (job) => {
    if (jobs.get(job.key) === job) jobs.delete(job.key);
  };
  const settle = (subscriber, error, data) => {
    if (subscriber.done) return;
    subscriber.done = true;
    clearTimeout(subscriber.timer);
    subscribers.delete(subscriber.id);
    const job = subscriber.job;
    job.subscribers.delete(subscriber);
    if (error) subscriber.reject(error); else subscriber.resolve(data);
    if (job.subscribers.size === 0) {
      forgetJob(job);
      if (!job.started) queue = queue.filter((candidate) => candidate !== job);
    }
  };
  const prune = (job) => {
    for (const subscriber of [...job.subscribers]) {
      if (!current(subscriber)) settle(subscriber, new DesktopError('STALE_GENERATION'));
    }
  };
  const pump = () => {
    while (!disposed && inFlight < maxInFlight && queue.length > 0) {
      const job = queue.shift();
      prune(job);
      if (!job.subscribers.size) continue;
      job.started = true;
      inFlight += 1;
      // Keep the slot until the real promise settles, even after local cancellation.
      Promise.resolve().then(() => {
        prune(job);
        if (job.subscribers.size === 0) return undefined;
        return job.run();
      }).then(
        (data) => {
          for (const subscriber of [...job.subscribers]) {
            settle(subscriber, current(subscriber) ? null : new DesktopError('STALE_GENERATION'), data);
          }
        },
        (error) => {
          for (const subscriber of [...job.subscribers]) {
            const safeError = error instanceof DesktopError ? error : new DesktopError('REMOTE_ERROR');
            settle(subscriber, current(subscriber) ? safeError : new DesktopError('STALE_GENERATION'));
          }
        },
      ).finally(() => {
        inFlight -= 1;
        forgetJob(job);
        pump();
      });
    }
  };

  function schedule({ key, callId, senderId, generation, run, isCurrent }) {
    if (disposed) return Promise.reject(new DesktopError('OPERATION_NOT_ALLOWED'));
    if (typeof key !== 'string' || !key || key.length > 4096
        || typeof callId !== 'string' || !/^[A-Za-z0-9_-]{8,100}$/.test(callId)
        || !Number.isSafeInteger(senderId) || senderId < 0
        || !Number.isSafeInteger(generation) || generation < 0
        || typeof run !== 'function' || typeof isCurrent !== 'function') {
      return Promise.reject(new DesktopError('INVALID_REQUEST'));
    }
    const id = JSON.stringify([senderId, generation, callId]);
    const jobKey = JSON.stringify([senderId, generation, key]);
    if (subscribers.has(id)) return Promise.reject(new DesktopError('INVALID_REQUEST'));
    if (!current({ isCurrent })) return Promise.reject(new DesktopError('STALE_GENERATION'));
    if (subscribers.size >= MAX_SUBSCRIBERS) return Promise.reject(new DesktopError('RESOURCE_EXHAUSTED'));
    let job = jobs.get(jobKey);
    if (!job) {
      if (inFlight >= maxInFlight && queue.length >= maxQueued) {
        return Promise.reject(new DesktopError('RESOURCE_EXHAUSTED'));
      }
      job = { key: jobKey, run, subscribers: new Set(), started: false };
      jobs.set(jobKey, job);
      queue.push(job);
    }
    const promise = new Promise((resolve, reject) => {
      const subscriber = { id, job, resolve, reject, isCurrent, done: false };
      subscribers.set(id, subscriber);
      job.subscribers.add(subscriber);
      subscriber.timer = setTimeout(() => {
        settle(subscriber, new DesktopError(current(subscriber) ? 'REQUEST_TIMEOUT' : 'STALE_GENERATION'));
        pump();
      }, timeoutMs);
    });
    pump();
    return promise;
  }

  function cancel({ callId, senderId, generation }) {
    const subscriber = subscribers.get(JSON.stringify([senderId, generation, callId]));
    if (!subscriber) return false;
    settle(subscriber, new DesktopError(current(subscriber) ? 'READ_CANCELLED' : 'STALE_GENERATION'));
    pump();
    return true;
  }
  function invalidate() {
    for (const subscriber of [...subscribers.values()]) settle(subscriber, new DesktopError('STALE_GENERATION'));
    queue = [];
  }
  function dispose() { disposed = true; invalidate(); }
  const stats = () => ({ inFlight, queued: queue.length, subscribers: subscribers.size, disposed });
  return { schedule, cancel, invalidate, dispose, stats };
}

module.exports = { createReadScheduler, MAX_SUBSCRIBERS };
