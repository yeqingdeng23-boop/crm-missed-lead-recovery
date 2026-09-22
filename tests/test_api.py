import os
from fastapi.testclient import TestClient
from automation.fixtures import payload


def headers(role, key=None):
    result = {"X-API-Key": os.environ[role + "_KEY"]}
    if key: result["Idempotency-Key"] = key
    return result


def test_authenticated_webhook_and_separate_approval(api):
    with TestClient(api) as client:
        assert client.post("/webhooks/intake", json=payload()).status_code == 401
        response = client.post("/webhooks/intake", json=payload(), headers=headers("WEBHOOK", "test1"))
        assert response.status_code == 202
        job_id = response.json()["job_id"]
        assert client.get(f"/jobs/{job_id}").status_code == 401
        run = client.post(f"/jobs/{job_id}/run", headers=headers("OPERATOR"))
        assert run.status_code == 200 and run.json()["state"] == "awaiting_approval"
        decision = {"version": run.json()["version"], "reviewer": "api-test", "approve": True}
        assert client.post(f"/jobs/{job_id}/decision", json=decision, headers=headers("OPERATOR")).status_code == 401
        assert client.post(f"/jobs/{job_id}/decision", json=decision, headers=headers("APPROVAL")).json()["state"] == "approved"
        assert client.post(f"/jobs/{job_id}/decision", json=decision, headers=headers("APPROVAL")).status_code == 409


def test_bad_payload_and_size_limit(api):
    with TestClient(api) as client:
        assert client.post("/webhooks/intake", json={"not": "valid"}, headers=headers("WEBHOOK", "bad1")).status_code == 422
        assert client.post("/webhooks/intake", content=b"x" * 512001, headers=headers("WEBHOOK", "large1")).status_code == 413


def test_webhook_replay_deduplicated(api):
    with TestClient(api) as client:
        a = client.post("/webhooks/intake", json=payload(), headers=headers("WEBHOOK", "same"))
        b = client.post("/webhooks/intake", json=payload(), headers=headers("WEBHOOK", "same"))
        assert a.json()["job_id"] == b.json()["job_id"] and not b.json()["created"]
