"""Admission control only; use Docker Compose directly for service operations."""
import argparse
import json
from cascade_mcp.jobs import ACTIVE, Store, atomic
from cascade_mcp.maintenance import tick
from cascade_mcp.settings import Settings


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("pause", "resume", "status", "maintenance-once"))
    args = parser.parse_args()
    store = Store(Settings())
    if args.action == "pause":
        with store.lock("admission"):
            atomic(store.root / ".admission-closed", {"paused": True})
    elif args.action == "maintenance-once":
        tick(store)
    elif args.action == "resume":
        tick(store)  # Corrupt records or failed recovery must not be ignored.
        with store.lock("admission"):
            (store.root / ".blocked").unlink(missing_ok=True)
            (store.root / ".admission-closed").unlink(missing_ok=True)
            try:
                store.ready()
            except Exception:
                atomic(store.root / ".admission-closed", {"paused": True})
                raise
    states = [store.load(i)["status"] for i in store.ids()]
    print(json.dumps({"admission_paused": (store.root / ".admission-closed").exists(),
                      "blocked": (store.root / ".blocked").exists(),
                      "active_jobs": sum(s in ACTIVE for s in states)}))


if __name__ == "__main__":
    main()
