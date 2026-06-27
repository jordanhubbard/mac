// =============================================================================
// DEPRECATED — MAINTENANCE-ONLY MODULE
// =============================================================================
// src/mac/ui (this file and its siblings) is in MAINTENANCE-ONLY mode.
// No new features should be added here. Critical bug fixes only.
//
// The canonical Fleet UI is the Fleet IDE located in ide/.
// All new browser and desktop UI work must target ide/ instead.
//
// See docs/adr/0010-fleet-ide-cutover-parity-matrix.md for the cut-over plan
// and parity matrix.
// =============================================================================

export interface DashboardApi {
  request(path: string, init?: RequestInit): Promise<unknown>;
  stream(path: string, init?: RequestInit): Promise<Response>;
  openService(serviceId: string, fallbackUrl?: string): Promise<void>;
  targets(): Promise<DashboardTarget[]>;
  selectTarget(targetId: string, options?: DashboardTargetSelection): Promise<DashboardConnection>;
  disconnect(): Promise<DashboardConnection>;
  connection(): DashboardConnection;
}

export interface DashboardConnection {
  mode: "browser-same-origin" | "remote-api" | "electron-managed";
  apiBaseUrl: string;
  displayName: string;
  targetId?: string;
  tokenSourceId?: string;
  connected?: boolean;
}

export interface DashboardTarget {
  id: string;
  label: string;
  mode: "fleet-direct" | "fleet-ssh" | "testing-url" | "testing-ssh" | "testing-local";
  apiUrl?: string;
  fleetName?: string;
  hubAgent?: string;
  source?: string;
  tokenSources?: DashboardTokenSource[];
  selectedTokenSourceId?: string;
}

export interface DashboardTokenSource {
  id: string;
  label: string;
  envKey?: string;
}

export interface DashboardTargetSelection {
  apiUrl?: string;
  tokenSourceId?: string;
  token?: string;
}

interface DashboardBridgeRequest {
  method?: string;
  headers?: Record<string, string>;
  body?: string;
}

export interface DashboardElectronBridge {
  request?(path: string, init?: DashboardBridgeRequest): Promise<unknown>;
  openService?(serviceId: string, fallbackUrl?: string): Promise<void>;
  connection?(): Promise<Partial<DashboardConnection> | null>;
  targets?(): Promise<DashboardTarget[]>;
  selectTarget?(targetId: string, options?: DashboardTargetSelection): Promise<Partial<DashboardConnection> | null>;
  disconnect?(): Promise<Partial<DashboardConnection> | null>;
}

declare global {
  interface Window {
    macDashboard?: DashboardElectronBridge;
    MAC_DASHBOARD_CONFIG?: Partial<DashboardConnection>;
  }
}

export function normalizeApiBaseUrl(raw: string | undefined | null): string {
  const value = String(raw || "").trim();
  if (!value || value === "/") return "";
  try {
    const url = new URL(value, window.location.origin);
    if (url.origin === window.location.origin && url.pathname === "/") return "";
    return url.toString().replace(/\/+$/, "");
  } catch {
    return value.replace(/\/+$/, "");
  }
}

export function createDashboardApi(
  tokenProvider: () => string,
  apiBaseUrlProvider: () => string = () => "",
  bridgeProvider: () => DashboardElectronBridge | undefined = () => window.macDashboard,
): DashboardApi {
  function headersFor(init: RequestInit = {}, includeToken = true): Record<string, string> {
    const headers: Record<string, string> = { Accept: "application/json" };
    if (init.headers instanceof Headers) {
      init.headers.forEach((value, key) => {
        headers[key] = value;
      });
    } else if (Array.isArray(init.headers)) {
      for (const [key, value] of init.headers) headers[key] = String(value);
    } else if (init.headers) {
      Object.assign(headers, init.headers as Record<string, string>);
    }
    if (init.body && !headers["Content-Type"]) headers["Content-Type"] = "application/json";
    const token = includeToken ? tokenProvider() : "";
    if (token) headers.Authorization = `Bearer ${token}`;
    return headers;
  }

  function resolvePath(path: string): string {
    if (/^https?:\/\//i.test(path)) return path;
    const baseUrl = normalizeApiBaseUrl(apiBaseUrlProvider());
    if (!baseUrl) return path;
    return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
  }

  function connection(): DashboardConnection {
    const apiBaseUrl = normalizeApiBaseUrl(apiBaseUrlProvider());
    const config = window.MAC_DASHBOARD_CONFIG || {};
    const bridge = bridgeProvider();
    const mode = bridge
      ? "electron-managed"
      : apiBaseUrl
        ? "remote-api"
        : "browser-same-origin";
      return {
        mode,
        apiBaseUrl,
        displayName: config.displayName || (apiBaseUrl || "This MAC server"),
        connected: false,
      };
    }

  return {
    async request(path: string, init: RequestInit = {}): Promise<unknown> {
      const bridge = bridgeProvider();
      const headers = headersFor(init, !bridge?.request);
      if (bridge?.request) {
        return bridge.request(path, {
          method: init.method || "GET",
          headers,
          body: typeof init.body === "string" ? init.body : undefined,
        });
      }
      const response = await fetch(resolvePath(path), { ...init, headers });
      if (!response.ok) {
        let detail = response.statusText;
        try {
          const body = (await response.json()) as { detail?: string };
          detail = body.detail || detail;
        } catch {
          detail = response.statusText;
        }
        throw new Error(`${response.status} ${detail}`);
      }
      return response.json();
    },
    async stream(path: string, init: RequestInit = {}): Promise<Response> {
      const bridge = bridgeProvider();
      return fetch(resolvePath(path), { ...init, headers: headersFor(init, !bridge) });
    },
    async openService(serviceId: string, fallbackUrl = ""): Promise<void> {
      const bridge = bridgeProvider();
      if (bridge?.openService) {
        await bridge.openService(serviceId, fallbackUrl);
        return;
      }
      if (fallbackUrl) window.open(fallbackUrl, "_blank", "noreferrer");
    },
    async targets(): Promise<DashboardTarget[]> {
      const bridge = bridgeProvider();
      if (!bridge?.targets) return [];
      return bridge.targets();
    },
    async selectTarget(targetId: string, options: DashboardTargetSelection = {}): Promise<DashboardConnection> {
      const bridge = bridgeProvider();
      if (!bridge?.selectTarget) return connection();
      const selected = await bridge.selectTarget(targetId, options);
      return {
        ...connection(),
        ...(selected || {}),
        mode: "electron-managed",
      };
    },
    async disconnect(): Promise<DashboardConnection> {
      const bridge = bridgeProvider();
      if (bridge?.disconnect) {
        const disconnected = await bridge.disconnect();
        return {
          ...connection(),
          ...(disconnected || {}),
          connected: false,
        };
      }
      return { ...connection(), connected: false };
    },
    connection,
  };
}
