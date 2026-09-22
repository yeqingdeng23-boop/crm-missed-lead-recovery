from datetime import datetime, timezone, timedelta
from .engine import canonical


def payload():
    return {"lead_id": "synthetic-lead-001"}


def seed(store):
    created = datetime.fromtimestamp(store.clock(), timezone.utc) - timedelta(minutes=90)
    record = {"email": "sam@harbor.example", "owner": "sales-queue", "created_at": created.isoformat(),
              "message": "We want to purchase the CRM integration package. Please arrange a quotation.",
              "sla_minutes": 30, "last_human_contact_at": None, "opted_out": False,
              "consent_to_contact": True}
    with store.connect() as db:
        db.execute("INSERT OR IGNORE INTO records(record_key,data,version) VALUES (?,?,1)",
                   ("synthetic-lead-001", canonical(record)))


def model_answer():
    return {"intent": "purchase", "evidence": "We want to purchase the CRM integration package"}
