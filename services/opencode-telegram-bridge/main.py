from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
import os
import sys


os.environ.setdefault("HARNESS_CLI", "opencode")
os.environ.setdefault("HARNESS_LABEL", "OpenCode")
os.environ.setdefault("HARNESS_SERVICE_NAME", "opencode-telegram-bridge")
os.environ.setdefault("HARNESS_SESSION_BACKEND", "bridge")
os.environ.setdefault(
    "BRIDGE_STATE_DIR",
    str(Path.home() / ".local" / "state" / "opencode-telegram-bridge"),
)

shared_main = Path(__file__).resolve().parents[1] / "claude-telegram-bridge" / "main.py"
spec = spec_from_file_location("shared_opencode_telegram_bridge", shared_main)
module = module_from_spec(spec)
assert spec.loader is not None
sys.modules[spec.name] = module
spec.loader.exec_module(module)

for name in dir(module):
    if name.startswith("__"):
        continue
    globals()[name] = getattr(module, name)
