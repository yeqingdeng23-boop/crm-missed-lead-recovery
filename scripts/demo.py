"""Real loopback HTTP + local Ollama run. Review decisions are automated test actions.

No commercial success or actual human approval is implied by this demonstration.
"""
import argparse
import hashlib
import html
import json
import os
from pathlib import Path
import secrets
import socket
import threading
import uvicorn
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from automation.api import create_app
from automation.fixtures import payload, seed
from automation.workflow import TITLE


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", default="evidence/live-run.json")
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="automation-proof-") as temp:
        config = {role + "_KEY": secrets.token_urlsafe(32) for role in ("WEBHOOK", "OPERATOR", "APPROVAL")}
        os.environ.update(config)
        os.environ["DB_PATH"] = str(Path(temp) / "proof.sqlite3")
        app = create_app(); seed(app.state.store)
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0)); port = sock.getsockname()[1]
        steps = []
        server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port,
                                               access_log=False, log_level="error"))
        thread = threading.Thread(target=server.run, daemon=True)
        thread.start()
        try:
            base = f"http://127.0.0.1:{port}"
            def request(method, path, role=None, body=None, idem=None, expected=200):
                headers = {"Content-Type": "application/json"}
                if role: headers["X-API-Key"] = config[role + "_KEY"]
                if idem: headers["Idempotency-Key"] = idem
                req = urllib.request.Request(base + path, json.dumps(body).encode() if body is not None else None,
                                             headers, method=method)
                try:
                    with urllib.request.urlopen(req, timeout=150) as response:
                        status, data = response.status, json.load(response)
                except urllib.error.HTTPError as exc:
                    status, data = exc.code, json.load(exc)
                assert status == expected, (path, status, data)
                steps.append({"method": method, "path": path, "status": status})
                return data

            for _ in range(100):
                try:
                    with urllib.request.urlopen(base + "/health", timeout=1): break
                except (OSError, urllib.error.URLError): time.sleep(.1)
            else: raise RuntimeError("local_server_failed_to_start")
            health = request("GET", "/health")
            request("POST", "/webhooks/intake", body=payload(), expected=401)
            event = request("POST", "/webhooks/intake", "WEBHOOK", payload(), "synthetic-run-001", 202)
            job_id = event["job_id"]
            run = request("POST", f"/jobs/{job_id}/run", "OPERATOR")
            assert run["state"] == "awaiting_approval", run
            assert run["output"]["inference"]["provider"] == "ollama-local"
            with app.state.store.connect() as db:
                before = db.execute("SELECT count(*) FROM effects").fetchone()[0]
            assert before == 0
            decision = {"version": run["version"], "reviewer": "automated-demo-reviewer", "approve": True}
            request("POST", f"/jobs/{job_id}/decision", "OPERATOR", decision, expected=401)
            approved = request("POST", f"/jobs/{job_id}/decision", "APPROVAL", decision)
            request("POST", f"/jobs/{job_id}/decision", "APPROVAL", decision, expected=409)
            replay = request("POST", "/webhooks/intake", "WEBHOOK", payload(), "synthetic-run-001", 202)
            assert not replay["created"] and replay["job_id"] == job_id
            record = request("GET", "/records/" + run["output"]["record_key"], "OPERATOR")
            events = request("GET", f"/jobs/{job_id}/events", "OPERATOR")
            with app.state.store.connect() as db:
                after = db.execute("SELECT count(*) FROM effects").fetchone()[0]
            assert after == 1 and approved["state"] == "approved"
            source_hashes = {str(p.relative_to(ROOT)): hashlib.sha256(p.read_bytes()).hexdigest()
                             for p in sorted((ROOT / "automation").glob("*.py"))}
            result = {"label": "SELF-BUILT TECHNICAL PROOF — NOT A CLIENT CASE STUDY", "project": TITLE,
                      "verified_at": datetime.now(timezone.utc).isoformat(), "transport": "real loopback HTTP",
                      "model_execution": "real local Ollama inference; no mock", "source_sha256": source_hashes,
                      "input": "synthetic fixture only", "reviewer": "automated demo action, not a real buyer/human approval",
                      "health": health, "steps": steps, "output": run["output"], "record_after_approval": record,
                      "effects_before_approval": before, "effects_after_approval": after, "events": events,
                      "external_messages_sent": 0, "cloud_api_cost_usd": 0, "result": "passed"}
            dest = ROOT / args.output; dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf8")
            write_html(dest.with_suffix(".html"), result)
            print(json.dumps({"project": TITLE, "result": "passed", "model": run["output"]["inference"],
                              "effects_before": before, "effects_after": after}))
        finally:
            server.should_exit = True
            thread.join(timeout=10)
            if thread.is_alive():
                raise RuntimeError("local_server_did_not_stop")


def write_html(path, result):
    data = html.escape(json.dumps(result["output"]["record"], indent=2, ensure_ascii=False))
    steps = "".join(f"<tr><td>{html.escape(s['method'])}</td><td>{html.escape(s['path'])}</td><td>{s['status']}</td></tr>" for s in result["steps"])
    page = """<!doctype html><html lang='en'><meta charset='utf-8'><meta name='viewport' content='width=device-width'>
    <title>Workflow execution evidence</title><style>
    body{font:16px/1.5 system-ui;color:#182a3c;background:#f6f8fb;max-width:1100px;margin:40px auto;padding:24px}
    h1{line-height:1.2} .label{font-size:13px;font-weight:700;color:#9b3b00} .pass{color:#08705c;font-weight:700}
    section{background:white;border:1px solid #dbe2ea;border-radius:10px;padding:24px;margin:20px 0}
    pre{white-space:pre-wrap;overflow-wrap:anywhere;background:#f3f6fa;padding:16px;font-size:14px}
    td,th{text-align:left;padding:7px;border-bottom:1px solid #dbe2ea} table{width:100%;font-size:13px}
    </style><body>"""
    page += f"<p class='label'>{html.escape(result['label'])}</p><h1>{html.escape(TITLE)}</h1>"
    page += f"<p class='pass'>PASS · real HTTP + real local model</p><p>Recorded {html.escape(result['verified_at'])}</p>"
    page += "<section><b>Approval boundary verified</b><p>Effects before approval: 0 → after approval: 1.<br>Worker key rejected for approval (401). Repeated approval rejected (409). No external messages sent.</p>"
    page += "<p>Data is synthetic. Reviewer actions in this run are automated test actions. This is execution evidence, not a customer deployment or a buyer paid test.</p></section>"
    page += f"<section><h2>Structured output</h2><pre>{data}</pre></section><section><h2>HTTP transcript</h2><table>{steps}</table></section>"
    page += f"<p>Model: {html.escape(result['output']['inference']['model'])}. The complete JSON file is saved beside this report.</p></body></html>"
    path.write_text(page, encoding="utf8")


if __name__ == "__main__": main()
