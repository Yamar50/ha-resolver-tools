# DNS Manager for Home Assistant on SwitchBot AI Hub

Choose which DNS servers Home Assistant uses on your **SwitchBot AI Hub**, with a backup of the original configuration and an explicit restore action.

## Why this integration exists

On the SwitchBot AI Hub tested for this project, the embedded Home Assistant container was preconfigured to use these public DNS servers:

```text
nameserver 8.8.8.8
nameserver 114.114.114.114
```

This can be a problem when you want Home Assistant to resolve local hostnames through your router, use a local DNS service such as Pi-hole or AdGuard Home, or operate on a network that restricts external DNS access.

DNS Manager lets you choose your preferred DNS servers from the Home Assistant UI, without opening a shell inside the container. It changes `/etc/resolv.conf`, refreshes Home Assistant's existing shared DNS resolver, and can restore the original file. It does not configure DNS for the entire AI Hub or its other applications.

## Tested environment

Verified on **October 30, 2026**, with the following combination:

| Component | Tested version |
| --- | --- |
| SwitchBot AI Hub firmware | **V3.2** |
| Home Assistant Core | **2026.10.4** (Home Assistant Container) |
| DNS Manager | **0.2.0** |

The two public DNS addresses above were observed on this tested installation. **Other AI Hub firmware versions, Home Assistant versions, devices, and installation types have not been tested.** This is not a claim that every AI Hub version uses the same defaults.

Changing the DNS server, resolving names through the selected server, and keeping the change through a **normal Home Assistant restart** were verified. The resolver file's contents, hash, inode, and modification time remained unchanged after that restart, with **no automatic reapplication** by this integration.

On November 8, 2026, the same HA version was observed with a different container identity and the original public DNS restored. The event that replaced the container was not established. Version **0.3.0** adds opt-in startup reapplication to handle this kind of reset. It cannot change the host container configuration, and full AI Hub reboot/update behavior has not been validated. Backup and rollback behavior is covered by local tests; restoring the original DNS was not exercised on the live AI Hub after the successful change.

On November 10, file reapplication was confirmed successful, but the running HA process still sent Alexa and SwitchBot DNS questions to an old DNS server. A fresh process using the same libraries used the selected server. This installation bind-mounts `resolv.conf` separately from `/etc`; c-ares 1.34.6 monitors the parent directory for changes. This explains why checking only the file and a new system lookup was insufficient. Version **0.4.0** adds an explicit runtime refresh using [c-ares system-configuration reload](https://c-ares.org/docs/ares_reinit.html).

Version **0.4.0** was checked on the same HA installation on **November 10, 2026**, with HA **2026.10.4**, aiohttp **3.14.5**, aiohttp-asyncmdnsresolver **0.2.0**, aiodns **4.0.4**, pycares **5.0.1** and c-ares **1.34.6**. An isolated process using these installed libraries reproduced a stale resolver after a temporary configuration file changed; the runtime refresh updated that existing resolver and its lookups succeeded. After backing up and deploying the integration, a normal HA restart, the startup refresh and an explicit refresh action succeeded. The running HA resolver reported the selected server, and packet observation confirmed its test queries used that server and received successful replies. Firmware was not rechecked for this follow-up. A full AI Hub reboot or container recreation was not performed; startup lifecycle and simulated file replacement are also covered by local tests.

## Use

1. Open **HACS**, search for **DNS Manager**, open its repository page, and select **Download**. Restart Home Assistant if requested.
2. Add **DNS Manager** in Settings → Devices & services.
3. Open its **Home Assistant DNS** device. Enter one to three comma-separated IP addresses in **DNS server candidates**. This stores candidates only.
4. Press **Apply DNS servers** as an administrator. The integration validates the IPs, checks each server, saves the original bytes, changes only nameserver lines, verifies system name resolution and refreshes HA's running shared resolver.
5. Optionally enable **Reapply saved DNS at startup**. After upgrading from 0.2.x, press **Apply DNS servers** once first to save a validated target; older backups contain only a hash.
6. Compare **Current DNS servers** (the file) with **Running HA DNS servers** (the shared resolver). Attributes show the saved target, startup policy, file result and runtime refresh result. **Restore original DNS** disables startup reapplication, restores the original bytes and refreshes the runtime. **Refresh DNS status** only reads status. **Refresh running HA DNS** explicitly reloads the current file without rewriting it.

If **DNS Manager** is not available in your HACS catalog yet, use the custom repository fallback: open the HACS menu → **Custom repositories**, add `https://github.com/Yamar50/ha-resolver-tools` with type **Integration**, then download it and continue with the steps above.

**Startup reapplication is opt-in and disabled by default.** Once enabled, integration startup (including reload) compares the current DNS addresses, in order, with the **last successfully applied servers** stored in the durable backup. Matching DNS leaves `/etc/resolv.conf` untouched, including its modification time. A mismatch uses the same preflight checks, hash guard, original backup and rollback as a manual apply. Non-nameserver directives in the current file are preserved. Editing candidates alone never changes the saved startup target. Turning the switch off leaves current DNS unchanged.

There is one file reapplication attempt per integration startup, with no background polling or automatic HA restart. A failed preflight leaves the file unchanged; a file verification failure attempts rollback. Errors are logged and reported in `startup_result`. A previous interrupted or failed write requires a successful manual apply before replay.

With startup reapplication enabled, the runtime is refreshed even if the file already matches. A second runtime-only check runs once when HA finishes starting, to cover resolvers created during concurrent integration setup. If the file has changed away from the saved target meanwhile, that check reports the mismatch rather than silently adopting it. Other integrations can start concurrently, so queries may still use the host-provided DNS **before** DNS Manager loads. Host changes after startup are not continuously monitored or corrected.

Runtime refresh reuses the existing HA shared resolver and requests c-ares to reload its system configuration. It verifies the ordered IP addresses and standard DNS port, then clears DNS caches only on HA connectors using that resolver. Open connections and client sessions remain open. Explicit application DNS overrides are not forcibly replaced; an override which prevents the requested configuration from taking effect is reported as a mismatch. Integrations with their own resolvers, explicit DNS servers, and other AI Hub applications are outside this scope.

The adapter checks for the required HA/aiohttp/pycares interfaces. An unsupported runtime, missing explicit nameservers or a refresh failure is reported separately. If the file was successfully applied but runtime refresh fails, **the file stays applied**, the action reports an error, and the runtime result explains the failure. A file result of `applied` alone does not mean the running resolver was updated. When no shared resolver exists yet, `not_initialized` is reported instead of claiming verification; a future resolver reads the current file.

The integration was previously called **Resolver Inspector**. Its internal domain and action names remain `resolver_inspector` for compatibility. Existing entries are renamed on setup.

## Actions

All actions require an administrator (or Home Assistant's own automation context). They accept optional responses:

- `resolver_inspector.inspect`: fixed-file content up to16KiB, hash, ownership, permissions, effective process access, filesystem read-only flag, Linux mount metadata, backup status, runtime DNS servers and last runtime refresh. Read only; access checks do not prove write success.
- `resolver_inspector.check`: bounded direct DNS A queries, system lookups and lookups through HA's running shared resolver for `api.switch-bot.com` and `www.home-assistant.io`. No HTTP/API requests are made.
- `resolver_inspector.apply`: `servers` (list of literal IPs; optional, otherwise candidates) and `expected_sha256` from the latest inspection. Hash comparison refuses a changed file. Symlinks/nonregular files are refused.
- `resolver_inspector.restore`: `expected_sha256` from a fresh inspection; disable startup reapplication and restore exact original contents.
- `resolver_inspector.configure_startup`: required boolean `enabled`; persist the startup policy without changing DNS immediately. Enabling requires a successful apply with 0.3.0 or later.
- `resolver_inspector.refresh_runtime`: reload the current resolver file into HA's existing shared resolver, verify the result and clear its connector DNS caches. Does not write the file or change the saved startup target.

Backups live in `/config/.storage/resolver_inspector_backup.json` (relative to the actual HA configuration directory), mode0600, with an integrity hash. Do not delete that backup before restoration. Writes preserve the existing resolver inode, mount, owner and mode. A normal write or verification failure attempts rollback; interruption or a failed rollback can require explicit restoration. A pending/failed write is reported and is not replayed at startup. System resolver caches and already-open connections can delay effective changes; a successful lookup alone cannot prove the upstream used by every integration.

Before a first write, inspect mount metadata to determine what owns the file and whether a change could affect a shared runtime. Availability of read/write access does not establish mount isolation. Changes affect the resolver file visible inside HA, not necessarily every application on the appliance. DNS probes are sent when checking or applying, including an enabled startup reapplication, to the selected DNS servers. Results are not uploaded; startup success or failure is logged locally. Reads can update access timestamps.

## Remove

Restore the original DNS first if desired. Removing the integration or uninstalling its files does not restore DNS automatically and does not delete the backup. Restart as requested by HACS.

## Development and validation

GitHub Actions run the official HACS validation, hassfest, and the local unit tests on pushes and pull requests. No HACS checks are disabled.

Version **0.4.0** adds runtime refresh, diagnostics and checks. Version **0.3.0** adds the opt-in startup policy and saved successful target. Version **0.2.1** added HACS distribution metadata and brand icons.

## Local verification

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q custom_components
```

Tests use temporary files and mocked network lookups. They check read-only inspection, validation, preserving non-nameserver lines/inodes/modes, original backup, explicit restoration, concurrency hash guards, rollback, matching-DNS no-op behavior, simulated container-file replacement, startup failure handling, and restoration disabling the startup policy. Local tests do not establish a particular appliance's write capability, mount isolation or restart persistence.

Runtime and lifecycle tests additionally cover stale channels, asynchronous reload, ordered IPv4/IPv6 endpoints and ports, explicit overrides, unchanged connection lifetime, startup opt-in, resolvers created during startup, read-only inspection, manual apply/restore wiring, and separately reported runtime failure. Lightweight HA test doubles do not establish real HA compatibility; runtime interfaces and actual DNS traffic must also be checked on the target installation.

## Development disclosure

The integration code and documentation were generated with **OpenAI Codex** and refined using automated tests and the live-device checks documented above. The tested environment and verification limits are stated explicitly in this README.

## License

MIT License. See [LICENSE](LICENSE).
