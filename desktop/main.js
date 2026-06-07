const { app, BrowserWindow, ipcMain, shell } = require("electron");
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");
const path = require("node:path");

const DEFAULT_API_URL = "http://127.0.0.1:8789";
const DEFAULT_API_TUNNEL_PORT = 18789;
const DEFAULT_PROXY_HOST = "127.0.0.1";
const DEFAULT_REMOTE_HOST = "127.0.0.1";
const DEFAULT_REMOTE_PORT = 8789;

const runtime = {
  profile: null,
  apiTargetUrl: "",
  proxyUrl: "",
  proxyServer: null,
  windows: new Set(),
  children: new Set(),
  tunnels: new Map(),
};

function parseArgs(argv) {
  const args = {};
  for (let i = 0; i < argv.length; i += 1) {
    const item = argv[i];
    if (!item.startsWith("--")) continue;
    const eq = item.indexOf("=");
    const key = item.slice(2, eq >= 0 ? eq : undefined);
    const value = eq >= 0 ? item.slice(eq + 1) : argv[i + 1] && !argv[i + 1].startsWith("--") ? argv[++i] : "1";
    args[key] = value;
  }
  return args;
}

function expandHome(value) {
  if (!value || typeof value !== "string") return value;
  if (value === "~") return app.getPath("home");
  if (value.startsWith("~/")) return path.join(app.getPath("home"), value.slice(2));
  return value;
}

function readJsonFile(filePath, profileName) {
  if (!filePath) return {};
  const abs = path.resolve(filePath);
  const raw = JSON.parse(fs.readFileSync(abs, "utf8"));
  if (profileName) {
    if (raw && raw[profileName]) return raw[profileName];
    throw new Error(`profile ${profileName} not found in ${abs}`);
  }
  return raw;
}

function boolValue(value, fallback = true) {
  if (value === undefined || value === null || value === "") return fallback;
  return !["0", "false", "no", "off"].includes(String(value).toLowerCase());
}

function numberValue(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed > 0 ? Math.floor(parsed) : fallback;
}

function loadProfile() {
  const args = parseArgs(process.argv.slice(2));
  const profileName = args["profile-name"] || process.env.MAC_DESKTOP_PROFILE_NAME || "";
  const fileProfile = readJsonFile(args.profile || process.env.MAC_DESKTOP_PROFILE, profileName);
  const profile = { ...fileProfile };

  profile.displayName = args.name || process.env.MAC_DESKTOP_DISPLAY_NAME || profile.displayName || "MAC Control Plane";
  profile.apiUrl = args["api-url"] || process.env.MAC_DESKTOP_API_URL || profile.apiUrl || "";
  profile.token = process.env.MAC_DESKTOP_API_TOKEN || profile.token || "";
  profile.tokenEnv = args["token-env"] || process.env.MAC_DESKTOP_API_TOKEN_ENV || profile.tokenEnv || "MAC_DESKTOP_API_TOKEN";
  profile.proxyPort = numberValue(args["proxy-port"] || process.env.MAC_DESKTOP_PROXY_PORT, profile.proxyPort || 0);
  profile.serviceTunnels = profile.serviceTunnels || {};

  const sshTarget = args.ssh || args["ssh-target"] || process.env.MAC_DESKTOP_SSH_TARGET;
  if (sshTarget) {
    profile.ssh = {
      ...(profile.ssh || {}),
      target: sshTarget,
      localPort: numberValue(args["ssh-local-port"] || process.env.MAC_DESKTOP_SSH_LOCAL_PORT, profile.ssh?.localPort || DEFAULT_API_TUNNEL_PORT),
      remoteHost: args["ssh-remote-host"] || process.env.MAC_DESKTOP_SSH_REMOTE_HOST || profile.ssh?.remoteHost || DEFAULT_REMOTE_HOST,
      remotePort: numberValue(args["ssh-remote-port"] || process.env.MAC_DESKTOP_SSH_REMOTE_PORT, profile.ssh?.remotePort || DEFAULT_REMOTE_PORT),
      identityFile: expandHome(args["ssh-key"] || process.env.MAC_DESKTOP_SSH_KEY || profile.ssh?.identityFile || ""),
      jumpHost: args["ssh-jump"] || process.env.MAC_DESKTOP_SSH_JUMP || profile.ssh?.jumpHost || "",
      strictHostKeyChecking: boolValue(
        args["ssh-strict-host-key-checking"] || process.env.MAC_DESKTOP_SSH_STRICT_HOST_KEY_CHECKING,
        profile.ssh?.strictHostKeyChecking !== false,
      ),
    };
  }

  if (!profile.apiUrl && !profile.ssh) profile.apiUrl = DEFAULT_API_URL;
  return profile;
}

function profileToken() {
  const profile = runtime.profile || {};
  return profile.token || process.env[profile.tokenEnv || "MAC_DESKTOP_API_TOKEN"] || "";
}

function stripHopByHopHeaders(headers) {
  const next = { ...headers };
  for (const key of [
    "connection",
    "host",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
  ]) {
    delete next[key];
  }
  return next;
}

function waitForTcp(host, port, timeoutMs = 12000) {
  const deadline = Date.now() + timeoutMs;
  return new Promise((resolve, reject) => {
    let finished = false;
    function attempt() {
      if (finished) return;
      const socket = net.connect({ host, port });
      socket.setTimeout(800);
      socket.once("connect", () => {
        finished = true;
        socket.destroy();
        resolve();
      });
      socket.once("timeout", () => socket.destroy());
      socket.once("error", () => {});
      socket.once("close", () => {
        if (finished) return;
        if (Date.now() > deadline) reject(new Error(`timed out waiting for ${host}:${port}`));
        else setTimeout(attempt, 150);
      });
    }
    attempt();
  });
}

function listenServer(server, preferredPort = 0) {
  return new Promise((resolve, reject) => {
    server.once("error", reject);
    server.listen(preferredPort || 0, DEFAULT_PROXY_HOST, () => {
      server.removeListener("error", reject);
      resolve(server.address().port);
    });
  });
}

async function startProcess(command, args, options = {}) {
  const child = spawn(command, args, { ...options, stdio: ["ignore", "ignore", "pipe"] });
  runtime.children.add(child);
  child.once("exit", () => runtime.children.delete(child));
  return child;
}

async function startLocalServerIfConfigured(profile) {
  const spec = profile.localServer;
  if (!spec || !Array.isArray(spec.command) || !spec.command.length) return "";
  const child = await startProcess(spec.command[0], spec.command.slice(1), {
    cwd: spec.cwd ? path.resolve(spec.cwd) : process.cwd(),
    env: { ...process.env, ...(spec.env || {}) },
  });
  child.stderr.on("data", (chunk) => process.stderr.write(`[mac-local-server] ${chunk}`));
  const url = spec.url || profile.apiUrl || DEFAULT_API_URL;
  const parsed = new URL(url);
  await waitForTcp(parsed.hostname, Number(parsed.port || (parsed.protocol === "https:" ? 443 : 80)), spec.timeoutMs || 15000);
  return url;
}

function sshArgsFor(spec) {
  const remoteHost = spec.remoteHost || DEFAULT_REMOTE_HOST;
  const remotePort = numberValue(spec.remotePort, DEFAULT_REMOTE_PORT);
  const localPort = numberValue(spec.localPort, DEFAULT_API_TUNNEL_PORT);
  const args = [
    "-N",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes",
    "-L", `${DEFAULT_PROXY_HOST}:${localPort}:${remoteHost}:${remotePort}`,
  ];
  if (spec.strictHostKeyChecking === false) {
    args.push("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null");
  }
  if (spec.identityFile) args.push("-i", expandHome(spec.identityFile));
  if (spec.jumpHost) args.push("-o", `ProxyJump=${spec.jumpHost}`);
  args.push(spec.target);
  return { args, localPort };
}

async function startSshTunnel(name, spec) {
  if (!spec || !spec.target) throw new Error(`missing SSH target for ${name}`);
  if (runtime.tunnels.has(name)) return runtime.tunnels.get(name);
  const { args, localPort } = sshArgsFor(spec);
  const child = await startProcess("ssh", args);
  let stderr = "";
  child.stderr.on("data", (chunk) => {
    stderr += chunk.toString("utf8");
    if (stderr.length > 4000) stderr = stderr.slice(-4000);
  });
  let exited = false;
  child.once("exit", () => {
    exited = true;
    runtime.tunnels.delete(name);
  });
  await waitForTcp(DEFAULT_PROXY_HOST, localPort, spec.timeoutMs || 12000).catch((error) => {
    if (!exited) child.kill();
    throw new Error(`${error.message}${stderr ? `: ${stderr.trim()}` : ""}`);
  });
  const tunnel = {
    name,
    child,
    localPort,
    url: `http://${DEFAULT_PROXY_HOST}:${localPort}`,
  };
  runtime.tunnels.set(name, tunnel);
  return tunnel;
}

function proxyRequest(targetBaseUrl, req, res) {
  const target = new URL(req.url || "/", targetBaseUrl);
  const client = target.protocol === "https:" ? https : http;
  const headers = stripHopByHopHeaders(req.headers);
  const token = profileToken();
  if (token && !headers.authorization) headers.authorization = `Bearer ${token}`;

  const upstream = client.request(target, { method: req.method, headers }, (upstreamRes) => {
    res.writeHead(upstreamRes.statusCode || 502, stripHopByHopHeaders(upstreamRes.headers));
    upstreamRes.pipe(res);
  });
  upstream.on("error", (error) => {
    res.writeHead(502, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: `desktop proxy failed: ${error.message}` }));
  });
  req.pipe(upstream);
}

async function startProxyServer(targetBaseUrl, preferredPort) {
  const server = http.createServer((req, res) => proxyRequest(targetBaseUrl, req, res));
  const port = await listenServer(server, preferredPort);
  runtime.proxyServer = server;
  runtime.proxyUrl = `http://${DEFAULT_PROXY_HOST}:${port}`;
  return runtime.proxyUrl;
}

async function prepareConnection() {
  const profile = loadProfile();
  runtime.profile = profile;

  const localUrl = await startLocalServerIfConfigured(profile);
  if (localUrl) profile.apiUrl = localUrl;

  if (profile.ssh) {
    const tunnel = await startSshTunnel("mac-api", profile.ssh);
    runtime.apiTargetUrl = tunnel.url;
  } else {
    runtime.apiTargetUrl = profile.apiUrl || DEFAULT_API_URL;
  }
  await startProxyServer(runtime.apiTargetUrl, profile.proxyPort);
}

function connectionInfo() {
  return {
    mode: "electron-managed",
    apiBaseUrl: runtime.proxyUrl,
    displayName: runtime.profile?.displayName || "MAC Control Plane",
  };
}

async function ipcRequest(_event, payload) {
  const requestPath = payload?.path || "/";
  const init = payload?.init || {};
  const url = new URL(requestPath, runtime.proxyUrl).toString();
  const response = await fetch(url, {
    method: init.method || "GET",
    headers: init.headers || {},
    body: init.body,
  });
  if (!response.ok) {
    let detail = response.statusText;
    try {
      const body = await response.json();
      detail = body.detail || detail;
    } catch {
      detail = await response.text();
    }
    throw new Error(`${response.status} ${detail}`);
  }
  const text = await response.text();
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return text;
  }
}

function serviceTunnelSpec(serviceId) {
  const profile = runtime.profile || {};
  const spec = profile.serviceTunnels?.[serviceId];
  if (!spec) return null;
  return {
    target: spec.target || profile.ssh?.target,
    remoteHost: spec.remoteHost || DEFAULT_REMOTE_HOST,
    remotePort: spec.remotePort,
    localPort: spec.localPort,
    identityFile: expandHome(spec.identityFile || profile.ssh?.identityFile || ""),
    jumpHost: spec.jumpHost || profile.ssh?.jumpHost,
    strictHostKeyChecking: spec.strictHostKeyChecking ?? profile.ssh?.strictHostKeyChecking,
    path: spec.path || "/",
    url: spec.url || "",
  };
}

async function ipcOpenService(_event, payload) {
  const serviceId = payload?.serviceId || "";
  const fallbackUrl = payload?.fallbackUrl || "";
  const spec = serviceTunnelSpec(serviceId);
  if (spec?.url) {
    await shell.openExternal(spec.url);
    return;
  }
  if (spec?.target && spec.remotePort) {
    const tunnel = await startSshTunnel(`service:${serviceId}`, spec);
    await shell.openExternal(`${tunnel.url}${spec.path || "/"}`);
    return;
  }
  if (fallbackUrl) {
    await shell.openExternal(fallbackUrl);
    return;
  }
  throw new Error(`no service route configured for ${serviceId}`);
}

async function createWindow() {
  await prepareConnection();
  ipcMain.handle("mac-dashboard:connection", () => connectionInfo());
  ipcMain.handle("mac-dashboard:request", ipcRequest);
  ipcMain.handle("mac-dashboard:open-service", ipcOpenService);

  const win = new BrowserWindow({
    width: 1440,
    height: 960,
    minWidth: 980,
    minHeight: 720,
    title: "MAC Control Plane",
    webPreferences: {
      preload: path.join(__dirname, "preload.js"),
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
    },
  });
  runtime.windows.add(win);
  win.once("closed", () => runtime.windows.delete(win));
  await win.loadURL(`${runtime.proxyUrl}/ui`);
}

function cleanup() {
  if (runtime.proxyServer) runtime.proxyServer.close();
  for (const child of runtime.children) {
    if (!child.killed) child.kill();
  }
}

app.whenReady().then(createWindow).catch((error) => {
  console.error(error);
  app.exit(1);
});
app.on("activate", () => {
  if (BrowserWindow.getAllWindows().length === 0) createWindow();
});
app.on("window-all-closed", () => {
  if (process.platform !== "darwin") app.quit();
});
app.on("before-quit", cleanup);
