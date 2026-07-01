const { app, BrowserWindow, ipcMain, shell } = require("electron");
const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const http = require("node:http");
const https = require("node:https");
const net = require("node:net");
const path = require("node:path");
const yaml = require("js-yaml");

const DEFAULT_API_URL = "http://127.0.0.1:8789";
const DEFAULT_API_TUNNEL_PORT = 18789;
const DEFAULT_PROXY_HOST = "127.0.0.1";
const DEFAULT_REMOTE_HOST = "127.0.0.1";
const DEFAULT_REMOTE_PORT = 8789;
const DEFAULT_FLEETS_CONFIG = "~/.mac/fleets.yaml";
const DEFAULT_ENV_FILE = "~/.mac/.env";
const DEFAULT_CLIENTS_DIR = "~/.mac/clients";
const TESTING_TARGET_ID = "testing-url";
const UI_ASSET_PREFIX = "/ui/assets/";
const UI_CONTENT_TYPES = {
  ".css": "text/css; charset=utf-8",
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".map": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
};

const runtime = {
  profile: null,
  targets: [],
  activeTargetId: "",
  apiTargetUrl: "",
  connected: false,
  proxyUrl: "",
  proxyServer: null,
  uiRoot: "",
  windows: new Set(),
  children: new Set(),
  tunnels: new Map(),
  ipcRegistered: false,
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
  const abs = path.resolve(expandHome(filePath));
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

function optionalNumberValue(value, fallback = 0) {
  const parsed = Number(value);
  return Number.isFinite(parsed) && parsed >= 0 ? Math.floor(parsed) : fallback;
}

function stringValue(value) {
  return typeof value === "string" ? value.trim() : "";
}

function normalizeEnvSuffix(value) {
  return stringValue(value).replace(/[^A-Za-z0-9]+/g, "_").replace(/^_+|_+$/g, "").toUpperCase();
}

function unquoteEnvValue(value) {
  const trimmed = String(value || "").trim();
  if (!trimmed) return "";
  const first = trimmed[0];
  const last = trimmed[trimmed.length - 1];
  if ((first === "\"" && last === "\"") || (first === "'" && last === "'")) {
    return trimmed.slice(1, -1).replace(/\\n/g, "\n").replace(/\\"/g, "\"").replace(/\\\\/g, "\\");
  }
  return trimmed;
}

function readEnvFile(filePath) {
  const abs = expandHome(filePath || DEFAULT_ENV_FILE);
  if (!abs || !fs.existsSync(abs)) return {};
  const env = {};
  const lines = fs.readFileSync(abs, "utf8").split(/\r?\n/);
  for (const line of lines) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const match = /^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)=(.*)$/.exec(trimmed);
    if (!match) continue;
    env[match[1]] = unquoteEnvValue(match[2]);
  }
  return env;
}

function readYamlFile(filePath) {
  const abs = expandHome(filePath);
  if (!abs || !fs.existsSync(abs)) return null;
  return yaml.load(fs.readFileSync(abs, "utf8")) || null;
}

function loadMacEnv(args) {
  const envFile = args["env-file"] || process.env.MAC_DESKTOP_ENV_FILE || DEFAULT_ENV_FILE;
  return { ...readEnvFile(envFile), ...process.env };
}

function hasManualConnectionConfig(args) {
  return !!(
    args.profile
    || process.env.MAC_DESKTOP_PROFILE
    || args["api-url"]
    || process.env.MAC_DESKTOP_API_URL
    || args.ssh
    || args["ssh-target"]
    || process.env.MAC_DESKTOP_SSH_TARGET
  );
}

function cloneProfile(profile) {
  return JSON.parse(JSON.stringify(profile || {}));
}

function loadProfile(options = {}) {
  const args = parseArgs(process.argv.slice(1));
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

  if (options.apiUrl) {
    profile.apiUrl = options.apiUrl;
    delete profile.ssh;
    delete profile.localServer;
  }
  if (options.allowDefault !== false && !profile.apiUrl && !profile.ssh) profile.apiUrl = DEFAULT_API_URL;
  return profile;
}

function tokenLabel(key) {
  const suffix = key.includes("__") ? key.split("__").slice(1).join("__") : "";
  if (key.startsWith("MAC_DEPLOY_HUB_TOKEN__")) return `Hub token (${suffix})`;
  if (key.startsWith("MAC_API_TOKEN__")) return `API token (${suffix})`;
  if (key === "MAC_DEPLOY_HUB_TOKEN") return "Hub token";
  if (key === "MAC_API_TOKEN") return "API token";
  if (key === "MAC_DESKTOP_API_TOKEN") return "Desktop token";
  return key;
}

function tokenSourcesForTarget(targetKey, fleet, env) {
  const suffixes = [
    targetKey,
    fleet?.fleet_name,
    fleet?.hub_agent,
  ].map(normalizeEnvSuffix).filter(Boolean);
  const candidates = [];
  for (const suffix of [...new Set(suffixes)]) {
    candidates.push(`MAC_DEPLOY_HUB_TOKEN__${suffix}`, `MAC_API_TOKEN__${suffix}`);
  }
  candidates.push("MAC_DEPLOY_HUB_TOKEN", "MAC_API_TOKEN", "MAC_DESKTOP_API_TOKEN");
  const sources = [];
  for (const key of candidates) {
    if (!env[key] || sources.some((source) => source.envKey === key)) continue;
    sources.push({
      id: `env:${key}`,
      label: tokenLabel(key),
      envKey: key,
      value: env[key],
    });
  }
  sources.push({ id: "manual", label: "Manual bearer token", envKey: "", value: "" });
  sources.push({ id: "none", label: "No token", envKey: "", value: "" });
  return sources;
}

function defaultTokenSource(tokenSources) {
  return tokenSources.find((source) => source.id.startsWith("env:")) || tokenSources[0] || null;
}

function fleetHubAgent(fleet) {
  const agents = Array.isArray(fleet?.agents) ? fleet.agents : [];
  const hubAgent = stringValue(fleet?.hub_agent);
  return agents.find((agent) => stringValue(agent?.name) === hubAgent)
    || agents.find((agent) => agent?.enabled !== false)
    || null;
}

function hostFromUrl(rawUrl) {
  try {
    return new URL(rawUrl).hostname;
  } catch {
    return "";
  }
}

function fleetNeedsSsh(fleet) {
  const defaults = fleet?.defaults || {};
  const host = hostFromUrl(fleet?.hub_url || "");
  return !!(
    defaults.ssh_jump
    || defaults.network?.provider === "none"
    || host.endsWith(".svc")
    || host.endsWith(".svc.cluster.local")
  );
}

function canonicalSshSpec(fleetKey, fleet, hubAgent, configPath) {
  const agentName = stringValue(hubAgent?.name) || stringValue(fleet?.hub_agent);
  const cli = process.env.MAC_CLI_BIN || "mac";
  let resolved;
  try {
    const raw = execFileSync(cli, [
      "--json",
      "fleet",
      "ssh-spec",
      "--fleet",
      fleetKey,
      "--agent",
      agentName,
      "--fleets-config",
      expandHome(configPath),
    ], {
      encoding: "utf8",
      env: { ...process.env, MAC_FLEETS_CONFIG: expandHome(configPath) },
      stdio: ["ignore", "pipe", "pipe"],
    });
    resolved = JSON.parse(raw);
  } catch (error) {
    const detail = error?.stderr?.toString?.("utf8")?.trim() || error.message;
    throw new Error(`could not resolve canonical SSH route for ${fleetKey}/${agentName}: ${detail}`);
  }
  const controlPort = numberValue(fleet?.control_port, DEFAULT_REMOTE_PORT);
  return {
    target: stringValue(resolved.target),
    port: optionalNumberValue(resolved.port, 0),
    localPort: 0,
    remoteHost: DEFAULT_REMOTE_HOST,
    remotePort: controlPort,
    identityFile: expandHome(resolved.identity_file || ""),
    identityRef: stringValue(resolved.identity_ref),
    jumpHost: stringValue(resolved.proxy_jump),
    knownHostsFile: expandHome(resolved.known_hosts_file || ""),
    hostKeyPolicy: stringValue(resolved.host_key_policy) || "strict",
    hostKeyFingerprint: stringValue(resolved.host_key_fingerprint),
    hostCa: stringValue(resolved.host_ca),
  };
}

function serviceTunnel(service, base, fallbackPort, servicePath) {
  const port = numberValue(service?.port, fallbackPort);
  if (!base.target || !port) return null;
  return {
    ...base,
    localPort: 0,
    remotePort: port,
    path: servicePath,
  };
}

function serviceTunnelsForFleet(fleet, base) {
  const defaults = fleet?.defaults || {};
  const tunnels = {};
  const qdrant = serviceTunnel(defaults.qdrant, base, 6333, "/dashboard");
  const firecrawl = serviceTunnel(defaults.firecrawl, base, 3002, "/");
  if (qdrant) tunnels.qdrant = qdrant;
  if (firecrawl) tunnels.firecrawl = firecrawl;
  return tunnels;
}

function targetFromFleet(key, fleet, configPath, env) {
  const hubAgent = fleetHubAgent(fleet);
  const hubUrl = stringValue(fleet?.hub_url);
  const fleetName = stringValue(fleet?.fleet_name) || key;
  const hubAgentName = stringValue(fleet?.hub_agent) || stringValue(hubAgent?.name);
  const displayName = hubAgentName && hubAgentName !== fleetName ? `${fleetName} / ${hubAgentName}` : fleetName;
  const tokenChoices = tokenSourcesForTarget(key, fleet, env);
  const defaultSource = defaultTokenSource(tokenChoices);
  const profile = {
    displayName,
    apiUrl: "",
    token: defaultSource?.value || "",
    tokenSourceId: defaultSource?.id || "none",
    tokenChoices,
    tokenEnv: "MAC_DESKTOP_API_TOKEN",
    proxyPort: 0,
    serviceTunnels: {},
  };
  const sshBase = stringValue(hubAgent?.target)
    ? canonicalSshSpec(key, fleet, hubAgent, configPath)
    : { target: "" };
  const useSsh = fleetNeedsSsh(fleet) || !hubUrl;
  if (useSsh && sshBase.target) {
    profile.ssh = sshBase;
    profile.serviceTunnels = serviceTunnelsForFleet(fleet, sshBase);
  } else if (hubUrl) {
    profile.apiUrl = hubUrl;
  } else if (sshBase.target) {
    profile.ssh = sshBase;
    profile.serviceTunnels = serviceTunnelsForFleet(fleet, sshBase);
  } else {
    return null;
  }

  return {
    id: `fleet:${key}`,
    label: displayName,
    mode: profile.ssh ? "fleet-ssh" : "fleet-direct",
    apiUrl: profile.ssh ? "" : profile.apiUrl,
    fleetName,
    hubAgent: hubAgentName,
    source: expandHome(configPath),
    profile,
  };
}

function loadFleetTargets(args, env) {
  const configuredPath = args["fleets-config"]
    || process.env.MAC_DESKTOP_FLEETS_CONFIG
    || (process.env.MAC_DEPLOY_FLEETS_CONFIG && fs.existsSync(expandHome(process.env.MAC_DEPLOY_FLEETS_CONFIG))
      ? process.env.MAC_DEPLOY_FLEETS_CONFIG
      : "")
    || DEFAULT_FLEETS_CONFIG;
  const config = readYamlFile(configuredPath);
  const fleets = config?.fleets && typeof config.fleets === "object" ? config.fleets : {};
  const targets = [];
  for (const [key, fleet] of Object.entries(fleets)) {
    if (fleet?.sample === true) continue;
    const target = targetFromFleet(key, fleet, configuredPath, env);
    if (target) targets.push(target);
  }
  return targets;
}

function loadSecureClientTargets(args) {
  const configuredRoot = path.resolve(expandHome(
    args["clients-dir"] || process.env.MAC_CLIENT_PROFILES_DIR || DEFAULT_CLIENTS_DIR,
  ));
  if (!fs.existsSync(configuredRoot)) return [];
  const root = fs.realpathSync(configuredRoot);
  const rootMode = fs.statSync(root).mode & 0o777;
  if ((rootMode & 0o077) !== 0) throw new Error(`client profile directory ${root} must be mode 0700`);
  let active = "";
  const current = path.join(root, "current");
  if (fs.existsSync(current)) {
    const currentMode = fs.statSync(current).mode & 0o777;
    if ((currentMode & 0o077) !== 0) throw new Error(`active client profile ${current} must be mode 0600`);
    active = fs.readFileSync(current, "utf8").trim();
  }
  const configuredCredentialRoot = path.resolve(
    process.env.MAC_CLIENT_CREDENTIALS_DIR
      ? expandHome(process.env.MAC_CLIENT_CREDENTIALS_DIR)
      : path.join(path.dirname(root), "credentials", "clients"),
  );
  const credentialRoot = fs.realpathSync(configuredCredentialRoot);
  const targets = [];
  for (const filename of fs.readdirSync(root).filter((name) => name.endsWith(".yaml"))) {
    const profilePath = fs.realpathSync(path.join(root, filename));
    if (!(profilePath === root || profilePath.startsWith(`${root}${path.sep}`))) {
      throw new Error(`client profile ${filename} escapes the client profile directory`);
    }
    const profileMode = fs.statSync(profilePath).mode & 0o777;
    if ((profileMode & 0o077) !== 0) throw new Error(`client profile ${profilePath} must be mode 0600`);
    const document = yaml.load(fs.readFileSync(profilePath, "utf8")) || {};
    if (document.schema !== "mac.client_profile.v1") continue;
    const profileName = stringValue(document.profile);
    const credentialRef = stringValue(document.credential?.path);
    const unresolvedCredentialPath = path.resolve(root, credentialRef);
    const credentialPath = credentialRef && fs.existsSync(unresolvedCredentialPath)
      ? fs.realpathSync(unresolvedCredentialPath)
      : unresolvedCredentialPath;
    if (!credentialRef || !(credentialPath === credentialRoot || credentialPath.startsWith(`${credentialRoot}${path.sep}`))) {
      throw new Error(`client profile ${profileName} has an invalid credential reference`);
    }
    const mode = fs.statSync(credentialPath).mode & 0o777;
    if ((mode & 0o077) !== 0) throw new Error(`client credential ${credentialPath} must be mode 0600`);
    const token = fs.readFileSync(credentialPath, "utf8").trim();
    const connection = document.connection || {};
    const ssh = document.ssh || {};
    const runtimeProfile = {
      displayName: stringValue(document.display_name) || profileName,
      apiUrl: stringValue(connection.api_url),
      token,
      tokenSourceId: `client-profile:${profileName}`,
      tokenChoices: [],
      tokenEnv: "MAC_DESKTOP_API_TOKEN",
      proxyPort: 0,
      serviceTunnels: {},
    };
    if (connection.mode === "ssh-tunnel") {
      runtimeProfile.ssh = {
        target: stringValue(ssh.target),
        port: optionalNumberValue(ssh.port, 0),
        localPort: optionalNumberValue(connection.local_port, 0),
        remoteHost: stringValue(connection.remote_host) || DEFAULT_REMOTE_HOST,
        remotePort: numberValue(connection.remote_port, DEFAULT_REMOTE_PORT),
        identityFile: expandHome(ssh.identity_file || ""),
        identityRef: stringValue(ssh.identity_ref),
        jumpHost: stringValue(ssh.proxy_jump),
        knownHostsFile: expandHome(ssh.known_hosts_file || ""),
        hostKeyPolicy: stringValue(ssh.host_key_policy) || "strict",
        hostKeyFingerprint: stringValue(ssh.host_key_fingerprint),
        hostCa: stringValue(ssh.host_ca),
      };
    }
    targets.push({
      id: `client:${profileName}`,
      label: runtimeProfile.displayName,
      mode: runtimeProfile.ssh ? "client-ssh" : "client-direct",
      apiUrl: runtimeProfile.ssh ? "" : runtimeProfile.apiUrl,
      fleetName: stringValue(document.fleet),
      hubAgent: "",
      source: profilePath,
      active: profileName === active,
      profile: runtimeProfile,
    });
  }
  targets.sort((left, right) => Number(right.active) - Number(left.active));
  return targets;
}

function testingTarget(env = {}) {
  const profile = loadProfile({ allowDefault: true });
  const tokenChoices = tokenSourcesForTarget("testing", {}, env);
  const defaultSource = defaultTokenSource(tokenChoices);
  if (!profile.token) profile.token = defaultSource?.value || "";
  profile.tokenChoices = tokenChoices;
  profile.tokenSourceId = defaultSource?.id || "none";
  const mode = profile.localServer ? "testing-local" : profile.ssh ? "testing-ssh" : "testing-url";
  return {
    id: TESTING_TARGET_ID,
    label: "Testing URL",
    mode,
    apiUrl: profile.apiUrl || DEFAULT_API_URL,
    fleetName: "",
    hubAgent: "",
    source: "",
    profile,
  };
}

function loadTargets() {
  const args = parseArgs(process.argv.slice(1));
  const env = loadMacEnv(args);
  const targets = [
    ...loadSecureClientTargets(args),
    ...loadFleetTargets(args, env),
  ];
  targets.push(testingTarget(env));
  return targets;
}

function targetPublicInfo(target) {
  return {
    id: target.id,
    label: target.label,
    mode: target.mode,
    apiUrl: ["fleet-direct", "client-direct"].includes(target.mode) || target.id === TESTING_TARGET_ID
      ? target.apiUrl || ""
      : "",
    fleetName: target.fleetName || "",
    hubAgent: target.hubAgent || "",
    source: target.source || "",
    tokenSources: (target.profile?.tokenChoices || []).map((source) => ({
      id: source.id,
      label: source.label,
      envKey: source.envKey || "",
    })),
    selectedTokenSourceId: target.profile?.tokenSourceId || "",
  };
}

function resolveTargetId(requestedId = "") {
  if (!runtime.targets.length) runtime.targets = loadTargets();
  if (requestedId && runtime.targets.some((target) => target.id === requestedId)) return requestedId;
  if (requestedId) {
    const fleetId = requestedId.startsWith("fleet:") ? requestedId : `fleet:${requestedId}`;
    if (runtime.targets.some((target) => target.id === fleetId)) return fleetId;
  }
  const args = parseArgs(process.argv.slice(1));
  const explicit = args.target || args.fleet || process.env.MAC_DESKTOP_TARGET || process.env.MAC_DESKTOP_FLEET || "";
  if (explicit) {
    const explicitId = explicit.startsWith("fleet:") || explicit === TESTING_TARGET_ID ? explicit : `fleet:${explicit}`;
    if (runtime.targets.some((target) => target.id === explicitId)) return explicitId;
  }
  if (hasManualConnectionConfig(args)) return TESTING_TARGET_ID;
  return runtime.targets.find((target) => target.id !== TESTING_TARGET_ID)?.id || TESTING_TARGET_ID;
}

function profileForTarget(target, options = {}) {
  const profile = cloneProfile(target?.profile || {});
  if (target?.id === TESTING_TARGET_ID && options.apiUrl) {
    profile.displayName = "Testing URL";
    profile.apiUrl = options.apiUrl;
    delete profile.ssh;
    delete profile.localServer;
  }
  if (!profile.displayName) profile.displayName = target?.label || "MAC Control Plane";
  if (!profile.serviceTunnels) profile.serviceTunnels = {};
  const tokenChoices = target?.profile?.tokenChoices || profile.tokenChoices || [];
  const requestedSourceId = stringValue(options.tokenSourceId);
  let selectedSource = requestedSourceId
    ? tokenChoices.find((source) => source.id === requestedSourceId) || null
    : tokenChoices.find((source) => source.id === profile.tokenSourceId) || defaultTokenSource(tokenChoices);
  if (requestedSourceId === "manual") selectedSource = tokenChoices.find((source) => source.id === "manual") || null;
  if (requestedSourceId === "none") selectedSource = tokenChoices.find((source) => source.id === "none") || null;
  if (selectedSource?.id === "manual") {
    profile.token = stringValue(options.token);
    profile.tokenSourceId = "manual";
  } else if (selectedSource?.id === "none") {
    profile.token = "";
    profile.tokenSourceId = "none";
  } else if (selectedSource) {
    profile.token = selectedSource.value || "";
    profile.tokenSourceId = selectedSource.id;
  }
  return profile;
}

function profileToken() {
  const profile = runtime.profile || {};
  return profile.token || process.env[profile.tokenEnv || "MAC_DESKTOP_API_TOKEN"] || "";
}

function uiRootCandidates() {
  return [
    process.env.MAC_DESKTOP_UI_ROOT || "",
    process.resourcesPath ? path.join(process.resourcesPath, "ui") : "",
    path.resolve(__dirname, "..", "src", "mac", "ui"),
    path.resolve(process.cwd(), "..", "src", "mac", "ui"),
  ].filter(Boolean);
}

function localUiRoot() {
  if (runtime.uiRoot && fs.existsSync(path.join(runtime.uiRoot, "index.html"))) return runtime.uiRoot;
  for (const candidate of uiRootCandidates()) {
    const abs = path.resolve(expandHome(candidate));
    if (fs.existsSync(path.join(abs, "index.html"))) {
      runtime.uiRoot = abs;
      return abs;
    }
  }
  return "";
}

function uiFileForRequest(reqUrl) {
  const root = localUiRoot();
  if (!root) return "";
  const parsed = new URL(reqUrl || "/", "http://desktop.local");
  let name = "";
  if (parsed.pathname === "/ui" || parsed.pathname === "/ui/" || parsed.pathname === "/ui/index.html") {
    name = "index.html";
  } else if (parsed.pathname.startsWith(UI_ASSET_PREFIX)) {
    const decoded = decodeURIComponent(parsed.pathname.slice(UI_ASSET_PREFIX.length));
    if (!decoded || decoded !== path.basename(decoded)) return "";
    name = decoded;
  } else {
    return "";
  }
  const filePath = path.resolve(root, name);
  if (filePath !== path.join(root, name)) return "";
  return filePath;
}

function serveLocalUi(req, res) {
  const filePath = uiFileForRequest(req.url || "/");
  if (!filePath) return false;
  fs.readFile(filePath, (error, body) => {
    if (error) {
      res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" });
      res.end("Not found");
      return;
    }
    const contentType = UI_CONTENT_TYPES[path.extname(filePath)] || "application/octet-stream";
    res.writeHead(200, { "Content-Type": contentType });
    res.end(body);
  });
  return true;
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

function allocateTcpPort() {
  const server = net.createServer();
  return listenServer(server, 0).then((port) => new Promise((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve(port)));
  }));
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

async function sshArgsFor(spec) {
  const remoteHost = spec.remoteHost || DEFAULT_REMOTE_HOST;
  const remotePort = numberValue(spec.remotePort, DEFAULT_REMOTE_PORT);
  const localPort = optionalNumberValue(spec.localPort, 0) || await allocateTcpPort();
  const args = [
    "-N",
    "-F", "/dev/null",
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=10",
    "-o", "ServerAliveInterval=30",
    "-o", "ServerAliveCountMax=3",
    "-o", "ExitOnForwardFailure=yes",
    "-L", `${DEFAULT_PROXY_HOST}:${localPort}:${remoteHost}:${remotePort}`,
  ];
  const hostKeyPolicy = spec.hostKeyPolicy || "strict";
  if (hostKeyPolicy === "insecure") {
    args.push("-o", "StrictHostKeyChecking=no", "-o", "UserKnownHostsFile=/dev/null");
  } else if (hostKeyPolicy === "accept-new") {
    args.push("-o", "StrictHostKeyChecking=accept-new");
  } else {
    args.push("-o", "StrictHostKeyChecking=yes");
  }
  if (spec.knownHostsFile) args.push("-o", `UserKnownHostsFile=${expandHome(spec.knownHostsFile)}`);
  if (spec.identityFile) args.push("-o", "IdentitiesOnly=yes", "-i", expandHome(spec.identityFile));
  if (spec.identityRef && !spec.identityFile && !spec.identityRef.startsWith("file:")) {
    throw new Error(`SSH identity reference ${spec.identityRef} is not available to OpenSSH`);
  }
  if (spec.jumpHost) args.push("-o", `ProxyJump=${spec.jumpHost}`);
  if (spec.port) args.push("-p", String(spec.port));
  args.push(spec.target);
  return { args, localPort };
}

async function startSshTunnel(name, spec) {
  if (!spec || !spec.target) throw new Error(`missing SSH target for ${name}`);
  if (runtime.tunnels.has(name)) return runtime.tunnels.get(name);
  const { args, localPort } = await sshArgsFor(spec);
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
  if (!runtime.connected || !targetBaseUrl) {
    res.writeHead(503, { "Content-Type": "application/json" });
    res.end(JSON.stringify({ detail: "desktop target is disconnected" }));
    return;
  }
  const target = new URL(req.url || "/", targetBaseUrl);
  const client = target.protocol === "https:" ? https : http;
  const headers = stripHopByHopHeaders(req.headers);
  const token = profileToken();
  if (token && !headers.authorization) headers.authorization = `Bearer ${token}`;

  let upstreamResponded = false;
  const upstream = client.request(target, { method: req.method, headers }, (upstreamRes) => {
    upstreamResponded = true;
    if (res.destroyed || res.writableEnded) {
      upstreamRes.resume();
      return;
    }
    if (!res.headersSent) {
      res.writeHead(upstreamRes.statusCode || 502, stripHopByHopHeaders(upstreamRes.headers));
    }
    upstreamRes.on("error", (error) => closeProxyResponse(res, error));
    upstreamRes.pipe(res);
  });
  upstream.on("error", (error) => {
    if (upstreamResponded || res.headersSent) {
      closeProxyResponse(res, error);
      return;
    }
    writeProxyError(res, 502, `desktop proxy failed: ${error.message}`);
  });
  req.on("error", (error) => {
    upstream.destroy(error);
  });
  req.pipe(upstream);
}

function writeProxyError(res, statusCode, detail) {
  if (res.destroyed || res.writableEnded) return;
  if (res.headersSent) {
    closeProxyResponse(res);
    return;
  }
  res.writeHead(statusCode, { "Content-Type": "application/json" });
  res.end(JSON.stringify({ detail }));
}

function closeProxyResponse(res, error) {
  if (res.destroyed || res.writableEnded) return;
  if (res.headersSent) {
    res.destroy();
    return;
  }
  res.end();
}

async function startProxyServer(preferredPort) {
  if (runtime.proxyServer) return runtime.proxyUrl;
  const server = http.createServer((req, res) => {
    if (serveLocalUi(req, res)) return;
    proxyRequest(runtime.apiTargetUrl || DEFAULT_API_URL, req, res);
  });
  const port = await listenServer(server, preferredPort);
  runtime.proxyServer = server;
  runtime.proxyUrl = `http://${DEFAULT_PROXY_HOST}:${port}`;
  return runtime.proxyUrl;
}

function stopConnectionChildren() {
  for (const child of Array.from(runtime.children)) {
    if (!child.killed) child.kill();
  }
  runtime.children.clear();
  runtime.tunnels.clear();
}

async function prepareConnection(targetId = "", options = {}) {
  if (!runtime.targets.length) runtime.targets = loadTargets();
  const resolvedTargetId = resolveTargetId(targetId);
  const target = runtime.targets.find((item) => item.id === resolvedTargetId) || runtime.targets[0] || testingTarget();
  if (runtime.proxyUrl && runtime.activeTargetId === target.id && !options.apiUrl) return;
  const profile = profileForTarget(target, options);
  stopConnectionChildren();
  runtime.profile = profile;
  runtime.activeTargetId = target.id;
  runtime.connected = false;

  const localUrl = await startLocalServerIfConfigured(profile);
  if (localUrl) profile.apiUrl = localUrl;

  if (profile.ssh) {
    const tunnel = await startSshTunnel("mac-api", profile.ssh);
    runtime.apiTargetUrl = tunnel.url;
  } else {
    runtime.apiTargetUrl = profile.apiUrl || DEFAULT_API_URL;
  }
  await startProxyServer(profile.proxyPort);
  runtime.connected = true;
}

function disconnectRuntime() {
  stopConnectionChildren();
  runtime.profile = null;
  runtime.activeTargetId = "";
  runtime.apiTargetUrl = "";
  runtime.connected = false;
}

function connectionInfo() {
  return {
    mode: "electron-managed",
    apiBaseUrl: runtime.proxyUrl,
    displayName: runtime.profile?.displayName || "Not connected",
    targetId: runtime.activeTargetId || "",
    tokenSourceId: runtime.profile?.tokenSourceId || "",
    connected: runtime.connected,
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
    identityRef: spec.identityRef || profile.ssh?.identityRef,
    jumpHost: spec.jumpHost || profile.ssh?.jumpHost,
    knownHostsFile: expandHome(spec.knownHostsFile || profile.ssh?.knownHostsFile || ""),
    hostKeyPolicy: spec.hostKeyPolicy || profile.ssh?.hostKeyPolicy,
    port: spec.port || profile.ssh?.port,
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

function ipcTargets() {
  if (!runtime.targets.length) runtime.targets = loadTargets();
  return runtime.targets.map(targetPublicInfo);
}

async function ipcSelectTarget(_event, payload) {
  const targetId = stringValue(payload?.targetId);
  const apiUrl = normalizeApiUrlOption(payload?.apiUrl);
  const tokenSourceId = stringValue(payload?.tokenSourceId);
  const token = stringValue(payload?.token);
  await prepareConnection(targetId, {
    ...(apiUrl ? { apiUrl } : {}),
    ...(tokenSourceId ? { tokenSourceId, token } : {}),
  });
  return connectionInfo();
}

async function ipcDisconnect() {
  disconnectRuntime();
  return connectionInfo();
}

function normalizeApiUrlOption(raw) {
  const value = stringValue(raw);
  if (!value) return "";
  try {
    return new URL(value).toString().replace(/\/+$/, "");
  } catch {
    return value.replace(/\/+$/, "");
  }
}

function registerIpcHandlers() {
  if (runtime.ipcRegistered) return;
  ipcMain.handle("mac-dashboard:connection", () => connectionInfo());
  ipcMain.handle("mac-dashboard:request", ipcRequest);
  ipcMain.handle("mac-dashboard:open-service", ipcOpenService);
  ipcMain.handle("mac-dashboard:targets", ipcTargets);
  ipcMain.handle("mac-dashboard:select-target", ipcSelectTarget);
  ipcMain.handle("mac-dashboard:disconnect", ipcDisconnect);
  runtime.ipcRegistered = true;
}

async function createWindow() {
  await prepareConnection();
  registerIpcHandlers();

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
