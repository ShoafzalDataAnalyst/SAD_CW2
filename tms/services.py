"""Application layer: services that implement the use cases and business rules.

Each service corresponds to a class in the application package of the UML
architecture diagram. Routes (presentation layer) call these and never touch
business rules directly.
"""
import json
import os
import uuid
from datetime import date, datetime, timedelta

from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from .db import now_str
from .domain import ACTIVE_STATUSES, DRIVER_FLOW, Invoice, Load, LoadStatus, parse_dt
from .integration import (DirectToBrokerDelivery, FactoringDelivery, MessengerGateway, PdfExtractor)

LABELS = {"reference": "Load / reference no.", "rate": "Rate", "pickup_city": "Pickup location",
          "delivery_city": "Delivery location", "pickup_appt": "Pickup appointment",
          "delivery_appt": "Delivery appointment", "equipment": "Equipment"}


class ServiceError(Exception):
    """A business rule was broken; the message is shown to the user."""


class AuditService:
    def log(self, db, user_id, entity, entity_id, action, detail, load_no=None):
        db.execute("INSERT INTO audit_log(entity,entity_id,load_no,action,detail,user_id,ts) VALUES (?,?,?,?,?,?,?)",
                   (entity, entity_id, load_no, action, detail, user_id, now_str()))

    def search(self, db, query=""):
        like = f"%{query.strip()}%"
        return db.execute("""SELECT a.*, u.full_name FROM audit_log a LEFT JOIN users u ON u.id=a.user_id
            WHERE a.load_no LIKE ? OR a.detail LIKE ? OR a.action LIKE ? ORDER BY a.id DESC LIMIT 200""",
                          (like, like, like)).fetchall()


audit = AuditService()


class AuthService:
    def login(self, db, username, password):
        row = db.execute("SELECT * FROM users WHERE username=?", (username.strip().lower(),)).fetchone()
        if not row or not row["active"] or not check_password_hash(row["password_hash"], password):
            raise ServiceError("Wrong user name or password.")
        return row


class RateConfirmationService:
    """UC-02, UC-03, UC-04: extract, compare and register the Rate Confirmation."""

    def __init__(self, upload_dir):
        self.upload_dir = upload_dir
        self.extractor = PdfExtractor()

    def store_file(self, file_storage):
        name = secure_filename(file_storage.filename or "rate-confirmation.pdf")
        stored = f"{uuid.uuid4().hex[:8]}_{name}"
        path = os.path.join(self.upload_dir, stored)
        file_storage.save(path)
        return name, stored

    def extract(self, stored_name):
        return self.extractor.extract(os.path.join(self.upload_dir, stored_name))

    @staticmethod
    def compare(terms, rc):
        """Return the fields where the Rate Confirmation differs from the agreed booking."""
        diffs = []

        def add(key, booked, found):
            diffs.append({"field": LABELS[key], "booked": booked, "rc": "not found" if found in (None, "") else found})

        if rc.get("reference", "").strip().lower() != terms["broker_ref"].strip().lower():
            add("reference", terms["broker_ref"], rc.get("reference"))
        if rc.get("rate") is None or abs(float(rc["rate"]) - float(terms["agreed_rate"])) > 0.005:
            add("rate", f"USD {float(terms['agreed_rate']):,.2f}", None if rc.get("rate") is None else f"USD {rc['rate']:,.2f}")
        for key in ("pickup_city", "delivery_city"):
            if rc.get(key, "").strip().lower() != terms[key].strip().lower():
                add(key, terms[key], rc.get(key))
        for key in ("pickup_appt", "delivery_appt"):
            if (rc.get(key) or "")[:16] != (terms[key] or "").replace("T", " ")[:16]:
                add(key, terms[key].replace("T", " "), rc.get(key))
        if rc.get("equipment") != terms["equipment_type"]:
            add("equipment", terms["equipment_type"], rc.get("equipment"))
        return diffs

    def duplicate_reference(self, db, broker_id, ref, exclude_load=None):
        row = db.execute("SELECT load_no FROM loads WHERE broker_id=? AND lower(broker_ref)=lower(?) AND id IS NOT ?",
                         (broker_id, ref.strip(), exclude_load)).fetchone()
        return row["load_no"] if row else None

    def register_load(self, db, user, terms, rc_fields, file_name, stored_name):
        """Create the load. A discrepancy blocks dispatch (status ON_HOLD)."""
        if self.duplicate_reference(db, terms["broker_id"], terms["broker_ref"]):
            raise ServiceError("A load with this broker reference already exists.")
        diffs = self.compare(terms, rc_fields)
        next_id = db.execute("SELECT COALESCE(MAX(id),0)+1 AS n FROM loads").fetchone()["n"]
        load_no = f"L-{1000 + next_id}"
        status = "ON_HOLD" if diffs else "BOOKED"
        hold = "; ".join(f"{d['field']}: agreed {d['booked']}, Rate Confirmation {d['rc']}" for d in diffs) or None
        cur = db.execute("""INSERT INTO loads(load_no,broker_id,broker_ref,status,hold_reason,pickup_city,pickup_address,pickup_appt,
            delivery_city,delivery_address,delivery_appt,commodity,weight_kg,equipment_type,loaded_miles,agreed_rate,
            special_instructions,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                         (load_no, terms["broker_id"], terms["broker_ref"], status, hold, terms["pickup_city"], terms["pickup_address"],
                          terms["pickup_appt"].replace("T", " "), terms["delivery_city"], terms["delivery_address"],
                          terms["delivery_appt"].replace("T", " "), terms["commodity"], terms["weight_kg"], terms["equipment_type"],
                          terms["loaded_miles"], terms["agreed_rate"], terms["special_instructions"], user["id"], now_str()))
        load_id = cur.lastrowid
        self._save_document(db, load_id, user, file_name, stored_name, rc_fields, diffs)
        TrackingService().record_event(db, user, load_id, status, "Rate Confirmation " + ("has discrepancies" if diffs else "verified"), load_no=load_no)
        audit.log(db, user["id"], "load", load_id, "CREATE", f"Load booked with {'discrepancies' if diffs else 'verified Rate Confirmation'}", load_no)
        return load_id, diffs

    def _save_document(self, db, load_id, user, file_name, stored_name, rc_fields, diffs):
        version = db.execute("SELECT COALESCE(MAX(version),0)+1 AS v FROM documents WHERE load_id=? AND kind='RATE_CONFIRMATION'",
                             (load_id,)).fetchone()["v"]
        db.execute("""INSERT INTO documents(load_id,kind,version,file_name,storage_path,status,extracted_json,discrepancies,uploaded_by,uploaded_at)
            VALUES (?,?,?,?,?,?,?,?,?,?)""", (load_id, "RATE_CONFIRMATION", version, file_name, stored_name,
                                              "DISCREPANCY" if diffs else "VERIFIED", json.dumps(rc_fields), json.dumps(diffs), user["id"], now_str()))

    def upload_corrected(self, db, user, load_id, file_storage):
        """UC-04: a corrected Rate Confirmation is saved as a new version; the original is kept."""
        load = Load(db.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone())
        name, stored = self.store_file(file_storage)
        rc, warnings = self.extract(stored)
        terms = dict(db.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone())
        terms["equipment_type"] = load.equipment_type
        diffs = self.compare(terms, rc)
        self._save_document(db, load_id, user, name, stored, rc, diffs)
        if not diffs and load.status == "ON_HOLD":
            db.execute("UPDATE loads SET status='BOOKED', hold_reason=NULL WHERE id=?", (load_id,))
            TrackingService().record_event(db, user, load_id, "BOOKED", "Corrected Rate Confirmation verified", load_no=load.load_no)
        elif diffs:
            db.execute("UPDATE loads SET hold_reason=? WHERE id=?",
                       ("; ".join(f"{d['field']}: agreed {d['booked']}, Rate Confirmation {d['rc']}" for d in diffs), load_id))
        audit.log(db, user["id"], "document", load_id, "UPLOAD_RC", f"Rate Confirmation version saved ({'still differs' if diffs else 'verified'})", load.load_no)
        return diffs


class TrackingService:
    """UC-09, UC-10, UC-11: status milestones, exceptions, extra charges, alerts."""

    def record_event(self, db, user, load_id, status, note="", occurred_at=None, load_no=None):
        db.execute("INSERT INTO status_events(load_id,status,occurred_at,recorded_at,note,recorded_by) VALUES (?,?,?,?,?,?)",
                   (load_id, status, (occurred_at or now_str()).replace("T", " "), now_str(), note, user["id"]))

    def advance(self, db, user, load_id, new_status, note="", occurred_at=None):
        load = Load(db.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone())
        expected = load.next_driver_status()
        if expected is None or expected.name != new_status:
            raise ServiceError("That milestone is not the next step for this load.")
        db.execute("UPDATE loads SET status=? WHERE id=?", (new_status, load_id))
        self.record_event(db, user, load_id, new_status, note, occurred_at)
        audit.log(db, user["id"], "load", load_id, "STATUS", f"Status changed to {LoadStatus[new_status].value}", load.load_no)

    def report_exception(self, db, user, load_id, kind, cause, revised_eta, details, approval_ref):
        load = Load(db.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone())
        if not cause.strip():
            raise ServiceError("Describe the cause of the problem.")
        if kind == "ADDRESS_CHANGE" and not approval_ref.strip():
            raise ServiceError("A changed delivery address needs the broker's written approval reference before the driver is redirected.")
        db.execute("""INSERT INTO load_exceptions(load_id,kind,reported_at,cause,revised_eta,details,approval_ref,reported_by)
            VALUES (?,?,?,?,?,?,?,?)""", (load_id, kind, now_str(), cause.strip(), (revised_eta or "").replace("T", " "), details.strip(), approval_ref.strip(), user["id"]))
        db.execute("INSERT INTO comm_log(load_id,channel,direction,message,logged_at,logged_by) VALUES (?,?,?,?,?,?)",
                   (load_id, "TMS", "IN" if user["role"] == "DRIVER" else "OUT", f"{kind.replace('_', ' ').title()} reported: {cause.strip()}", now_str(), user["id"]))
        audit.log(db, user["id"], "exception", load_id, kind, cause.strip(), load.load_no)

    def add_charge(self, db, user, load_id, kind, amount, evidence, approved):
        load = Load(db.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone())
        if amount <= 0:
            raise ServiceError("Enter a positive amount.")
        if approved and not evidence.strip():
            raise ServiceError("An approved extra charge needs evidence (times, receipt or broker approval).")
        db.execute("INSERT INTO additional_charges(load_id,kind,amount,evidence,broker_approved,created_by,created_at) VALUES (?,?,?,?,?,?,?)",
                   (load_id, kind, amount, evidence.strip(), 1 if approved else 0, user["id"], now_str()))
        audit.log(db, user["id"], "charge", load_id, "CHARGE", f"{kind.title()} USD {amount:,.2f} ({'approved' if approved else 'not approved'})", load.load_no)

    def log_message(self, db, user, load_id, channel, direction, message):
        if not message.strip():
            raise ServiceError("The message is empty.")
        db.execute("INSERT INTO comm_log(load_id,channel,direction,message,logged_at,logged_by) VALUES (?,?,?,?,?,?)",
                   (load_id, channel, direction, message.strip(), now_str(), user["id"]))

    def alerts(self, db):
        """Loads behind schedule or without a recent update (FR-03)."""
        now = datetime.now()
        out = []
        for row in db.execute("SELECT * FROM loads WHERE status IN ('ASSIGNED','DISPATCHED','AT_PICKUP','LOADED','IN_TRANSIT','AT_DELIVERY')"):
            load = Load(row)
            text = load.lateness(now)
            last = db.execute("SELECT MAX(recorded_at) AS t FROM status_events WHERE load_id=?", (load.id,)).fetchone()["t"]
            if not text and last and (now - parse_dt(last)) > timedelta(hours=24):
                text = "No update for more than 24 hours"
            if text:
                out.append((load, text))
        return out


class AssignmentService:
    """UC-06, UC-07, UC-08: availability check, assignment and instructions."""

    def __init__(self):
        self.messenger = MessengerGateway()

    def candidates(self, db, load):
        busy_driver = {r["driver_id"]: r["load_no"] for r in db.execute(
            """SELECT a.driver_id, l.load_no FROM assignments a JOIN loads l ON l.id=a.load_id
               WHERE a.active=1 AND a.accepted IS NOT 0 AND l.status IN ('BOOKED','ASSIGNED','DISPATCHED','AT_PICKUP','LOADED','IN_TRANSIT','AT_DELIVERY')""")}
        busy_equipment = {}
        for r in db.execute("""SELECT a.truck_id, a.trailer_id, l.load_no FROM assignments a JOIN loads l ON l.id=a.load_id
               WHERE a.active=1 AND a.accepted IS NOT 0 AND l.status IN ('BOOKED','ASSIGNED','DISPATCHED','AT_PICKUP','LOADED','IN_TRANSIT','AT_DELIVERY')"""):
            busy_equipment[r["truck_id"]] = r["load_no"]
            busy_equipment[r["trailer_id"]] = r["load_no"]
        drivers = []
        for d in db.execute("SELECT d.*, u.full_name FROM drivers d JOIN users u ON u.id=d.user_id ORDER BY u.full_name"):
            reason = ""
            if d["status"] != "ACTIVE":
                reason = "On leave"
            elif d["id"] in busy_driver:
                reason = f"Busy on {busy_driver[d['id']]}"
            drivers.append({"row": d, "reason": reason})
        trucks, trailers = [], []
        for e in db.execute("SELECT * FROM equipment WHERE status='ACTIVE' ORDER BY unit_no"):
            reason = ""
            if e["id"] in busy_equipment:
                reason = f"In use on {busy_equipment[e['id']]}"
            elif e["kind"] == "TRAILER" and e["equipment_type"] != load.equipment_type:
                reason = f"Wrong type ({e['equipment_type'].replace('_', ' ').title()})"
            elif e["kind"] == "TRAILER" and e["capacity_kg"] < (load.weight_kg or 0):
                reason = f"Capacity {e['capacity_kg']:,} kg is below the load weight"
            (trucks if e["kind"] == "TRUCK" else trailers).append({"row": e, "reason": reason})
        return drivers, trucks, trailers

    def assign(self, db, user, load_id, driver_id, truck_id, trailer_id):
        load = Load(db.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone())
        if not load.can_be_assigned():
            raise ServiceError("Only a booked load with a verified Rate Confirmation can be assigned.")
        drivers, trucks, trailers = self.candidates(db, load)
        for chosen, pool, label in ((driver_id, drivers, "driver"), (truck_id, trucks, "truck"), (trailer_id, trailers, "trailer")):
            item = next((c for c in pool if c["row"]["id"] == chosen), None)
            if not item:
                raise ServiceError(f"Choose a {label}.")
            if item["reason"]:
                raise ServiceError(f"The selected {label} is not available: {item['reason']}.")
        db.execute("UPDATE assignments SET active=0, end_reason='Replaced' WHERE load_id=? AND active=1", (load_id,))
        db.execute("INSERT INTO assignments(load_id,driver_id,truck_id,trailer_id,assigned_at,accepted,active) VALUES (?,?,?,?,?,NULL,1)",
                   (load_id, driver_id, truck_id, trailer_id, now_str()))
        driver = db.execute("SELECT u.full_name FROM drivers d JOIN users u ON u.id=d.user_id WHERE d.id=?", (driver_id,)).fetchone()
        self.messenger.send(db, load_id, driver["full_name"],
                            f"Load {load.load_no}: pickup {load.pickup_city} {load.pickup_appt}, delivery {load.delivery_city} {load.delivery_appt}. Open the TMS app for details.",
                            user["id"], now_str())
        audit.log(db, user["id"], "assignment", load_id, "ASSIGN", f"Assigned to {driver['full_name']}; awaiting driver acceptance", load.load_no)

    def respond(self, db, user, load_id, accept, reason=""):
        a = db.execute("""SELECT a.* FROM assignments a JOIN drivers d ON d.id=a.driver_id
            WHERE a.load_id=? AND a.active=1 AND a.accepted IS NULL AND d.user_id=?""", (load_id, user["id"])).fetchone()
        if not a:
            raise ServiceError("There is no assignment waiting for your answer.")
        load = Load(db.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone())
        if accept:
            db.execute("UPDATE assignments SET accepted=1, decided_at=? WHERE id=?", (now_str(), a["id"]))
            db.execute("UPDATE loads SET status='ASSIGNED' WHERE id=?", (load_id,))
            TrackingService().record_event(db, user, load_id, "ASSIGNED", "Driver accepted the load")
            audit.log(db, user["id"], "assignment", load_id, "ACCEPT", "Driver accepted the load", load.load_no)
        else:
            db.execute("UPDATE assignments SET accepted=0, active=0, decided_at=?, end_reason=? WHERE id=?",
                       (now_str(), reason or "Driver refused", a["id"]))
            db.execute("INSERT INTO load_exceptions(load_id,kind,reported_at,cause,reported_by) VALUES (?,?,?,?,?)",
                       (load_id, "DRIVER_REFUSAL", now_str(), reason or "Driver refused the load", user["id"]))
            audit.log(db, user["id"], "assignment", load_id, "REFUSE", reason or "Driver refused the load", load.load_no)


class DocumentService:
    """UC-12, UC-13: proof of delivery upload and verification."""

    def __init__(self, upload_dir):
        self.upload_dir = upload_dir

    def upload_pod(self, db, user, load_id, file_storage):
        load = Load(db.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone())
        if not file_storage or not file_storage.filename:
            raise ServiceError("Choose a photo or PDF of the signed delivery document.")
        name = secure_filename(file_storage.filename)
        stored = f"{uuid.uuid4().hex[:8]}_{name}"
        file_storage.save(os.path.join(self.upload_dir, stored))
        db.execute("""INSERT INTO documents(load_id,kind,version,file_name,storage_path,status,uploaded_by,uploaded_at)
            VALUES (?,?,1,?,?,'PENDING',?,?)""", (load_id, "POD", name, stored, user["id"], now_str()))
        audit.log(db, user["id"], "document", load_id, "UPLOAD_POD", f"Proof of delivery uploaded ({name})", load.load_no)

    def verify(self, db, user, doc_id, outcome, remarks):
        doc = db.execute("SELECT d.*, l.load_no FROM documents d JOIN loads l ON l.id=d.load_id WHERE d.id=? AND d.kind='POD'", (doc_id,)).fetchone()
        if not doc:
            raise ServiceError("Document not found.")
        if outcome in ("UNCLEAR", "EXCEPTION") and not remarks.strip():
            raise ServiceError("Write what is wrong with the document so the driver or receiver can fix it.")
        db.execute("UPDATE documents SET status=?, remarks=? WHERE id=?", (outcome, remarks.strip(), doc_id))
        audit.log(db, user["id"], "document", doc["load_id"], "VERIFY_POD", f"Proof of delivery marked {outcome.lower()}: {remarks.strip()}", doc["load_no"])

    @staticmethod
    def pod_verified(db, load_id):
        row = db.execute("SELECT 1 FROM documents WHERE load_id=? AND kind='POD' AND status='VERIFIED' LIMIT 1", (load_id,)).fetchone()
        return bool(row)


class InvoiceService:
    """UC-14 to UC-17: generate, deliver and settle invoices."""

    def can_generate(self, db, load):
        reasons = []
        if db.execute("SELECT 1 FROM invoices WHERE load_id=?", (load.id,)).fetchone():
            reasons.append("An invoice already exists for this load")
        if load.status != "DELIVERED":
            reasons.append("The load is not marked delivered")
        if not DocumentService.pod_verified(db, load.id):
            reasons.append("The proof of delivery is not verified")
        rc = db.execute("SELECT 1 FROM documents WHERE load_id=? AND kind='RATE_CONFIRMATION' AND status='VERIFIED'", (load.id,)).fetchone()
        if not rc:
            reasons.append("The Rate Confirmation is not verified")
        return reasons

    def generate(self, db, user, load_id):
        load = Load(db.execute("SELECT * FROM loads WHERE id=?", (load_id,)).fetchone())
        reasons = self.can_generate(db, load)
        if reasons:
            raise ServiceError("Cannot create the invoice: " + "; ".join(reasons) + ".")
        broker = db.execute("SELECT * FROM brokers WHERE id=?", (load.broker_id,)).fetchone()
        charges = db.execute("SELECT * FROM additional_charges WHERE load_id=? AND broker_approved=1 AND evidence <> ''", (load_id,)).fetchall()
        total = load.agreed_rate + sum(c["amount"] for c in charges)
        number = db.execute("SELECT COUNT(*)+1 AS n FROM invoices").fetchone()["n"]
        issue = date.today()
        cur = db.execute("INSERT INTO invoices(invoice_no,load_id,issue_date,due_date,total,status,created_by) VALUES (?,?,?,?,?,?,?)",
                         (f"INV-{issue.year}-{number:04d}", load_id, issue.isoformat(),
                          (issue + timedelta(days=broker["payment_terms_days"])).isoformat(), round(total, 2), "DRAFT", user["id"]))
        inv_id = cur.lastrowid
        db.execute("INSERT INTO invoice_lines(invoice_id,description,amount) VALUES (?,?,?)",
                   (inv_id, f"Transportation charge, load {load.broker_ref} ({load.pickup_city} to {load.delivery_city})", load.agreed_rate))
        for c in charges:
            db.execute("INSERT INTO invoice_lines(invoice_id,description,amount) VALUES (?,?,?)",
                       (inv_id, f"{c['kind'].title()} (evidence: {c['evidence']})", c["amount"]))
        db.execute("UPDATE loads SET status='INVOICED' WHERE id=?", (load_id,))
        TrackingService().record_event(db, user, load_id, "INVOICED", f"Invoice created")
        audit.log(db, user["id"], "invoice", inv_id, "INVOICE", f"Invoice created, total USD {total:,.2f}", load.load_no)
        return inv_id

    def deliver(self, db, user, invoice_id, method):
        inv = db.execute("SELECT i.*, l.load_no FROM invoices i JOIN loads l ON l.id=i.load_id WHERE i.id=?", (invoice_id,)).fetchone()
        if inv["status"] != "DRAFT":
            raise ServiceError("This invoice has already been sent.")
        strategy = FactoringDelivery() if method == "FACTORING" else DirectToBrokerDelivery()
        message = strategy.deliver(db, inv, now_str())
        db.execute("UPDATE invoices SET status=?, submitted_at=? WHERE id=?", (strategy.new_status, now_str(), invoice_id))
        audit.log(db, user["id"], "invoice", invoice_id, "SEND", message, inv["load_no"])
        return message

    def record_payment(self, db, user, invoice_id, amount, received_on, reference):
        inv = db.execute("SELECT i.*, l.load_no FROM invoices i JOIN loads l ON l.id=i.load_id WHERE i.id=?", (invoice_id,)).fetchone()
        if inv["status"] == "DRAFT":
            raise ServiceError("Send the invoice before recording a payment.")
        paid = db.execute("SELECT COALESCE(SUM(amount),0) AS p FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()["p"]
        if amount <= 0 or amount > round(inv["total"] - paid, 2) + 0.005:
            raise ServiceError(f"Enter an amount between 0.01 and {inv['total'] - paid:,.2f}.")
        db.execute("INSERT INTO payments(invoice_id,amount,received_on,reference,recorded_by) VALUES (?,?,?,?,?)",
                   (invoice_id, amount, received_on, reference.strip(), user["id"]))
        paid += amount
        full = paid >= inv["total"] - 0.005
        db.execute("UPDATE invoices SET status=? WHERE id=?", ("PAID" if full else "PARTIALLY_PAID", invoice_id))
        if full:
            db.execute("UPDATE loads SET status='COMPLETED' WHERE id=?", (inv["load_id"],))
            TrackingService().record_event(db, user, inv["load_id"], "COMPLETED", "Payment received in full")
        audit.log(db, user["id"], "payment", invoice_id, "PAYMENT", f"Payment USD {amount:,.2f} recorded ({'paid in full' if full else 'partial'})", inv["load_no"])

    def invoices_with_balance(self, db):
        rows = db.execute("""SELECT i.*, l.load_no, l.broker_ref, b.name AS broker_name,
            COALESCE((SELECT SUM(amount) FROM payments p WHERE p.invoice_id=i.id),0) AS paid
            FROM invoices i JOIN loads l ON l.id=i.load_id JOIN brokers b ON b.id=l.broker_id ORDER BY i.id DESC""").fetchall()
        today = date.today()
        out = []
        for r in rows:
            inv = Invoice(r)
            out.append({"inv": inv, "paid": r["paid"], "outstanding": inv.outstanding(r["paid"]),
                        "overdue": inv.is_overdue(today, r["paid"]), "load_no": r["load_no"], "broker": r["broker_name"]})
        return out


class ReportService:
    """UC-18: management figures."""

    def summary(self, db, start, end):
        scalar = lambda sql, *a: db.execute(sql, a).fetchone()[0] or 0
        active = scalar("SELECT COUNT(*) FROM loads WHERE status IN ('BOOKED','ASSIGNED','DISPATCHED','AT_PICKUP','LOADED','IN_TRANSIT','AT_DELIVERY','ON_HOLD')")
        completed = scalar("SELECT COUNT(*) FROM loads WHERE status IN ('DELIVERED','INVOICED','COMPLETED') AND date(created_at)<=date(?)", end)
        revenue = scalar("SELECT SUM(total) FROM invoices WHERE issue_date BETWEEN ? AND ?", start, end)
        collected = scalar("SELECT SUM(p.amount) FROM payments p WHERE p.received_on BETWEEN ? AND ?", start, end)
        extra = scalar("SELECT SUM(amount) FROM additional_charges WHERE broker_approved=1 AND date(created_at) BETWEEN ? AND ?", start, end)
        invs = InvoiceService().invoices_with_balance(db)
        outstanding = sum(i["outstanding"] for i in invs)
        overdue = [i for i in invs if i["overdue"]]
        per_broker = db.execute("""SELECT b.name, COUNT(l.id) AS loads, COALESCE(SUM(l.agreed_rate),0) AS value
            FROM brokers b LEFT JOIN loads l ON l.broker_id=b.id GROUP BY b.id ORDER BY value DESC""").fetchall()
        delayed = TrackingService().alerts(db)
        return {"active": active, "completed": completed, "revenue": revenue, "collected": collected, "extra": extra,
                "outstanding": outstanding, "overdue": overdue, "per_broker": per_broker, "delayed": delayed}
