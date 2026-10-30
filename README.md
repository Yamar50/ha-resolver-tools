# DNS Manager for Home Assistant on SwitchBot AI Hub

Choose which DNS servers Home Assistant uses on your **SwitchBot AI Hub**, with a backup of the original configuration and an explicit restore action.

## Why this integration exists

On the SwitchBot AI Hub tested for this project, the embedded Home Assistant container was preconfigured to use these public DNS servers:

```text
nameserver 8.8.8.8
nameserver 114.114.114.114
```

This can be a problem when you want Home Assistant to resolve local hostnames through your router, use a local DNS service such as Pi-hole or AdGuard Home, or operate on a network that restricts external DNS access.

DNS Manager lets you choose your preferred DNS servers from the Home Assistant UI, without opening a shell inside the container. It changes the `/etc/resolv.conf` file visible to Home Assistant and can restore its original contents. It does not configure DNS for the entire AI Hub or its other applications.

## Tested environment

Verified on **October 30, 2026**, with the following combination:

| Component | Tested version |
| --- | --- |
| SwitchBot AI Hub firmware | **V3.2** |
| Home Assistant Core | **2026.10.4** (Home Assistant Container) |
| DNS Manager | **0.2.0** |

The two public DNS addresses above were observed on this tested installation. **Other AI Hub firmware versions, Home Assistant versions, devices, and installation types have not been tested.** This is not a claim that every AI Hub version uses the same defaults.

Changing the DNS server, resolving names through the selected server, and keeping the change through a **normal Home Assistant restart** were verified. The resolver file's contents, hash, inode, and modification time remained unchanged after that restart, with **no automatic reapplication** by this integration.

Persistence through an **AI Hub reboot, container recreation, or Home Assistant update has not been tested**. Backup and rollback behavior is covered by local tests; restoring the original DNS was not exercised on the live AI Hub after the successful change.

## Use

1. In HACS, add `https://github.com/Yamar50/ha-resolver-tools` as a custom repository (Integration), download, and restart Home Assistant if requested.
2. Add **DNS Manager** in Settings → Devices & services.
3. Open its **Home Assistant DNS** device. Enter one to three comma-separated IP addresses in **DNS server candidates**. This stores candidates only.
4. Press **Apply DNS servers** as an administrator. The integration validates the IPs, checks each server, saves the original bytes, changes only nameserver lines and verifies system name resolution.
5. Inspect **Current DNS servers** and **Last DNS operation**. **Restore original DNS** restores the original bytes saved before the first apply. **Refresh DNS status** rereads the file.

**No startup reapplication, polling or automatic restart is implemented.** A restart reads the current file without writing it. Candidate values survive in Home Assistant's configuration, independently of whether the runtime resolver file survives. Host/container recreation and Home Assistant upgrades need separate verification; this integration cannot control the host's container DNS configuration.

The integration was previously called **Resolver Inspector**. Its internal domain and action names remain `resolver_inspector` for compatibility. Existing entries are renamed on setup.

## Actions

All actions require an administrator (or Home Assistant's own automation context). They accept optional responses:

- `resolver_inspector.inspect`: fixed-file content up to16KiB, hash, ownership, permissions, effective process access, filesystem read-only flag, Linux mount metadata and backup status. Read only; access checks do not prove write success.
- `resolver_inspector.check`: bounded direct DNS A queries and system resolver lookups for `api.switch-bot.com` and `www.home-assistant.io`.
- `resolver_inspector.apply`: `servers` (list of literal IPs; optional, otherwise candidates) and `expected_sha256` from the latest inspection. Hash comparison refuses a changed file. Symlinks/nonregular files are refused.
- `resolver_inspector.restore`: `expected_sha256` from a fresh inspection; restore exact original contents.

Backups live in `/config/.storage/resolver_inspector_backup.json` (relative to the actual HA configuration directory), mode0600, with an integrity hash. Do not delete that backup before restoration. Writes preserve the existing resolver inode, mount, owner and mode. A normal write or verification failure attempts rollback; interruption or a failed rollback can require explicit restoration. A pending/failed operation is reported, never silently reapplied at startup. System resolver caches and already-open connections can delay effective changes; a successful lookup alone cannot prove the upstream used by every integration.

Before a first write, inspect mount metadata to determine what owns the file and whether a change could affect a shared runtime. Availability of read/write access does not establish mount isolation. Changes affect the resolver file visible inside HA, not necessarily every application on the appliance. DNS probes are sent only when explicitly checking or applying, to the configured candidate servers. Results are not uploaded or logged by this integration. Reads can update access timestamps.

## Remove

Restore the original DNS first if desired. Removing the integration or uninstalling its files does not restore DNS automatically and does not delete the backup. Restart as requested by HACS.

## Local verification

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q custom_components
```

Tests use temporary files and mocked network lookups. They check read-only inspection, validation, preserving non-nameserver lines/inodes/modes, original backup, explicit restoration, concurrency hash guards and rollback. Local tests do not establish a particular appliance's write capability, mount isolation or restart persistence.
