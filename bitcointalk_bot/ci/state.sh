#!/usr/bin/env bash
# Load / save the bot's memory (seen.json) from / to its own git branch.
#
#   bash bitcointalk_bot/ci/state.sh load   # before the first check
#   bash bitcointalk_bot/ci/state.sh save   # after every check (skips if nothing changed)
#
# The memory lives on branch $STATE_BRANCH (default "bitcointalk-bot-state") so it never
# touches your normal commits. Saving MERGES with the remote copy (union), so even if two
# runs ever overlapped, nothing is forgotten and no coin is posted twice.
set -euo pipefail

BOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
STATE_BRANCH="${STATE_BRANCH:-bitcointalk-bot-state}"
STATE_DIR="$BOT_DIR/state"
STATE_REMOTE="${STATE_REMOTE:-https://x-access-token:${GH_TOKEN:?GH_TOKEN not set}@github.com/${GITHUB_REPOSITORY:?GITHUB_REPOSITORY not set}.git}"
HASH_FILE="${RUNNER_TEMP:-/tmp}/bitcointalk-bot-state.sha256"

state_hash() {
  if [ -f "$STATE_DIR/seen.json" ]; then sha256sum < "$STATE_DIR/seen.json" | cut -d' ' -f1; else echo none; fi
}

load() {
  mkdir -p "$STATE_DIR"
  local work; work=$(mktemp -d)
  git -C "$work" init -q
  if git -C "$work" fetch -q --depth=1 "$STATE_REMOTE" "$STATE_BRANCH" 2>/dev/null; then
    git -C "$work" show "FETCH_HEAD:seen.json"     > "$STATE_DIR/seen.json"     2>/dev/null || rm -f "$STATE_DIR/seen.json"
    git -C "$work" show "FETCH_HEAD:seen.json.bak" > "$STATE_DIR/seen.json.bak" 2>/dev/null || rm -f "$STATE_DIR/seen.json.bak"
    echo "Loaded bot memory from branch $STATE_BRANCH"
  else
    echo "No state branch yet - the first check will only remember the current topics"
  fi
  rm -rf "$work"
  state_hash > "$HASH_FILE"
}

save() {
  [ -f "$STATE_DIR/seen.json" ] || { echo "Nothing to save yet"; return 0; }
  if [ "$(state_hash)" = "$(cat "$HASH_FILE" 2>/dev/null)" ]; then
    echo "Memory unchanged - nothing to save"; return 0
  fi
  local work i
  work=$(mktemp -d)
  for i in 1 2 3 4 5; do
    rm -rf "$work" && mkdir -p "$work"
    (
      cd "$work"
      git init -q
      git config user.name  "github-actions[bot]"
      git config user.email "github-actions[bot]@users.noreply.github.com"
      if git fetch -q --depth=1 "$STATE_REMOTE" "$STATE_BRANCH" 2>/dev/null; then
        git checkout -q -B "$STATE_BRANCH" FETCH_HEAD
        # union the remote copy into ours so nothing is ever forgotten
        python "$BOT_DIR/bot.py" --merge-state "$work/seen.json"
      else
        git checkout -q --orphan "$STATE_BRANCH"
      fi
      cp "$STATE_DIR/seen.json" seen.json
      [ -f "$STATE_DIR/seen.json.bak" ] && cp "$STATE_DIR/seen.json.bak" seen.json.bak
      git add seen.json
      [ -f seen.json.bak ] && git add seen.json.bak
      if git diff --cached --quiet; then echo "No change"; exit 0; fi
      git commit -q -m "bot memory update [skip ci]"
      git push -q "$STATE_REMOTE" "HEAD:refs/heads/$STATE_BRANCH"
      echo "Saved bot memory to branch $STATE_BRANCH"
    ) && { state_hash > "$HASH_FILE"; rm -rf "$work"; return 0; }
    echo "Save attempt $i failed (push race / network), retrying"
    sleep $((i * 3))
  done
  rm -rf "$work"
  echo "::error::Could not save bot memory"
  return 1
}

case "${1:-}" in
  load) load ;;
  save) save ;;
  *) echo "usage: $0 load|save" >&2; exit 2 ;;
esac
