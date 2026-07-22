#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
TARGET_HOME="${HOME}"

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo-root)
      REPO_ROOT="$(cd "$2" && pwd)"
      shift 2
      ;;
    --home)
      TARGET_HOME="$(cd "$2" && pwd)"
      shift 2
      ;;
    -h|--help)
      echo "Usage: $0 [--repo-root PATH] [--home PATH]"
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      exit 2
      ;;
  esac
done

SOURCE="${REPO_ROOT}/skills/project-checkpoint"
if [[ ! -f "${SOURCE}/SKILL.md" ]]; then
  echo "Project checkpoint skill not found: ${SOURCE}" >&2
  exit 1
fi

install_link() {
  local skill_root="$1"
  local target="${skill_root}/project-checkpoint"
  mkdir -p "${skill_root}"

  if [[ -L "${target}" ]]; then
    ln -sfn "${SOURCE}" "${target}"
  elif [[ -e "${target}" ]]; then
    echo "Refusing to replace non-symlink skill: ${target}" >&2
    return 1
  else
    ln -s "${SOURCE}" "${target}"
  fi
  echo "Installed ${target} -> ${SOURCE}"
}

install_link "${TARGET_HOME}/.codex/skills"
install_link "${TARGET_HOME}/.claude/skills"
