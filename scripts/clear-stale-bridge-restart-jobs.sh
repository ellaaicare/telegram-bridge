#!/usr/bin/env bash
set -uo pipefail

LAUNCHCTL_BIN="${LAUNCHCTL_BIN:-launchctl}"

list_output="$("${LAUNCHCTL_BIN}" list 2>/dev/null)" || exit 0
failures=0

while IFS= read -r label; do
  [[ -n "${label}" ]] || continue
  case "${label}" in
    com.ella.codex-bridge.restart-once.*|\
    com.ella.codex-bridge.force-restart-once.*|\
    com.ella.codex-bridge.retired-target-reload.*)
      if "${LAUNCHCTL_BIN}" remove "${label}" >/dev/null 2>&1; then
        printf '[bridge-startup] removed stale transient launchd job: %s\n' "${label}" >&2
      else
        printf '[bridge-startup] failed to remove stale transient launchd job: %s\n' "${label}" >&2
        failures=1
      fi
      ;;
  esac
done < <(printf '%s\n' "${list_output}" | awk '{print $3}')

exit "${failures}"
