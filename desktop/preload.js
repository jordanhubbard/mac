const { contextBridge, ipcRenderer } = require("electron");

contextBridge.exposeInMainWorld("macDashboard", {
  async connection() {
    return ipcRenderer.invoke("mac-dashboard:connection");
  },
  async request(path, init) {
    return ipcRenderer.invoke("mac-dashboard:request", { path, init });
  },
  async openService(serviceId, fallbackUrl) {
    return ipcRenderer.invoke("mac-dashboard:open-service", { serviceId, fallbackUrl });
  },
});
