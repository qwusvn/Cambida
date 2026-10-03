# Cambida 2.3 release/update contract

Cambida 2.3.0 is the first schema-2 full-package baseline. Cambida 2.3.1 adds
client self-registration with ControlHub: a client no longer needs a
server-generated installer ZIP or a manually entered Cloudflare subdomain.
Cambida 2.3.2 is the updater bridge for 2.3.0 clients: release health checks no
longer contain a fixed localhost port, and the running app prefers the updater
shipped inside the release being installed.

## Automatic ControlHub provisioning (2.3.1+)

1. Cambida creates and persists a machine ID plus a private client secret.
2. If no Cloudflare subdomain/token is configured, the client registers itself
   with `https://server.hhan24.org` and receives a short pairing code.
3. ControlHub shows registered clients to the administrator.
4. The administrator assigns the client to a Cambida site.
5. Cambida polls automatically, receives the assigned subdomain + Tunnel Token,
   saves them locally, and starts the Named Tunnel.
6. The client-reported `server_port` is used for Cloudflare ingress, so an
   existing shop running on port 8000 or another configured port does not need
   a manual Cloudflare edit.

Manual subdomain claim and legacy one-time bootstrap remain supported for
backward compatibility.

## Update flow

1. Cambida checks the latest GitHub Release for `qwusvn/Cambida`.
2. If a newer semantic version exists, it selects only `<version>.zip`.
3. The ZIP is downloaded to the Windows temporary directory.
4. `release_manifest.json` and every program file SHA-256 are verified.
5. A copy of `updater.ps1` is launched outside the application directory.
6. The old Cambida PID is allowed to exit and is force-stopped only if needed.
7. Files owned by the previous release but absent from the new release are
   backed up and removed; the complete new release is then overlaid.
8. Shop/runtime data is never part of a release and is preserved.
9. The start entrypoint declared by the new `update.json` is launched.
10. Release health metadata uses `health_v2.mode=config_port`; the 2.3.2+ updater builds
    the localhost health URL from the preserved `config.json.server_port`.
    Legacy 2.3.0 parsers/updaters do not know the `health_v2` key and therefore
    see no legacy `health.url`, so they do not incorrectly
    kill/rollback a healthy installation running on port 8000 or another port.
11. From 2.3.2 onward, Cambida copies and launches the `updater.ps1` from the
    downloaded payload first; the installed updater is fallback only.

The update protocol does not require a specific executable name, module layout
or programming language. Future releases may change those as long as they keep
schema 2 `release_manifest.json` + `update.json`.

## Preserved local data

At minimum: `config.json`, `analytics.db`, `device_id.key`,
`tunnel_token.txt`, `controlhub_machine_id.txt`,
`controlhub_client_secret.txt`, `controlhub_bootstrap.json`, `logs/`,
`cctv_videos/`, `nvr_cache/`, `.updates/` and `.update-backups/`.

## Browser behavior

Starting the server is headless. The application no longer opens a browser
automatically. The tray menu remains the explicit way to open the local UI.

## Packaging

Build 2.3.2 with:

```powershell
.\package_2.3.2.ps1
```

Every GitHub release from 2.3.0 onward must attach the complete
`<version>.zip` asset. Delta packages are intentionally not used.
