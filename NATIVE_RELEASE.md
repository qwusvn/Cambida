# Cambida 2.3 release/update contract

Cambida 2.3.0 is the first schema-2 full-package baseline. Cambida 2.3.1 adds
client self-registration with ControlHub: a client no longer needs a
server-generated installer ZIP or a manually entered Cloudflare subdomain.
Cambida 2.3.2 is the updater bridge for 2.3.0 clients: release health checks no
longer contain a fixed localhost port, and the running app prefers the updater
shipped inside the release being installed. Cambida 2.3.3 adds an independent
watchdog installed outside the Cambida application tree so application/update
failure no longer removes the recovery path.

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

## Runtime-state preservation (2.3.3+)

- Release ZIPs never contain `config.json`, database, shop identity, Tunnel
  Token, ControlHub client secret, video, logs or caches. Copying/extracting a
  full package over an existing Cambida directory therefore does not overwrite
  those files.
- Small identity-critical state (`config.json`, `tunnel_token.txt`,
  `controlhub_machine_id.txt`, `controlhub_client_secret.txt` and legacy
  `controlhub_bootstrap.json`) is additionally mirrored outside the install
  tree under `%LOCALAPPDATA%\Cambida\runtime-state`.
- A frozen Cambida release restores only missing files from that vault before
  the first-run seed is considered. Existing runtime files always win, so
  repeating migration/startup is idempotent.
- The watchdog snapshots this state before an updater stops Cambida and while a
  healthy release is running. A Tunnel Token is tied to the subdomain that was
  active when it was captured, so an old token is not revived after a deliberate
  subdomain change.
- `config.release.json` is only a clean first-install seed. It is never merged
  over an existing runtime config. Its web-server default is 8000.

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
12. Startup order is: restore/load runtime config -> start the local web server
    on the preserved `server_port` -> wait for that listener -> ControlHub
    claim/poll -> start the Named Tunnel. The TunnelHealth loop also refuses to
    start cloudflared while the local origin is not listening.

## Independent watchdog (2.3.3+)

- The release carries `CambidaWatchdog.exe`, but installation copies it to a
  versioned executable under `%LOCALAPPDATA%\CambidaWatchdog` and registers it
  in the current user's Windows Run key. It therefore survives replacement of
  the Cambida directory.
- Cambida and `Chay_CCTV.cmd` also refresh the external watchdog on normal
  startup, covering fresh/manual installations.
- The updater creates the protected `.watchdog-update.json` transaction before
  stopping Cambida. Every release file is backed up and journaled before it is
  modified. Runtime/shop data is never journaled.
- While an update is applying or starting, the watchdog does not interfere. If
  that transaction is left stale by a crash/reboot, the watchdog restores the
  journaled files from `.update-backups` and starts the previous release.
- After updater health succeeds, the transaction enters a stabilization window.
  If the new release repeatedly crashes, the watchdog restarts it up to three
  times and then rolls back. A stable release is marked `healthy`.
- Outside an update transaction, the watchdog probes `/api/ping` on the
  preserved `config.json.server_port`; it only terminates a process whose
  full executable path exactly matches the configured installation's
  `Cambida.exe`.

The update protocol does not require a specific executable name, module layout
or programming language. Future releases may change those as long as they keep
schema 2 `release_manifest.json` + `update.json`.

## Preserved local data

At minimum: `config.json`, `analytics.db`, `device_id.key`,
`tunnel_token.txt`, `controlhub_machine_id.txt`,
`controlhub_client_secret.txt`, `controlhub_bootstrap.json`, `logs/`,
`cctv_videos/`, `nvr_cache/`, `.updates/`, `.update-backups/` and the
watchdog transaction `.watchdog-update.json`.

## Browser behavior

Starting the server is headless. The application no longer opens a browser
automatically. The tray menu remains the explicit way to open the local UI.

## Packaging

Build 2.3.3 with:

```powershell
.\package_2.3.3.ps1
```

Every GitHub release from 2.3.0 onward must attach the complete
`<version>.zip` asset. Delta packages are intentionally not used.
