# Resolver Inspector for Home Assistant

An on-demand, administrator-only custom integration for reading the resolver configuration **inside the Home Assistant runtime**.

The `resolver_inspector.inspect` action returns:

- `/etc/resolv.conf` contents (up to 16 KiB) and a content hash when complete
- Symlink status, resolved path, file ownership and permission mode
- Home Assistant process effective UID/GID and supplementary groups
- Effective-user read/write access checks, where supported by the OS
- The filesystem read-only flag

It does not write files, change DNS or permissions, run subprocesses, send network requests, or schedule polling. Results are returned only as an action response; the integration does not log or upload them. File reads may update filesystem access timestamps according to the mount policy.

The access check is non-destructive: it is **not an actual write test**, and does not establish whether changes would persist after a restart or upgrade. Runtime metadata describes the Home Assistant process, not necessarily its host OS.

## Install using HACS

1. In HACS, add `https://github.com/Yamar50/ha-resolver-tools` as a custom repository, category **Integration**.
2. Download **Resolver Inspector**, and restart Home Assistant if requested.
3. Go to **Settings → Devices & services → Add integration → Resolver Inspector**.
4. As an administrator, open **Developer tools → Actions**, run `resolver_inspector.inspect`, and inspect the returned response.

## Manual installation

Copy `custom_components/resolver_inspector` into your Home Assistant configuration directory's `custom_components` folder. Restart Home Assistant and add the integration as above.

## Remove

Remove the integration entry, uninstall its files using HACS or your normal configuration-file access method, and restart Home Assistant if requested. No DNS restoration is necessary because this integration never changes DNS.

## Local verification

```sh
python3 -m unittest discover -s tests -v
python3 -m compileall -q custom_components
```

Tests use temporary synthetic files and verify that contents, permission mode, and modification time remain unchanged; they also cover symlinks, missing files, and bounded reads. Successful local tests do not verify a particular Home Assistant appliance's filesystem permissions.
