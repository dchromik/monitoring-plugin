#!/usr/bin/env bash
# Compare the old logging-view-plugin English translation keys against the
# merged monitoring-plugin locale: during the migration the keys of
# plugin__logging-view-plugin.json were absorbed into
# plugin__monitoring-plugin.json, so a file-level diff is useless.
#
# Reports, per key: surviving keys with changed values, dropped keys, and
# possible renames (a dropped key whose value reappears under a different key).
# The other languages have no old-repo counterpart to compare against.
#
# Usage:
#   scripts/diff-logging-locales.sh
#   LOGGING_VIEW_PLUGIN=/path/to/logging-view-plugin scripts/diff-logging-locales.sh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OLD_ROOT="${LOGGING_VIEW_PLUGIN:-$ROOT/../logging-view-plugin}"
OLD="$OLD_ROOT/web/locales/en/plugin__logging-view-plugin.json"
NEW="$ROOT/web/locales/en/plugin__monitoring-plugin.json"

[[ -f $OLD && -f $NEW ]] || {
  echo "error: $OLD or $NEW not found — set LOGGING_VIEW_PLUGIN to your logging-view-plugin checkout" >&2
  exit 1
}

python3 - "$OLD" "$NEW" <<'EOF'
import json
import sys

old = json.load(open(sys.argv[1]))
new = json.load(open(sys.argv[2]))

same = [k for k in old if k in new and old[k] == new[k]]
changed = [k for k in old if k in new and old[k] != new[k]]
dropped = [k for k in old if k not in new]

# a dropped key whose value survived under a different key was probably reworded
new_by_value = {}
for k, v in new.items():
    if isinstance(v, str) and len(v) >= 20:
        new_by_value.setdefault(v, []).append(k)
renames = {}
for k in dropped:
    if isinstance(old[k], str):
        hits = new_by_value.get(old[k])
        if hits:
            renames[k] = hits
dropped = [k for k in dropped if k not in renames]

print(f"old keys: {len(old)}, new keys: {len(new)}")
print(f"survived unchanged: {len(same)}, value changed: {len(changed)}, "
      f"dropped: {len(dropped)}, possible renames: {len(renames)}")

for k in sorted(changed):
    print(f"\n  CHANGED {k!r}\n    old: {old[k]!r}\n    new: {new[k]!r}")
for k, targets in sorted(renames.items()):
    print(f"\n  RENAME? {k!r} -> {targets}")
for k in sorted(dropped):
    extra = f" (was: {old[k]!r})" if old[k] != k else ""
    print(f"\n  DROPPED {k!r}{extra}")
EOF

echo
echo "other languages in web/locales (no old-repo counterpart to compare):"
ls "$ROOT/web/locales"
