const SHARE_DB = 'centaur-mobile-shares';
const SHARE_STORE = 'shares';

function openShareDb() {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open(SHARE_DB, 1);
    req.onupgradeneeded = () => {
      const db = req.result;
      if (!db.objectStoreNames.contains(SHARE_STORE)) db.createObjectStore(SHARE_STORE, { keyPath: 'id' });
    };
    req.onsuccess = () => resolve(req.result);
    req.onerror = () => reject(req.error);
  });
}

async function putShare(data) {
  const db = await openShareDb();
  return new Promise((resolve, reject) => {
    const tx = db.transaction(SHARE_STORE, 'readwrite');
    tx.objectStore(SHARE_STORE).put(data);
    tx.oncomplete = () => resolve();
    tx.onerror = () => reject(tx.error);
  });
}

self.addEventListener('fetch', (event) => {
  const url = new URL(event.request.url);
  if (url.pathname !== '/mobile/share' || event.request.method !== 'POST') return;
  event.respondWith((async () => {
    const form = await event.request.formData();
    const id = String(Date.now()) + '-' + Math.random().toString(36).slice(2);
    const files = form.getAll('files').filter((x) => x instanceof File && x.size > 0);
    await putShare({
      id,
      title: String(form.get('title') || ''),
      text: String(form.get('text') || ''),
      url: String(form.get('url') || ''),
      files,
      created_at: new Date().toISOString(),
    });
    return Response.redirect('/mobile?shared=' + encodeURIComponent(id), 303);
  })());
});
