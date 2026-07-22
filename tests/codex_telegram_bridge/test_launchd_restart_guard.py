import os
import subprocess
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[2]
CLEAR_SCRIPT = REPO_ROOT / "scripts" / "clear-stale-bridge-restart-jobs.sh"
RESTART_SCRIPT = REPO_ROOT / "scripts" / "restart-codex-bridge-when-idle.sh"


def write_executable(path: Path, content: str) -> None:
    path.write_text(content)
    path.chmod(0o755)


def test_clear_script_removes_only_known_transient_jobs(tmp_path):
    removed = tmp_path / "removed"
    fake_launchctl = tmp_path / "launchctl"
    write_executable(
        fake_launchctl,
        """#!/bin/bash
if [[ "$1" == "list" ]]; then
  cat <<'EOF'
101\t0\tcom.ella.codex-bridge
102\t0\tcom.ella.codex-bridge.ios
103\t0\tcom.ella.codex-bridge.restart-once.1
104\t0\tcom.ella.codex-bridge.force-restart-once.2
105\t0\tcom.ella.codex-bridge.retired-target-reload.henry.3
106\t0\tcom.example.unrelated
EOF
elif [[ "$1" == "remove" ]]; then
  echo "$2" >> "$REMOVED_FILE"
else
  exit 9
fi
""",
    )
    env = os.environ | {
        "LAUNCHCTL_BIN": str(fake_launchctl),
        "REMOVED_FILE": str(removed),
    }

    result = subprocess.run([str(CLEAR_SCRIPT)], env=env, text=True, capture_output=True)

    assert result.returncode == 0
    assert removed.read_text().splitlines() == [
        "com.ella.codex-bridge.restart-once.1",
        "com.ella.codex-bridge.force-restart-once.2",
        "com.ella.codex-bridge.retired-target-reload.henry.3",
    ]
    assert "com.ella.codex-bridge.ios" not in result.stderr


def test_idle_restart_helper_waits_for_empty_queue_then_kickstarts_once(tmp_path):
    calls = tmp_path / "calls"
    curl_count = tmp_path / "curl-count"
    fake_launchctl = tmp_path / "launchctl"
    fake_curl = tmp_path / "curl"
    write_executable(
        fake_launchctl,
        """#!/bin/bash
echo "launchctl $*" >> "$CALLS_FILE"
if [[ "$1" == "list" ]]; then
  printf '101\\t0\\tcom.ella.codex-bridge\\n'
elif [[ "$1" != "kickstart" ]]; then
  exit 9
fi
""",
    )
    write_executable(
        fake_curl,
        """#!/bin/bash
count=0
[[ -f "$CURL_COUNT_FILE" ]] && count=$(cat "$CURL_COUNT_FILE")
count=$((count + 1))
echo "$count" > "$CURL_COUNT_FILE"
echo "curl $count" >> "$CALLS_FILE"
if [[ "$count" == "1" ]]; then
  printf '{"status":"ok","queue":{"busy":false,"runs":1,"events":2}}\\n'
else
  printf '{"status":"ok","queue":{"busy":false,"runs":0,"events":0}}\\n'
fi
""",
    )
    env = os.environ | {
        "LAUNCHCTL_BIN": str(fake_launchctl),
        "CURL_BIN": str(fake_curl),
        "CALLS_FILE": str(calls),
        "CURL_COUNT_FILE": str(curl_count),
    }

    result = subprocess.run(
        [str(RESTART_SCRIPT), "--timeout", "1", "--poll", "0"],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 0, result.stderr
    operation_calls = calls.read_text().splitlines()
    assert operation_calls == [
        "launchctl list",
        "curl 1",
        "curl 2",
        f"launchctl kickstart -k gui/{os.getuid()}/com.ella.codex-bridge",
        "curl 3",
    ]
    assert all("submit" not in call for call in operation_calls)


def test_idle_restart_helper_rejects_unsafe_service_label():
    result = subprocess.run(
        [str(RESTART_SCRIPT), "--service", "bad;label"],
        text=True,
        capture_output=True,
    )

    assert result.returncode == 2
    assert "Invalid launchd service label" in result.stderr


def test_idle_restart_helper_times_out_without_kickstart_when_runs_are_pending(tmp_path):
    calls = tmp_path / "calls"
    fake_launchctl = tmp_path / "launchctl"
    fake_curl = tmp_path / "curl"
    write_executable(
        fake_launchctl,
        """#!/bin/bash
echo "$*" >> "$CALLS_FILE"
if [[ "$1" == "list" ]]; then
  printf '101\\t0\\tcom.ella.codex-bridge\\n'
elif [[ "$1" == "kickstart" ]]; then
  exit 99
fi
""",
    )
    write_executable(
        fake_curl,
        """#!/bin/bash
printf '{"status":"ok","queue":{"busy":false,"runs":1,"events":1}}\\n'
""",
    )
    env = os.environ | {
        "LAUNCHCTL_BIN": str(fake_launchctl),
        "CURL_BIN": str(fake_curl),
        "CALLS_FILE": str(calls),
    }

    result = subprocess.run(
        [str(RESTART_SCRIPT), "--timeout", "0", "--poll", "0"],
        env=env,
        text=True,
        capture_output=True,
    )

    assert result.returncode == 3
    assert "no restart performed" in result.stderr
    assert all("kickstart" not in call for call in calls.read_text().splitlines())
