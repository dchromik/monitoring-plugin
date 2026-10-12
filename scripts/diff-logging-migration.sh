#!/usr/bin/env bash
# Review the migration of logging-view-plugin into this repo (monitoring-plugin):
# pair the files of both checkouts and emit the actual content changes.
#
# Usage:
#   scripts/diff-logging-migration.sh [output.diff]
#   LOGGING_VIEW_PLUGIN=/path/to/logging-view-plugin scripts/diff-logging-migration.sh
#
# Output (defaults to /tmp/logging-migration.diff):
#   <output>.diff   combined git-style diff of all changed files:
#                     delta -s -n --navigate < /tmp/logging-migration.diff
#   <output>.pairs  tab-separated old/new paths of changed files:
#                     while IFS=$'\t' read -r o n; do nvim -d "$o" "$n" || break; done < /tmp/logging-migration.pairs
#
# See also: scripts/diff-logging-locales.sh (translation-key comparison).
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
OLD_ROOT="${LOGGING_VIEW_PLUGIN:-$ROOT/../logging-view-plugin}"
OUT="${1:-/tmp/logging-migration.diff}"
PAIRS="${OUT%.diff}.pairs"

[[ -d $OLD_ROOT ]] || {
  echo "error: $OLD_ROOT not found — set LOGGING_VIEW_PLUGIN to your logging-view-plugin checkout" >&2
  exit 1
}

# The migration renamed files (kebab-case logs-table.tsx -> PascalCase
# LogsTable.tsx, logs.types.ts -> logs-types.ts) and moved trees, so files are
# paired per tree by relative path with case, dashes and dots stripped, then by
# basename. Format: "old-tree:new-tree[:shared]" — 'shared' marks trees that
# also hold pre-existing monitoring-plugin code, so new-only files there are
# pre-existing rather than migration additions and are not listed.
TREE_PAIRS=(
  "web/src:web/src/features/logging"
  "web/cypress/e2e/integration:web/cypress/logging/integration"
  "web/cypress/fixtures:web/cypress/logging/fixtures"
  "web/cypress/e2e/logging:web/cypress/e2e/logging"
  "web/cypress/support:web/cypress/support:shared"
  "web/cypress/views:web/cypress/views:shared"
  "pkg/server:pkg/server"
  "pkg/server/testdata:pkg/testdata"
  "cmd:cmd"
)

# Renames the path normalizer cannot derive (verified by content similarity).
ALIASES=(
  "web/src/e2e-tests-app.tsx:web/cypress/logging/harness/app.tsx"
  "web/cypress/e2e/logging/testUtils.cy.ts:web/cypress/e2e/logging/test-utils.ts"
  "web/cypress/support/e2e.ts:web/cypress/support/index.ts"
  "web/cypress/support/commands/log-commands.ts:web/cypress/support/commands/logging-console-commands.ts"
  "pkg/server/manifest_handler.go:pkg/server/plugin_handler.go"
)

norm() { printf '%s' "$1" | tr '[:upper:]' '[:lower:]' | tr -d '.-'; }
disp() { local p=${1#"$OLD_ROOT/"}; printf '%s' "${p#"$ROOT/"}"; }

declare -A used_old used_new
: > "$OUT"
: > "$PAIRS"
g_matched=0 g_identical=0 g_changed=0

emit() { # $1 = old file, $2 = new file
  used_old["$1"]=1 used_new["$2"]=1
  g_matched=$((g_matched + 1))
  if cmp -s "$1" "$2"; then
    g_identical=$((g_identical + 1))
  else
    git diff --no-index -- "$1" "$2" >> "$OUT" || true
    printf '%s\t%s\n' "$1" "$2" >> "$PAIRS"
    g_changed=$((g_changed + 1))
  fi
}

echo "== renames (explicit aliases) =="
for a in "${ALIASES[@]}"; do
  o="$OLD_ROOT/${a%%:*}" n="$ROOT/${a##*:}"
  if [[ -f $o && -f $n ]]; then
    echo "  $(disp "$o") -> $(disp "$n")"
    emit "$o" "$n"
  else
    echo "  warn: skip alias $a (file missing)" >&2
  fi
done

for spec in "${TREE_PAIRS[@]}"; do
  IFS=: read -r ot nt shared <<<"$spec"
  old_tree="$OLD_ROOT/$ot" new_tree="$ROOT/$nt"
  [[ -d $old_tree && -d $new_tree ]] || { echo "warn: skip $ot -> $nt (tree missing)" >&2; continue; }

  declare -A by_path by_name
  while IFS= read -r f; do
    [[ -n "${used_old[$f]:-}" ]] && continue
    rel="${f#"$old_tree"/}"
    by_path["$(norm "$rel")"]="$f"
    by_name["$(norm "${rel##*/}")"]="$f"
  done < <(find "$old_tree" -type f | sort)

  added=() p_matched=0
  while IFS= read -r f; do
    [[ -n "${used_new[$f]:-}" ]] && continue
    rel="${f#"$new_tree"/}"
    k="$(norm "$rel")"
    match="${by_path[$k]:-${by_name["$(norm "${rel##*/}")"]:-}}"
    if [[ -n "$match" ]]; then
      emit "$match" "$f"
      p_matched=$((p_matched + 1))
    else
      added+=("$rel")
    fi
  done < <(find "$new_tree" -type f -not -path '*__snapshots__*' | sort)

  removed=()
  while IFS= read -r f; do
    [[ -n "${used_old[$f]:-}" ]] || removed+=("${f#"$old_tree"/}")
  done < <(find "$old_tree" -type f | sort)

  echo
  echo "== $ot -> $nt =="
  echo "matched: $p_matched, removed: ${#removed[@]}"
  if [[ -n $shared ]]; then
    echo "new-only files (pre-existing monitoring code, not listed): ${#added[@]}"
  else
    for r in "${added[@]}"; do echo "  + $r"; done
  fi
  for r in "${removed[@]}"; do echo "  - $r"; done
  unset by_path by_name
done

echo
echo "== totals =="
echo "matched: $g_matched ($g_identical identical, $g_changed changed)"
echo "diff: $OUT ($(wc -l < "$OUT") lines)"
echo "changed pairs: $PAIRS ($(wc -l < "$PAIRS") files)"
echo "--- stats:"
(cd "$ROOT" && git apply --stat "$OUT" 2>/dev/null | tail -1) || true
