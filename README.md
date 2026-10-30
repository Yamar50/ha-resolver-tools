# DNS Manager for Home Assistant

Inspect `/etc/resolv.conf` inside Home Assistant and explicitly configure DNS servers with a restorable original backup. Version 0.2.0 adds administrator-only apply/restore actions to the original read-only diagnostic.

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
