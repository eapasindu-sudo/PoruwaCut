const { contextBridge, ipcRenderer } = require("electron");
contextBridge.exposeInMainWorld("resolveApi", {
  snapshot: () => ipcRenderer.invoke("resolve:snapshot"),
  markMoments: (m) => ipcRenderer.invoke("resolve:markMoments", m),
  clearMarkers: () => ipcRenderer.invoke("resolve:clearMarkers"),
  markSong: (s) => ipcRenderer.invoke("resolve:markSong", s),
  build: (plan, opts) => ipcRenderer.invoke("resolve:build", plan, opts),
  learnSnapshot: () => ipcRenderer.invoke("resolve:learnSnapshot"),
});
