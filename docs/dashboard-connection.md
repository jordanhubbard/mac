# Dashboard Connection Contract

The shipped observability console is read-only. The optional Fleet Workbench
prototype and Electron shell are separate clients; packaging a desktop shell
does not make its entire bridge an implemented renderer feature.

## Browser and Workbench connections

The hub console uses same-origin requests. The Workbench client in
`ide/src/api/mac.ts` defaults to `/api`, which its development proxy routes to
the hub. Its connection form can persist an alternate base URL under the
local-storage key `mac.apiBaseUrl`.

Manual Workbench bearer tokens are normalized and kept in session storage.
Builds configured with `VITE_MAC_AUTH_MODE=managed` rely on the containing
proxy to supply credentials. Do not put credentials into documentation or
share token-bearing URLs.

## Electron connection ownership

`desktop/main.js` owns profiles, credentials, the local proxy, and SSH tunnels.
It reads secure profiles from `~/.mac/clients/*.yaml`, with credential files
under `~/.mac/credentials/clients/`, and also discovers legacy fleet entries.
Fleet SSH routes are resolved through `mac admin fleet ssh-spec`.

Use `mac admin login` to create the active CLI profile, `mac admin login status`
to inspect it, and `mac admin logout --revoke` to retire it. See
[SSH Client Bootstrap Contracts](client-bootstrap-contract.md). Enrollment is
provided by the CLI; do not copy a hub's complete configuration directory into
a desktop client.

`desktop/preload.js` exposes the optional `macDashboard` bridge with
`connection`, `targets`, `selectTarget`, `disconnect`, `request`, and
`openService` methods. This is a main-process integration surface. The current
Workbench HTTP client does not call that bridge, so target-selection controls
and bridge-based service navigation must not be inferred from its presence.

The current Workbench connection dialog accepts a hub URL and, outside managed
auth mode, a bearer token. Its top bar displays connection status and agent
count. It does not expose the previously documented fleet-target dropdown.

## Packaging boundary

Build the optional Workbench before packaging the desktop shell:

```console
make ide-build
make desktop-install
make desktop-package
```

The Electron package loads the built Workbench from its resources and proxies
requests to the selected connection. See `desktop/README.md` for packaging
commands. Neither a source audit nor a Python release-wheel smoke test proves
native desktop enrollment, operating-system keychain behavior, or an installed
application's end-to-end connectivity; validate those in the target package.
