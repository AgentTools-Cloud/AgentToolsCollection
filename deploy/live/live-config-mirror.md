# Live server configuration mirror (directory-host)

Copied from the production host on 2026-11-15 so the repo holds what actually runs:

- `etc/systemd/system/` - every `agent-tools-*` and `mcpserver*` unit, timer and drop-in
  (backups such as `*.bak*` left out). `mcpserver.service.d/override.conf` is what makes the
  main instance run `--workers 2` with `MemoryMax=2G`.
- `opt/agent-tools-ops/disk_cleanup_reminder.py` - the daily disk Notice (09:15).
- `usr/local/sbin/` - `agent-tools-claim-invites`, `agent-tools-rolling-restart`.

No secrets: units reference root-0600 files under `/etc/agent-tools/` and `/opt/mcpserver/.env`,
which stay on the server. When you change a unit on the server, copy it here in the same change.
The older single files directly under `deploy/` predate this mirror; where they differ, this
mirror is the running version.
