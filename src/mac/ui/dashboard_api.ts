export interface DashboardApi {
  request(path: string, init?: RequestInit): Promise<unknown>;
  stream(path: string, init?: RequestInit): Promise<Response>;
  openService(serviceId: string, fallbackUrl?: string): Promise<void>;
  connection(): DashboardConnection;
}

export interface DashboardConnection {
  mode: "browser-same-origin" | "remote-api" | "electron-managed";
  apiBaseUrl: string;
  displayName: string;
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
  function headersFor(init: RequestInit = {}): Record<string, string> {
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
    const token = tokenProvider();
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
    };
  }

  return {
    async request(path: string, init: RequestInit = {}): Promise<unknown> {
      const headers = headersFor(init);
      const bridge = bridgeProvider();
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
      return fetch(resolvePath(path), { ...init, headers: headersFor(init) });
    },
    async openService(serviceId: string, fallbackUrl = ""): Promise<void> {
      const bridge = bridgeProvider();
      if (bridge?.openService) {
        await bridge.openService(serviceId, fallbackUrl);
        return;
      }
      if (fallbackUrl) window.open(fallbackUrl, "_blank", "noreferrer");
    },
    connection,
  };
}
