'use strict'
// Sandboxed preload: no application-module or filesystem imports.
const { contextBridge, ipcRenderer } = require('electron')
const invoke = (operation, ...args) => ipcRenderer.invoke('zhijun:invoke', operation, args)
contextBridge.exposeInMainWorld('zhijunDesktop', Object.freeze({
  protocolVersion: 1,
  getSnapshot: () => invoke('getSnapshot'),
  subscribe(listener) {
    if (typeof listener !== 'function') throw new TypeError('Expected a snapshot listener')
    const receive = (_event, snapshot) => listener(snapshot)
    ipcRenderer.on('zhijun:snapshot', receive)
    let active = true
    return () => { if (active) { active = false; ipcRenderer.removeListener('zhijun:snapshot', receive) } }
  },
  beginSignIn: context => invoke('beginSignIn', context),
  listDevices: context => invoke('listDevices', context),
  connect: (context, deviceId) => invoke('connect', context, deviceId),
  disconnect: context => invoke('disconnect', context),
  signOut: context => invoke('signOut', context),
  materials: Object.freeze({ list: (context, query) => invoke('materials.list', context, query) }),
  cancelRead: (context, targetCallId) => invoke('cancelRead', context, targetCallId),
}))
