"""Local-only API and durable worker. Credentials stay in ignored var/."""
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import uvicorn

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main():
    state = ROOT / "var"; state.mkdir(exist_ok=True)
    key_file = state / "local-keys.json"
    if key_file.exists():
        keys = json.loads(key_file.read_text())
    else:
        keys = {r + "_KEY": secrets.token_urlsafe(32) for r in ("WEBHOOK", "OPERATOR", "APPROVAL")}
        key_file.write_text(json.dumps(keys), encoding="utf8")
    os.environ.update(keys)
    os.environ["DB_PATH"] = str(state / "workflow.sqlite3")
    from automation.api import create_app
    from automation.fixtures import seed
    app = create_app()
    store = app.state.store
    seed(store)
    stop = threading.Event()

    def work():
        while not stop.is_set():
            for job_id in store.due():
                if stop.is_set():
                    break
                try:
                    store.run(job_id)
                except Exception as exc:
                    print(type(exc).__name__, flush=True)
            stop.wait(1)

    worker = threading.Thread(target=work, daemon=True)
    worker.start()
    print("API docs: http://127.0.0.1:8010/docs", flush=True)
    print("Separate role keys: var/local-keys.json. Synthetic data only.", flush=True)
    try:
        uvicorn.run(app, host="127.0.0.1", port=8010, access_log=False,
                    timeout_graceful_shutdown=5)
    finally:
        stop.set()
        worker.join(timeout=2)
        # An interrupted inference remains leased and recovers after lease expiry.


if __name__ == "__main__":
    main()
