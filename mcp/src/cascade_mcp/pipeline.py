"""One monitored process group covers legacy inference, validation, and ZIP work."""
import json
import os
from pathlib import Path
import subprocess
import sys
from cascade_mcp.artifacts import build


def main():
    directory = Path(sys.argv[1])
    legacy, models, max_atoms = sys.argv[2:]
    env = dict(os.environ, PYTHONPATH=models)
    env.pop("PYTHONHOME", None)
    command = [legacy, str(Path(__file__).parent / "legacy_adapter.py"), "--request", str(directory / "input/request.json"),
               "--output", str(directory / "work"), "--max-atoms", max_atoms]
    code = subprocess.call(command, cwd=models, env=env, pass_fds=(int(env["CASCADE_LOCK_FD"]),))
    if code:
        return code
    try:
        request = json.loads((directory / "input/request.json").read_text())
        build(directory, request["nuclei"][0])
    except Exception:
        import traceback
        traceback.print_exc()
        return 21
    return 0


if __name__ == "__main__":
    sys.exit(main())
