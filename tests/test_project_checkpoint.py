import importlib.util
import json
import os
import stat
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


def load_module():
    root = Path(__file__).resolve().parents[1]
    path = root / "skills" / "project-checkpoint" / "scripts" / "project_checkpoint.py"
    name = f"project_checkpoint_test_{uuid.uuid4().hex}"
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def complete_capsule(path: Path) -> dict:
    capsule = json.loads(path.read_text())
    capsule.update(
        {
            "status": "complete",
            "objective": "Finish the checkpoint lifecycle implementation and open a reviewed PR.",
            "current_state": "Implementation is local on a dedicated branch and has not been deployed.",
            "source_of_truth": [
                {"type": "issue", "ref": "https://github.com/example/repo/issues/1", "status": "open"}
            ],
            "completed": ["Added the provider-neutral capsule schema."],
            "decisions": [
                {
                    "decision": "Keep policy in a shared skill.",
                    "reason": "Codex and Claude must read the same maintained instructions.",
                }
            ],
            "knowledge": [
                {
                    "fact": "The bridge runs one prompt at a time.",
                    "source": "queue worker inspection",
                    "revalidate": False,
                }
            ],
            "configuration": [
                {
                    "name": "HERMES_API_KEY",
                    "purpose": "Authenticate the retained gateway.",
                    "location": "protected service environment",
                    "secret_value_recorded": False,
                }
            ],
            "validation": [{"check": "pytest", "result": "passed targeted tests"}],
            "next_actions": [
                {"action": "Run the complete regression suite.", "owner": "next agent", "priority": "P0"}
            ],
        }
    )
    path.write_text(json.dumps(capsule, indent=2) + "\n")
    return capsule


def test_prepare_validate_commit_and_latest_round_trip(tmp_path):
    module = load_module()
    project = tmp_path / "repo"
    project.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "work.txt").write_text("in progress\n")

    args = type(
        "Args",
        (),
        {
            "project": str(project),
            "runtime": "codex",
            "bridge": "test-bridge",
            "session_id": "session-1",
            "note": "Preserve the reasoning.",
            "draft_dir": str(tmp_path / "drafts"),
        },
    )()
    prepared = module.cmd_prepare(args)
    draft = Path(prepared["draft"])
    complete_capsule(draft)
    module.validate_capsule(module._load_capsule(draft))

    commit_args = type(
        "Args",
        (),
        {"draft": str(draft), "store_root": str(tmp_path / "store"), "allow_older": False},
    )()
    committed = module.cmd_commit(commit_args)
    latest = Path(committed["latest_json"])

    assert latest.exists()
    assert Path(committed["latest_markdown"]).exists()
    assert "Decisions And Reasons" in Path(committed["latest_markdown"]).read_text()
    assert stat.S_IMODE(latest.stat().st_mode) == 0o600

    latest_args = type(
        "Args",
        (),
        {"project": str(project), "store_root": str(tmp_path / "store"), "format": "path"},
    )()
    assert module.cmd_latest(latest_args) == str(latest)


def test_validator_rejects_secret_values(tmp_path):
    module = load_module()
    project = tmp_path / "repo"
    project.mkdir()
    args = type(
        "Args",
        (),
        {
            "project": str(project),
            "runtime": "claude",
            "bridge": "test-bridge",
            "session_id": None,
            "note": "",
            "draft_dir": str(tmp_path),
        },
    )()
    draft = Path(module.cmd_prepare(args)["draft"])
    capsule = complete_capsule(draft)
    capsule["notes"] = "OPENAI_API_KEY=sk-example_abcdefghijklmnopqrstuvwxyz"

    with pytest.raises(module.CheckpointError, match="likely secret"):
        module.validate_capsule(capsule)


def test_validator_rejects_deterministic_snapshot_tampering(tmp_path):
    module = load_module()
    project = tmp_path / "repo"
    project.mkdir()
    args = type(
        "Args",
        (),
        {
            "project": str(project),
            "runtime": "codex",
            "bridge": "test-bridge",
            "session_id": None,
            "note": "",
            "draft_dir": str(tmp_path),
        },
    )()
    draft = Path(module.cmd_prepare(args)["draft"])
    capsule = complete_capsule(draft)
    capsule["project"]["git"]["branch"] = "invented-branch"

    with pytest.raises(module.CheckpointError, match="snapshot was modified"):
        module.validate_capsule(capsule)


def test_loader_rejects_oversized_capsule(tmp_path):
    module = load_module()
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * (module.MAX_CAPSULE_BYTES + 1))

    with pytest.raises(module.CheckpointError, match="safety limit"):
        module._load_capsule(oversized)
