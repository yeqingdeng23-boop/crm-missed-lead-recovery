"""Submit one synthetic fixture to the local API; does not approve it."""
import json
from pathlib import Path
import sys
import urllib.request
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from automation.fixtures import payload

keys = json.loads((ROOT / "var/local-keys.json").read_text())
req = urllib.request.Request("http://127.0.0.1:8010/webhooks/intake", json.dumps(payload()).encode(),
    {"Content-Type": "application/json", "X-API-Key": keys["WEBHOOK_KEY"], "Idempotency-Key": str(uuid.uuid4())})
with urllib.request.urlopen(req, timeout=10) as response: print(response.read().decode())
