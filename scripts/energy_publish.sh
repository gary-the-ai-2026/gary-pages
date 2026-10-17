#!/bin/bash
# energy_publish.sh — atomic "read live → edit → publish" for gary-pages data JSON files.
#
# WHY THIS EXISTS (dream rec dream-wf-energy-git-dance, 17 Oct 2026):
# The energy data JSONs (energy-data.json, battery-soc-history.json, energy-usage*.json, …)
# live on the orphan `data` branch and are owned by the 2-min collect cron
# (energy_dashboard_collect.sh), which force-resets + branch-switches the shared working
# tree every cycle. Anything uncommitted on `main` gets reverted within 2 minutes, so a
# manual "edit data/*.json then git commit" is racy, and rescuing the tree by hand
# (git stash push -m wip, edit, git checkout --) was pure overhead with data-loss risk.
#
# This script encapsulates the SAFE pattern used by the existing wrappers (collect.sh /
# amber_daily_refresh.sh):
#   1. read the authoritative LIVE copy of each target file from a git ref
#      (default origin/data — NEVER the main working tree, which is stale/reverted)
#   2. apply a caller-supplied transform to that staged copy
#   3. validate each edited .json is still valid JSON (refuses to publish corruption)
#   4. switch to the data branch, reset --hard origin/data, lay the edited files in,
#      commit, and push --force-with-lease (fallback hard-reset + force), matching the
#      sibling wrappers' concurrency behaviour
#   5. return to main and restore the accumulated working-tree data files so the next
#      2-min collect cycle loses no SOC/history points
#   6. verify the pushed blob via the data-branch ref
#
# Because the edited content differs from origin/data, a push to the `data` branch
# invalidates the raw CDN edge (20-60 s lag) — the frontend already cache-busts each
# JSON fetch with ?v=Date.now() + {cache:'no-store'}; no extra token is stamped into the
# data. Verify after a publish with the GitHub API or `git show origin/data:<file>`,
# never the main working-tree copy.
#
# USAGE (run from anywhere; drop-in from a cron job):
#   energy_publish.sh \
#       --message "Describe the edit" \
#       --files "data/energy-data.json data/battery-soc-history.json" \
#       --apply /path/to/transform.py
#
#   --apply  path to a python (or bash) transform. It receives the STAGE directory as
#            argv[1] and must READ the authoritative copies there and REWRITE the target
#            files in place. Example transform (python):
#              import sys, json, os
#              stage = sys.argv[1]
#              p = os.path.join(stage, "energy-data.json")
#              d = json.load(open(p))
#              d["note"] = "feed-in reconciled"
#              json.dump(d, open(p, "w"), indent=2)
#            The transform may take extra args after --apply "--arg1 --arg2".
#
#   --dry-run optional: stage + apply + validate only; print diff summary; do NOT touch
#            git commits/pushes or switch branches. Safe to test a transform.
#
# REQUIRES: set -euo pipefail; the shlock guard serialises against the 2-min collect and
#           15-min refresh crons (shared ~/.hermes/run/energy_dashboard.lock).
set -euo pipefail

REPO_DIR="${ENERGY_PUBLISH_REPO:-$HOME/Projects/gary-pages}"
SCRIPTS_DIR="$HOME/.hermes/scripts"
LOCK_FILE="$HOME/.hermes/run/energy_dashboard.lock"   # SHARED with collect + refresh
LOG_PREFIX="[energy-publish]"
SOURCE_REF="origin/data"    # authoritative live source; main working tree is stale
DRY_RUN=false
MESSAGE=""
FILES=""
APPLY=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --message)   MESSAGE="$2"; shift 2 ;;
        --files)     FILES="$2"; shift 2 ;;
        --apply)     APPLY="$2"; shift 2 ;;
        --source)    SOURCE_REF="$2"; shift 2 ;;
        --dry-run)   DRY_RUN=true; shift ;;
        *) echo "$LOG_PREFIX Unknown option: $1" >&2; echo "Usage: energy_publish.sh --message \"...\" --files \"...\" --apply /path/transform.py [--source origin/data] [--dry-run]" >&2; exit 2 ;;
    esac
done

[[ -n "$MESSAGE" ]] || { echo "$LOG_PREFIX --message is required" >&2; exit 2; }
[[ -n "$FILES" ]]   || { echo "$LOG_PREFIX --files is required (space-separated data-relative paths)" >&2; exit 2; }
[[ -n "$APPLY" ]]   || { echo "$LOG_PREFIX --apply transform path is required" >&2; exit 2; }
[[ -f "$APPLY" ]]   || { echo "$LOG_PREFIX --apply transform not found: $APPLY" >&2; exit 2; }

# ── Serialisation: don't collide with the 2-min collect / 15-min refresh crons ──
MODE_LABEL="publish"
$DRY_RUN && MODE_LABEL="dry-run"
echo "$LOG_PREFIX $(date '+%H:%M:%S') Starting ($MODE_LABEL, source=$SOURCE_REF)"

if ! $DRY_RUN; then
    mkdir -p "$(dirname "$LOCK_FILE")"
    if ! shlock -f "$LOCK_FILE" -p $$ 2>/dev/null; then
        echo "$LOG_PREFIX Skipping: another energy job holds the git lock (concurrent run)."
        exit 0
    fi
fi

cd "$REPO_DIR"

# ── Fetch latest data branch so we read + publish against a fresh base ──
if ! $DRY_RUN; then
    git fetch origin "$SOURCE_REF" 2>/dev/null || true
fi

# ── Stage the authoritative live copies ──
STAGE=$(mktemp -d /tmp/energy-publish-XXXXXX)
trap 'rm -rf "$STAGE"' EXIT

# parse space-separated file list portably (bash 3.2 has no mapfile)
FILE_LIST=()
for f in $FILES; do FILE_LIST+=("$f"); done
for f in "${FILE_LIST[@]}"; do
    # mirror the data-relative path under stage so transforms using basename or
    # full path both work
    mkdir -p "$STAGE/$(dirname "$f")"
    if git cat-file -e "$SOURCE_REF:$f" 2>/dev/null; then
        git show "$SOURCE_REF:$f" > "$STAGE/$f"
        echo "$LOG_PREFIX staged $f from $SOURCE_REF"
    else
        echo "$LOG_PREFIX ⚠️ $f not present in $SOURCE_REF — staging empty (new file?)" >&2
        : > "$STAGE/$f"
    fi
done

# ── Apply the caller's transform ──
# The transform receives the STAGE directory as argv[1] and must READ the
# authoritative copies there and REWRITE the target files in place.
echo "$LOG_PREFIX applying transform: $(basename "$APPLY")"
python3 "$APPLY" "$STAGE"

# ── Validate every edited .json is well-formed (refuse to publish corruption) ──
echo "$LOG_PREFIX validating JSON..."
for f in "${FILE_LIST[@]}"; do
    if [[ "$f" == *.json ]]; then
        python3 -c 'import json,sys; json.load(open(sys.argv[1]))' "$STAGE/$f" \
            || { echo "$LOG_PREFIX ❌ $f is NOT valid JSON after transform — aborting (no publish)." >&2; exit 1; }
        echo "$LOG_PREFIX   ✓ $f parses"
    fi
done

# ── DRY RUN: stop before touching git ──
if $DRY_RUN; then
    echo "$LOG_PREFIX [DRY-RUN] staged + validated OK. Diff vs $SOURCE_REF:"
    for f in "${FILE_LIST[@]}"; do
        if git cat-file -e "$SOURCE_REF:$f" 2>/dev/null; then
            echo "--- $f ---"
            diff <(git show "$SOURCE_REF:$f") "$STAGE/$f" | head -40 || true
        else
            echo "--- $f (new) --- (no previous copy)"
        fi
    done
    echo "$LOG_PREFIX [DRY-RUN] no git changes made."
    exit 0
fi

# ── Save current working-tree data files (collect cron accumulates SOC/history here) ──
RM_DIR=$(mktemp -d /tmp/energy-publish-restore-XXXXXX)
trap 'rm -rf "$STAGE" "$RM_DIR"' EXIT
for f in "${FILE_LIST[@]}"; do
    mkdir -p "$RM_DIR/$(dirname "$f")"
    if [[ -f "$f" ]]; then cp "$f" "$RM_DIR/$f" 2>/dev/null || true; fi
done

# ── Switch to data branch, lay edited files in, commit, push (mirror sibling wrappers) ──
git checkout -f data 2>/dev/null || { git branch -D data 2>/dev/null; git checkout -b data; }
git reset --hard origin/data 2>/dev/null || true

for f in "${FILE_LIST[@]}"; do
    mkdir -p "$(dirname "$f")"
    cp "$STAGE/$f" "$f"
done

git add ${FILES}
if git diff --cached --quiet; then
    echo "$LOG_PREFIX No content change from $SOURCE_REF — skipping commit/push."
else
    git commit -m "[energy-publish] $MESSAGE"
    git push --force-with-lease origin data 2>&1 || {
        echo "$LOG_PREFIX ⚠️ force-with-lease rejected (concurrent push) — reset+retry" >&2
        git reset --hard origin/data
        git add ${FILES}
        git commit -m "[energy-publish] $MESSAGE (retry)" || true
        git push --force origin data
    }
    echo "$LOG_PREFIX ✅ pushed to data branch"
fi

# ── Back to main; restore accumulated working-tree data files ──
git checkout -f main
for f in "${FILE_LIST[@]}"; do
    if [[ -f "$RM_DIR/$f" ]]; then cp "$RM_DIR/$f" "$f" 2>/dev/null || true; fi
done

# ── Verify the pushed blob served via the data ref ──
git fetch origin data -q 2>/dev/null || true
for f in "${FILE_LIST[@]}"; do
    if git cat-file -e "origin/data:$f" 2>/dev/null; then
        if diff -q <(git show "origin/data:$f") "$STAGE/$f" >/dev/null 2>&1; then
            echo "$LOG_PREFIX ✓ origin/data:$f matches edited content"
        else
            echo "$LOG_PREFIX ⚠️ origin/data:$f differs from stage (CDN/rebase lag?)" >&2
        fi
    else
        echo "$LOG_PREFIX ⚠️ origin/data:$f not visible after push" >&2
    fi
done

echo "$LOG_PREFIX Done."