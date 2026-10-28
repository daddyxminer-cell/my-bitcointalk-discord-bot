#!/usr/bin/env bash
# One "leg" of the 24/7 relay on GitHub Actions.
#
# GitHub kills any job after 6 hours, so instead of one endless job the bot runs in legs:
# this script checks the board every CHECK_INTERVAL_MINUTES for RUN_MINUTES (default 5.5 h),
# saving the bot's memory after every check. The workflow then starts the next leg, which
# loads that memory and carries on - nothing is missed and nothing is posted twice.
set -uo pipefail

CI_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INTERVAL_MIN="${CHECK_INTERVAL_MINUTES:-15}"
RUN_MIN="${RUN_MINUTES:-330}"
CHECK_TIMEOUT_MIN="${CHECK_TIMEOUT_MINUTES:-12}"
# shellcheck disable=SC2206
EXTRA_ARGS=(${BOT_EXTRA_ARGS:-})

end=$(( $(date +%s) + RUN_MIN * 60 ))
n=0
echo "Relay leg: checking every ${INTERVAL_MIN} min for ${RUN_MIN} min"
while :; do
  start=$(date +%s)
  n=$((n + 1))
  echo "::group::Check #$n at $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
  timeout "${CHECK_TIMEOUT_MIN}m" python "$CI_DIR/../bot.py" --once "${EXTRA_ARGS[@]}" \
    || echo "::warning::Check #$n failed (exit $?) - retrying at the next interval"
  bash "$CI_DIR/state.sh" save || echo "::warning::Could not save memory after check #$n"
  echo "::endgroup::"

  next=$(( start + INTERVAL_MIN * 60 ))
  (( next < end )) || break
  wait=$(( next - $(date +%s) ))
  (( wait > 0 )) && sleep "$wait"
done
echo "Relay leg finished after $n checks - handing over to the next run"
