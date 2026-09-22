# Directory operations: categories, Ask and encrypted backups

## Categories

`GET /categories?kind=x402|mcp|a2a&q=...&page=1` renders at most 60 topics.
Each kind has a five-minute process-local aggregate cache; query strings do not
create new cache entries. No raw categories or skill tags are deleted.
x402 links use `/x402?category=...`; MCP/A2A topics use full-text search, so
coverage counts need not equal search result counts. Existing JSON API contracts
and exact-name expansion remain unchanged.

## Ask

Install `deploy/agent-tools-ask.env` as `/etc/agent-tools/ask.env` and
`deploy/mcpserver-ask.conf` as
`/etc/systemd/system/mcpserver.service.d/ask.conf`. Restart the web service to
load changed environment or Python modules; preserve the separate 2 GiB memory
override and two-worker command.

`AGENT_TOOLS_ASK_USE_SAFETY_BACKEND=1` explicitly selects this project's existing
`AGENT_TOOLS_SAFETY_BASE_URL` and `AGENT_TOOLS_SAFETY_API_KEY`.
Both `AGENT_TOOLS_ASK_MODEL` and `AGENT_TOOLS_SAFETY_MODEL` are explicitly set to
`OpenAI/GPT-5.6-Sol`. Ask still supports independent model selection; if unset
or empty, it inherits `AGENT_TOOLS_SAFETY_MODEL`.
It shares the project's existing key quota/billing;
no key values are stored in this repository or copied between environment files.
Missing shared settings fail closed, not back to another credential. With the
flag absent, the old explicit Ask configuration retains its behavior.

Ranking prompts cap candidate text and omit full resource schemas; returned
service cards are unchanged. `ask_outcome=llm_success|llm_unavailable|no_valid_recommendations`
logs distinguish successful ranking from HTTP-200 retrieval fallback. These are
operational logs, not proof of sustained model availability or ranking quality.

## Scheduled work

Keep `agent-tools-onchain.timer` **disabled and inactive**. Reverify's `Wants=`
and onchain's `After=` retain the post-verification run. Do not remove that
dependency or assume `disable` alone stops a running timer.

`agent-tools-backup.timer`: daily 04:00 host time (production Asia/Shanghai).
Source backup unit: 384 MiB memory, 45-minute job deadline, nice/idle I/O.
The existing 09:30 watchdog reports a failed backup unit or a missing/invalid/
older-than-30h durable success receipt. It does not rely on transient systemd
oneshot timestamps, which can disappear when a unit unloads.

## Backup and restore verification

- Source `/var/backups/agent-tools`: newest 3 verified completed archives.
- Destination `root@43.130.32.180:/var/backups/agent-tools`: newest 14 archives.
- Source stores only public age recipients at `/etc/agent-tools-backup/recipients.txt`.
- Destination private identity: `/etc/agent-tools-backup/identity.agekey`, root 0600.
  Keep an additional operator-managed offline copy: losing that identity makes
  the encrypted backups unreadable. Do not put it in Git or on the source host.
- `ops/backup_agent_tools.py` takes individually consistent SQLite online snapshots,
  validates those copies, bundles allowlisted config and both application `.env`
  files (including Hub's Fernet key), then encrypts before network transfer.
- Destination needs `age`, Python 3 and `cryptography`; both backup Python scripts
  are installed together under `/opt/agent-tools-backup`.
- Transfer goes to `.partial`. Destination verification runs under a bounded
  systemd transient service: 384 MiB / 30 minutes, locked private scratch, 8 GiB
  extraction cap. It decrypts, verifies all hashes and both SQLite databases,
  compares recorded counts, and decrypts every nonempty Hub upstream credential.
  No application is started and no live DB is replaced.
- Only after verification succeeds does promotion/retention run. Source atomically
  writes `/var/backups/agent-tools/last-success.json` (root 0600).
- Source reserves 2 × logical DB sizes + 2 GiB free, and refuses another snapshot
  at 4 completed ciphertexts. Upload failure cannot grow daily archives without
  bound. Follow `complete_offsite()`'s documented lock protocol to recover pending
  ciphertext rather than deleting known-good backups to bypass the cap.

Normal failures clean plaintext. Hard-kill leftovers are removed at the next
locked invocation, not securely erased; underlying disk encryption/offline key
escrow and immutable backups are not provided by these scripts. Source and
destination root SSH remain mutually trusted, so this is not ransomware-proof
storage. Database/config snapshots are not one cross-service transaction; avoid
rotating Fernet keys during backup. Config allowlists are supporting artifacts,
not a complete OS/nginx/SSH disaster-recovery image; `sites-available` can differ
from active nginx configuration. Preserve independent infrastructure records.

## Rollback boundary

Restore only the changed code/config from the pre-release bundle and restart
the main web service; do not restore production databases merely to roll back
this code change. Stop the backup timer if disabling automation, but retain
verified encrypted archives and the destination identity. Keep the onchain
timer disabled unless deliberately returning to the old duplicate schedule.