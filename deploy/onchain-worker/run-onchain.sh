#!/bin/bash
# Worker host: measure the newest batch in inbox/ with the code revision the
# site host sent it with. Results go to outbox/; the site host pulls them.
set -euo pipefail
W=/opt/agent-tools-worker
keys=$(ls -1t "$W"/inbox/*.keys.json 2>/dev/null | head -1 || true)
[ -n "$keys" ] || { echo "no batch in inbox"; exit 0; }
# older batches are superseded by the newest one
for old in "$W"/inbox/*.keys.json; do [ "$old" = "$keys" ] || rm -f "$old"; done
sha=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("code") or "")' "$keys")
cd "$W/src"
git fetch -q origin
if [ -n "$sha" ]; then git checkout -q --detach "$sha"; else git checkout -q --detach origin/main; fi
req=$(sha256sum requirements.txt | cut -c1-16)
if [ ! -f "$W/.req-$req" ]; then
  "$W/venv/bin/pip" install -q -r requirements.txt
  touch "$W/.req-$req"
fi
export PYTHONPATH="$W/src"
exec "$W/venv/bin/python" -m directory.jobs onchain-worker --keys "$keys" --outbox "$W/outbox"
