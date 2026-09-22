"""Durable workflow: ingest -> claim -> process -> review -> atomic local commit.

SQLite is an explicitly synthetic CRM/output sink, not a claimed vendor integration.
No emails are sent. Approval and worker credentials are separate.
"""
import hashlib
import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path


class Conflict(RuntimeError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


class Engine:
    def __init__(self, path, processor, clock=time.time, backoff=2, max_attempts=3):
        self.path, self.processor, self.clock = str(path), processor, clock
        self.backoff, self.max_attempts = backoff, max_attempts
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, request_key TEXT UNIQUE NOT NULL,
                body_hash TEXT NOT NULL, payload TEXT NOT NULL, state TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, version INTEGER NOT NULL DEFAULT 0,
                next_at REAL NOT NULL DEFAULT 0, lease_until REAL NOT NULL DEFAULT 0,
                output TEXT, error_code TEXT, created_at REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS events (
                seq INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
                at REAL NOT NULL, event TEXT NOT NULL, details TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS records (
                record_key TEXT PRIMARY KEY, data TEXT NOT NULL, version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS effects (
                job_id TEXT PRIMARY KEY, record_key TEXT NOT NULL,
                action TEXT NOT NULL, at REAL NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        try:
            with db:
                yield db
        finally:
            db.close()

    def event(self, db, job_id, event, **details):
        # Operational events contain IDs/status only, not message bodies or addresses.
        db.execute("INSERT INTO events(job_id,at,event,details) VALUES (?,?,?,?)",
                   (job_id, self.clock(), event, canonical(details)))

    def enqueue(self, key, payload):
        raw = canonical(payload)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            prior = db.execute("SELECT * FROM jobs WHERE request_key=?", (key,)).fetchone()
            if prior:
                if prior["body_hash"] != digest:
                    raise Conflict("idempotency_key_payload_mismatch")
                return prior["id"], False
            job_id = str(uuid.uuid4())
            db.execute("INSERT INTO jobs(id,request_key,body_hash,payload,state,created_at) VALUES (?,?,?,?,?,?)",
                       (job_id, key, digest, raw, "queued", self.clock()))
            self.event(db, job_id, "accepted")
            return job_id, True

    def get(self, job_id):
        with self.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        result = dict(row)
        result["payload"] = json.loads(result["payload"])
        result["output"] = json.loads(result["output"]) if result["output"] else None
        return result

    def run(self, job_id):
        now = self.clock()
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                raise KeyError(job_id)
            runnable = row["state"] in ("queued", "retry") and row["next_at"] <= now
            expired = row["state"] == "processing" and row["lease_until"] <= now
            if not (runnable or expired):
                raise Conflict("job_not_ready")
            if row["attempts"] >= self.max_attempts:
                db.execute("UPDATE jobs SET state='dead_letter',version=version+1 WHERE id=?", (job_id,))
                self.event(db, job_id, "attempts_exhausted")
                return {"id": job_id, "state": "dead_letter"}
            attempt = row["attempts"] + 1
            claim = row["version"] + 1
            db.execute("UPDATE jobs SET state='processing',attempts=?,version=?,lease_until=? WHERE id=?",
                       (attempt, claim, now + 300, job_id))
            self.event(db, job_id, "processing_started", attempt=attempt, recovered_lease=expired)
            payload = json.loads(row["payload"])
        try:
            output = self.processor(payload)
            encoded = canonical(output)
            state, error = ("skipped" if output.get("skip") else "awaiting_approval"), None
        except Exception as exc:
            # Persist a bounded error class only: exceptions may contain sensitive input.
            encoded = None
            error = type(exc).__name__
            state = "dead_letter" if attempt >= self.max_attempts else "retry"
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE jobs SET state=?,output=?,error_code=?,version=version+1,"
                                 "next_at=?,lease_until=0 WHERE id=? AND state='processing' AND version=?",
                                 (state, encoded, error, self.clock() + self.backoff * 2 ** (attempt - 1),
                                  job_id, claim)).rowcount
            if changed != 1:
                raise Conflict("processing_lease_replaced")
            self.event(db, job_id, state, attempt=attempt, error_code=error)
        return self.get(job_id)

    def approve(self, job_id, expected_version, reviewer, approve=True):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            job = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job["state"] != "awaiting_approval" or job["version"] != expected_version:
                raise Conflict("stale_or_non_reviewable_job")
            output = json.loads(job["output"])
            if not approve:
                state = "rejected"
            else:
                key = output["record_key"]
                current = db.execute("SELECT * FROM records WHERE record_key=?", (key,)).fetchone()
                current_version = current["version"] if current else 0
                if current_version != output.get("expected_record_version", 0):
                    raise Conflict("record_changed_since_review")
                # Recovery rechecks latest state in the same transaction as the effect.
                if output.get("requires_unhandled") and current:
                    record = json.loads(current["data"])
                    if record.get("last_human_contact_at") or record.get("opted_out"):
                        raise Conflict("lead_already_handled_or_opted_out")
                db.execute("INSERT INTO records(record_key,data,version) VALUES (?,?,?) "
                           "ON CONFLICT(record_key) DO UPDATE SET data=excluded.data,version=excluded.version",
                           (key, canonical(output["record"]), current_version + 1))
                db.execute("INSERT INTO effects(job_id,record_key,action,at) VALUES (?,?,?,?)",
                           (job_id, key, output["action"], self.clock()))
                state = "approved"
            db.execute("UPDATE jobs SET state=?,version=version+1 WHERE id=?", (state, job_id))
            self.event(db, job_id, state, reviewer=reviewer)
        return self.get(job_id)

    def record(self, key):
        with self.connect() as db:
            row = db.execute("SELECT * FROM records WHERE record_key=?", (key,)).fetchone()
        return {"data": json.loads(row["data"]), "version": row["version"]} if row else None

    def events(self, job_id):
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE job_id=? ORDER BY seq", (job_id,)).fetchall()
        return [{**dict(r), "details": json.loads(r["details"])} for r in rows]

    def human_contact(self, key, expected_version, reviewer):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM records WHERE record_key=?", (key,)).fetchone()
            if row is None:
                raise KeyError(key)
            if row["version"] != expected_version:
                raise Conflict("stale_record")
            record = json.loads(row["data"])
            record["last_human_contact_at"] = self.clock()
            db.execute("UPDATE records SET data=?,version=version+1 WHERE record_key=?", (canonical(record), key))
            self.event(db, "record:" + key, "human_contact_recorded", reviewer=reviewer)
        return self.record(key)

    def due(self):
        with self.connect() as db:
            return [r[0] for r in db.execute(
                "SELECT id FROM jobs WHERE (state IN ('queued','retry') AND next_at<=?) "
                "OR (state='processing' AND lease_until<=?) ORDER BY created_at LIMIT 10",
                (self.clock(), self.clock()))]
