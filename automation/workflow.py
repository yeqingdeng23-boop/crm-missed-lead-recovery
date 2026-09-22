from datetime import datetime, timezone
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field

TITLE = "CRM Missed-Lead Recovery"


class Intake(BaseModel):
    model_config = ConfigDict(extra="forbid")
    lead_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,80}$")


class Analysis(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["purchase", "support", "other"]
    evidence: str = Field(min_length=4, max_length=240)


def aware(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone_required")
    return result


class Processor:
    def __init__(self, model, store):
        self.model, self.store = model, store

    def __call__(self, payload):
        lead_id = Intake.model_validate(payload).lead_id
        current = self.store.record(lead_id)
        if current is None:
            raise ValueError("crm_record_not_found")
        lead = current["data"]
        if lead.get("last_human_contact_at") or lead.get("opted_out") or not lead.get("consent_to_contact"):
            return {"skip": True, "reason": "handled_or_ineligible", "record_key": lead_id}
        if lead.get("recovery_task"):
            return {"skip": True, "reason": "recovery_already_queued", "record_key": lead_id}
        age_minutes = (datetime.fromtimestamp(self.store.clock(), timezone.utc) - aware(lead["created_at"])).total_seconds() / 60
        if age_minutes < lead.get("sla_minutes", 30):
            return {"skip": True, "reason": "sla_not_breached", "record_key": lead_id}
        data, provenance = self.model.extract(
            "Classify the enquiry: purchase (wants to buy), support (existing customer support), or other. "
            "evidence must be an exact quote from the enquiry.", lead["message"], Analysis.model_json_schema())
        analysis = Analysis.model_validate(data)
        if analysis.evidence not in lead["message"]:
            raise ValueError("ungrounded_evidence")
        if analysis.intent != "purchase":
            return {"skip": True, "reason": "not_purchase_intent", "record_key": lead_id, "inference": provenance}
        record = {**lead, "recovery_task": {"type": "SLA_BREACH", "owner": lead["owner"],
                  "overdue_minutes": round(age_minutes - lead.get("sla_minutes", 30), 2),
                  "reply_draft": "Thank you for your enquiry. A team member will review the details and follow up.",
                  "draft_only": True}, "analysis": analysis.model_dump()}
        return {"record_key": lead_id, "expected_record_version": current["version"],
                "requires_unhandled": True, "record": record, "action": "local_crm_recovery_task",
                "approval_required": True, "inference": provenance, "external_emails_sent": 0}
