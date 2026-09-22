import json
import threading
from concurrent.futures import ThreadPoolExecutor
import pytest
from automation.engine import Engine, Conflict
from automation.fixtures import payload


def effects(store):
    with store.connect() as db:
        return db.execute("SELECT count(*) FROM effects").fetchone()[0]


def test_dedup_and_changed_body_conflict(store):
    first, created = store.enqueue("event1", payload())
    duplicate, created_again = store.enqueue("event1", payload())
    assert created and not created_again and first == duplicate
    with pytest.raises(Conflict): store.enqueue("event1", {**payload(), "changed": True})


def test_no_effect_before_approval_then_exactly_once(store):
    job, _ = store.enqueue("event1", payload())
    result = store.run(job)
    assert result["state"] == "awaiting_approval" and effects(store) == 0
    store.approve(job, result["version"], "test-reviewer")
    assert effects(store) == 1
    with pytest.raises(Conflict): store.approve(job, result["version"], "test-reviewer")
    assert effects(store) == 1


def test_reject_has_no_effect(store):
    job, _ = store.enqueue("event1", payload()); result = store.run(job)
    assert store.approve(job, result["version"], "test-reviewer", False)["state"] == "rejected"
    assert effects(store) == 0


def test_cannot_approve_unprocessed_job(store):
    job, _ = store.enqueue("event1", payload())
    with pytest.raises(Conflict): store.approve(job, 0, "test-reviewer")
    assert effects(store) == 0


def test_bounded_retry_dead_letter_and_redacted_logs(store):
    def fails(_): raise ValueError("PRIVATE_MESSAGE_DO_NOT_LOG")
    store.processor = fails
    job, _ = store.enqueue("event1", payload())
    assert [store.run(job)["state"] for _ in range(3)] == ["retry", "retry", "dead_letter"]
    with pytest.raises(Conflict): store.run(job)
    assert effects(store) == 0
    assert "PRIVATE_MESSAGE" not in json.dumps(store.events(job))


def test_transient_failure_recovers(store):
    real = store.processor; attempts = []
    def flaky(data):
        attempts.append(1)
        if len(attempts) == 1: raise TimeoutError()
        return real(data)
    store.processor = flaky
    job, _ = store.enqueue("event1", payload())
    assert store.run(job)["state"] == "retry"
    assert store.run(job)["state"] == "awaiting_approval"
    assert store.get(job)["attempts"] == 2


def test_backoff_enforced(store):
    store.backoff = 60
    store.processor = lambda _: (_ for _ in ()).throw(TimeoutError())
    job, _ = store.enqueue("event1", payload()); store.run(job)
    with pytest.raises(Conflict): store.run(job)


def test_restart_and_expired_lease_recovery(store):
    job, _ = store.enqueue("event1", payload())
    with store.connect() as db:
        db.execute("UPDATE jobs SET state='processing',attempts=1,version=1,lease_until=0 WHERE id=?", (job,))
    restarted = Engine(store.path, store.processor, backoff=0)
    result = restarted.run(job)
    assert result["state"] == "awaiting_approval" and result["attempts"] == 2
    assert any(e["details"].get("recovered_lease") for e in restarted.events(job))


def test_concurrent_worker_cannot_double_claim(store):
    entered, release = threading.Event(), threading.Event()
    process = store.processor
    def slow(data):
        entered.set(); release.wait(5)
        return process(data)
    store.processor = slow
    job, _ = store.enqueue("event1", payload())
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(store.run, job)
        try:
            assert entered.wait(3)
            with pytest.raises(Conflict): store.run(job)
        finally:
            release.set()
        assert result.result()["state"] == "awaiting_approval"


def test_stale_review_version_rejected(store):
    job, _ = store.enqueue("event1", payload()); result = store.run(job)
    with pytest.raises(Conflict): store.approve(job, result["version"] - 1, "test-reviewer")
    assert effects(store) == 0
