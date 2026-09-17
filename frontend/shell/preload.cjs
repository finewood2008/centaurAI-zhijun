'use strict'
// Sandboxed preload: no application-module or filesystem imports.
const { contextBridge, ipcRenderer } = require('electron')
const invoke = (operation, ...args) => ipcRenderer.invoke('zhijun:invoke', operation, args)
contextBridge.exposeInMainWorld('zhijunDesktop', Object.freeze({
  protocolVersion: 1,
  getSnapshot: () => invoke('getSnapshot'),
  getRememberedLogin: context => invoke('getRememberedLogin', context),
  subscribe(listener) {
    if (typeof listener !== 'function') throw new TypeError('Expected a snapshot listener')
    const receive = (_event, snapshot) => listener(snapshot)
    ipcRenderer.on('zhijun:snapshot', receive)
    let active = true
    return () => { if (active) { active = false; ipcRenderer.removeListener('zhijun:snapshot', receive) } }
  },
  beginSignIn: context => invoke('beginSignIn', context),
  signInWithPassword: (context, credentials) => invoke('signInWithPassword', context, credentials),
  signInWithSavedPassword: (context, rememberPassword) => invoke('signInWithSavedPassword', context, rememberPassword),
  sendRegistrationCode: (context, phone, scene) => invoke('sendRegistrationCode', context, phone,
    ...(scene === undefined ? [] : [scene])),
  resetPassword: (context, credentials) => invoke('resetPassword', context, credentials),
  registerWithPassword: (context, credentials) => invoke('registerWithPassword', context, credentials),
  listDevices: context => invoke('listDevices', context),
  claimDevice: (context, claimToken) => invoke('claimDevice', context, claimToken),
  openProvisioning: context => invoke('openProvisioning', context),
  connect: (context, deviceId) => invoke('connect', context, deviceId),
  disconnect: context => invoke('disconnect', context),
  signOut: context => invoke('signOut', context),
  product: Object.freeze({ ...Object.fromEntries(['start', 'poll', 'cancel', 'uploadCreate', 'uploadChunk', 'uploadComplete', 'uploadStatus', 'uploadCancel', 'blobRead', 'save', 'openMedia', 'closeMedia']
    .map(method => [method, (context, input) => invoke(`product.${method}`, context, input)])),
    requestMicrophone(context) {
      // Preload owns this user-activation check; renderer cannot request an OS
      // permission by scheduling the public method from an untrusted timer.
      if (!globalThis.navigator?.userActivation?.isActive) return Promise.resolve({ ok: false,
        generation: context?.expectedGeneration ?? 0,
        error: { code: 'OPERATION_NOT_ALLOWED', message: '请点击录音按钮后再允许麦克风。', recovery: 'none' } })
      return invoke('product.requestMicrophone', context)
    },
  }),
  materials: Object.freeze({ list: (context, query) => invoke('materials.list', context, query) }),
  cancelRead: (context, targetCallId) => invoke('cancelRead', context, targetCallId),
}))
