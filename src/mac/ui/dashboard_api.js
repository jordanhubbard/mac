export function normalizeApiBaseUrl(raw) {
    const value = String(raw || "").trim();
    if (!value || value === "/")
        return "";
    try {
        const url = new URL(value, window.location.origin);
        if (url.origin === window.location.origin && url.pathname === "/")
            return "";
        return url.toString().replace(/\/+$/, "");
    }
    catch {
        return value.replace(/\/+$/, "");
    }
}
export function createDashboardApi(tokenProvider, apiBaseUrlProvider = () => "", bridgeProvider = () => window.macDashboard) {
    function headersFor(init = {}, includeToken = true) {
        const headers = { Accept: "application/json" };
        if (init.headers instanceof Headers) {
            init.headers.forEach((value, key) => {
                headers[key] = value;
            });
        }
        else if (Array.isArray(init.headers)) {
            for (const [key, value] of init.headers)
                headers[key] = String(value);
        }
        else if (init.headers) {
            Object.assign(headers, init.headers);
        }
        if (init.body && !headers["Content-Type"])
            headers["Content-Type"] = "application/json";
        const token = includeToken ? tokenProvider() : "";
        if (token)
            headers.Authorization = `Bearer ${token}`;
        return headers;
    }
    function resolvePath(path) {
        if (/^https?:\/\//i.test(path))
            return path;
        const baseUrl = normalizeApiBaseUrl(apiBaseUrlProvider());
        if (!baseUrl)
            return path;
        return `${baseUrl}${path.startsWith("/") ? path : `/${path}`}`;
    }
    function connection() {
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
        async request(path, init = {}) {
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
                    const body = (await response.json());
                    detail = body.detail || detail;
                }
                catch {
                    detail = response.statusText;
                }
                throw new Error(`${response.status} ${detail}`);
            }
            return response.json();
        },
        async stream(path, init = {}) {
            const bridge = bridgeProvider();
            return fetch(resolvePath(path), { ...init, headers: headersFor(init, !bridge) });
        },
        async openService(serviceId, fallbackUrl = "") {
            const bridge = bridgeProvider();
            if (bridge?.openService) {
                await bridge.openService(serviceId, fallbackUrl);
                return;
            }
            if (fallbackUrl)
                window.open(fallbackUrl, "_blank", "noreferrer");
        },
        async targets() {
            const bridge = bridgeProvider();
            if (!bridge?.targets)
                return [];
            return bridge.targets();
        },
        async selectTarget(targetId, options = {}) {
            const bridge = bridgeProvider();
            if (!bridge?.selectTarget)
                return connection();
            const selected = await bridge.selectTarget(targetId, options);
            return {
                ...connection(),
                ...(selected || {}),
                mode: "electron-managed",
            };
        },
        async disconnect() {
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
