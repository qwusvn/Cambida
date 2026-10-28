# Cambida 2.3 release/update contract

Cambida 2.3.0 is the first schema-2 full-package baseline. Cambida 2.3.1 adds
client self-registration with ControlHub: a client no longer needs a
server-generated installer ZIP or a manually entered Cloudflare subdomain.
Cambida 2.3.2 is the updater bridge for 2.3.0 clients: release health checks no
longer contain a fixed localhost port, and the running app prefers the updater
shipped inside the release being installed. The current updater runs independently as updater.exe; the external watchdog
is no longer started or packaged.

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

The release keeps cloudflared.exe for the tunnel connection. Manual tunnel
installation scripts in cloudflared_setup are no longer packaged; ControlHub
supplies the domain and token. Existing local tokens remain supported.

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
- `config.release.json` is only a clean first-install seed. It is never merged
  over an existing runtime config. Its web-server default is 8000.

## Update flow

1. Cambida checks the latest GitHub Release and selects <version>.zip.
2. It copies updater.exe outside the install directory on the same drive,
   then launches it silently. Cambida does not exit if that launch fails.
3. The independent updater removes the old watchdog's autostart and stops
   Cambida plus its child processes belonging to this installation.
4. It downloads the ZIP to a temporary directory on the same drive and checks
   the release version, manifest hashes and archive paths before copying.
5. It reads the installed update_exclude.txt; if absent, the release's TXT
   supplies the initial policy. Each nonempty line is a relative file or
   directory path. # starts a comment. Matching is case-insensitive and a
   directory rule covers its whole subtree. Listed paths are skipped; all
   other release files are overwritten, including read-only files.
6. It launches the entrypoint in the new update.json and reports an error
   if the process exits immediately. Details are in logs/updater.log.

A failed download or validation restarts the original application without
overwriting it. A copy or startup failure is logged as failure. There is no
watchdog, transaction journal, stabilization window or automatic rollback.
Files absent from the ZIP are left in place.

updater.ps1 is only a compatibility bridge for older 2.3.x clients that
already download and launch a payload updater. It delegates to the
updater.exe shipped in that payload. Since 2.3.7, that bridge also works for
installations at a drive root (`D:\`): it falls back to the system temp
directory when the installation has no parent directory, quotes arguments
with the Windows `CommandLineToArgvW` rules so a trailing backslash cannot
escape the closing quote, and restarts the application if the hand-off fails
instead of leaving the recorder stopped. Its parameter signature
(`-Payload -Target -OldPid -CleanupRoot`) is unchanged for 2.3.4 clients.

## Preserved local data

The entire copy policy is in update_exclude.txt, including the TXT itself.
Its defaults preserve configuration, databases, identity/token files,
videos, caches and logs. Edit that one file to change what survives an update.
Removing a line permits a matching release file to overwrite the local file.

## Browser behavior

Starting the server is headless. The application no longer opens a browser
automatically. The tray menu remains the explicit way to open the local UI.

## Packaging

Build 2.3.7 with:

```powershell
.\package_native.ps1 -Version 2.3.7
```

Every GitHub release from 2.3.0 onward must attach the complete
`<version>.zip` asset. Delta packages are intentionally not used.
