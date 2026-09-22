import pytest
from automation.engine import Conflict, canonical
from automation.workflow import Processor
from automation.fixtures import payload


def change(store, **changes):
    row = store.record("synthetic-lead-001")
    with store.connect() as db:
        db.execute("UPDATE records SET data=?,version=version+1 WHERE record_key=?",
                   (canonical({**row["data"], **changes}), "synthetic-lead-001"))


@pytest.mark.parametrize("changes", [{"opted_out": True}, {"consent_to_contact": False}, {"last_human_contact_at": "2026-09-22T00:00:00Z"}])
def test_ineligible_lead_skips_ai(store, fake_model, changes):
    change(store, **changes)
    assert Processor(fake_model, store)(payload())["skip"]
    assert fake_model.calls == 0


def test_no_alert_inside_sla(store, fake_model):
    change(store, sla_minutes=120)
    assert Processor(fake_model, store)(payload())["reason"] == "sla_not_breached"


def test_human_contact_after_draft_blocks_stale_approval(store):
    job, _ = store.enqueue("r1", payload()); result = store.run(job)
    row = store.record("synthetic-lead-001")
    store.human_contact("synthetic-lead-001", row["version"], "human-reviewer")
    with pytest.raises(Conflict): store.approve(job, result["version"], "stale-reviewer")
    assert "recovery_task" not in store.record("synthetic-lead-001")["data"]


def test_recovery_never_sends_and_not_duplicated(store):
    job, _ = store.enqueue("r1", payload()); result = store.run(job)
    assert result["output"]["external_emails_sent"] == 0
    store.approve(job, result["version"], "test-reviewer")
    other, _ = store.enqueue("r2", payload())
    assert store.run(other)["state"] == "skipped"
