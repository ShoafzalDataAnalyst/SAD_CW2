"""Persistence layer: SQLite connection, schema and demo data."""
import os
import shutil
import sqlite3
from datetime import datetime, timedelta

from flask import g
from werkzeug.security import generate_password_hash

SCHEMA = """
CREATE TABLE users (
  id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL, full_name TEXT NOT NULL,
  email TEXT, password_hash TEXT NOT NULL, role TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1);
CREATE TABLE brokers (
  id INTEGER PRIMARY KEY, name TEXT NOT NULL, contact_email TEXT,
  payment_terms_days INTEGER NOT NULL DEFAULT 30, factoring_default INTEGER NOT NULL DEFAULT 0);
CREATE TABLE drivers (
  id INTEGER PRIMARY KEY, user_id INTEGER REFERENCES users(id), license_no TEXT, phone TEXT,
  current_location TEXT, status TEXT NOT NULL DEFAULT 'ACTIVE');
CREATE TABLE equipment (
  id INTEGER PRIMARY KEY, kind TEXT NOT NULL, unit_no TEXT NOT NULL, equipment_type TEXT NOT NULL,
  capacity_kg INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'ACTIVE');
CREATE TABLE loads (
  id INTEGER PRIMARY KEY, load_no TEXT UNIQUE NOT NULL, broker_id INTEGER NOT NULL REFERENCES brokers(id),
  broker_ref TEXT NOT NULL, status TEXT NOT NULL, hold_reason TEXT,
  pickup_city TEXT, pickup_address TEXT, pickup_appt TEXT,
  delivery_city TEXT, delivery_address TEXT, delivery_appt TEXT,
  commodity TEXT, weight_kg INTEGER, equipment_type TEXT, loaded_miles INTEGER,
  agreed_rate REAL NOT NULL, special_instructions TEXT, created_by INTEGER, created_at TEXT);
CREATE TABLE documents (
  id INTEGER PRIMARY KEY, load_id INTEGER NOT NULL REFERENCES loads(id), kind TEXT NOT NULL,
  version INTEGER NOT NULL DEFAULT 1, file_name TEXT NOT NULL, storage_path TEXT NOT NULL,
  status TEXT, remarks TEXT, extracted_json TEXT, discrepancies TEXT,
  uploaded_by INTEGER, uploaded_at TEXT);
CREATE TABLE assignments (
  id INTEGER PRIMARY KEY, load_id INTEGER NOT NULL REFERENCES loads(id),
  driver_id INTEGER NOT NULL REFERENCES drivers(id), truck_id INTEGER REFERENCES equipment(id),
  trailer_id INTEGER REFERENCES equipment(id), assigned_at TEXT, accepted INTEGER,
  decided_at TEXT, active INTEGER NOT NULL DEFAULT 1, end_reason TEXT);
CREATE TABLE status_events (
  id INTEGER PRIMARY KEY, load_id INTEGER NOT NULL REFERENCES loads(id), status TEXT NOT NULL,
  occurred_at TEXT, recorded_at TEXT, note TEXT, recorded_by INTEGER);
CREATE TABLE load_exceptions (
  id INTEGER PRIMARY KEY, load_id INTEGER NOT NULL REFERENCES loads(id), kind TEXT NOT NULL,
  reported_at TEXT, cause TEXT, revised_eta TEXT, details TEXT, approval_ref TEXT,
  resolved INTEGER NOT NULL DEFAULT 0, reported_by INTEGER);
CREATE TABLE additional_charges (
  id INTEGER PRIMARY KEY, load_id INTEGER NOT NULL REFERENCES loads(id), kind TEXT NOT NULL,
  amount REAL NOT NULL, evidence TEXT, broker_approved INTEGER NOT NULL DEFAULT 0,
  created_by INTEGER, created_at TEXT);
CREATE TABLE comm_log (
  id INTEGER PRIMARY KEY, load_id INTEGER NOT NULL REFERENCES loads(id), channel TEXT NOT NULL,
  direction TEXT NOT NULL, message TEXT NOT NULL, logged_at TEXT, logged_by INTEGER);
CREATE TABLE invoices (
  id INTEGER PRIMARY KEY, invoice_no TEXT UNIQUE NOT NULL, load_id INTEGER UNIQUE NOT NULL REFERENCES loads(id),
  issue_date TEXT, due_date TEXT, total REAL NOT NULL, status TEXT NOT NULL,
  submitted_at TEXT, created_by INTEGER);
CREATE TABLE invoice_lines (
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  description TEXT NOT NULL, amount REAL NOT NULL);
CREATE TABLE payments (
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  amount REAL NOT NULL, received_on TEXT, reference TEXT, recorded_by INTEGER);
CREATE TABLE factoring_submissions (
  id INTEGER PRIMARY KEY, invoice_id INTEGER NOT NULL REFERENCES invoices(id),
  company TEXT, submitted_at TEXT, submission_ref TEXT);
CREATE TABLE audit_log (
  id INTEGER PRIMARY KEY, entity TEXT, entity_id INTEGER, load_no TEXT, action TEXT, detail TEXT,
  user_id INTEGER, ts TEXT);
"""


def get_db():
    from flask import current_app
    if "db" not in g:
        g.db = sqlite3.connect(current_app.config["DATABASE"])
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
    return g.db


def close_db(_exc=None):
    db = g.pop("db", None)
    if db is not None:
        db.close()


def now_str(delta_hours=0):
    return (datetime.now() + timedelta(hours=delta_hours)).strftime("%Y-%m-%d %H:%M")


def init_db(app):
    """Create the database and load demo data on the first start."""
    os.makedirs(app.config["UPLOAD_DIR"], exist_ok=True)
    if os.path.exists(app.config["DATABASE"]):
        return
    db = sqlite3.connect(app.config["DATABASE"])
    db.executescript(SCHEMA)
    seed(db, app)
    db.commit()
    db.close()


def _copy_sample(app, sample, stored_name):
    src = os.path.join(app.config["SAMPLE_DIR"], sample)
    dst = os.path.join(app.config["UPLOAD_DIR"], stored_name)
    if os.path.exists(src):
        shutil.copy(src, dst)
    return stored_name


def seed(db, app):
    h = generate_password_hash("demo123")
    users = [
        (1, "dispatcher", "Dana Whitfield", "dana@transportationone.example", "DISPATCHER"),
        (2, "billing", "Priya Raman", "priya@transportationone.example", "BILLING"),
        (3, "manager", "Marcus Cole", "marcus@transportationone.example", "MANAGER"),
        (4, "tom", "Tom Reyes", "tom@transportationone.example", "DRIVER"),
        (5, "luis", "Luis Ortega", "luis@transportationone.example", "DRIVER"),
        (6, "sam", "Sam Patel", "sam@transportationone.example", "DRIVER"),
        (7, "ana", "Ana Kowalski", "ana@transportationone.example", "DRIVER"),
    ]
    db.executemany("INSERT INTO users(id,username,full_name,email,password_hash,role) VALUES (?,?,?,?,?,?)",
                   [(i, u, n, e, h, r) for i, u, n, e, r in users])
    db.executemany("INSERT INTO brokers(id,name,contact_email,payment_terms_days,factoring_default) VALUES (?,?,?,?,?)", [
        (1, "Atlas Freight Brokers", "ops@atlasfreight.example", 30, 1),
        (2, "Northbound Logistics", "dispatch@northbound.example", 21, 0),
        (3, "Summit Load Services", "loads@summit.example", 30, 0),
    ])
    db.executemany("INSERT INTO drivers(id,user_id,license_no,phone,current_location,status) VALUES (?,?,?,?,?,?)", [
        (1, 4, "CDL-77120", "+1 555 0142", "Memphis, TN", "ACTIVE"),
        (2, 5, "CDL-65018", "+1 555 0177", "Kansas City, MO", "ACTIVE"),
        (3, 6, "CDL-80431", "+1 555 0109", "Chicago, IL", "ACTIVE"),
        (4, 7, "CDL-59377", "+1 555 0123", "Dallas, TX", "ON_LEAVE"),
    ])
    db.executemany("INSERT INTO equipment(id,kind,unit_no,equipment_type,capacity_kg,status) VALUES (?,?,?,?,?,?)", [
        (1, "TRUCK", "T-201", "TRACTOR", 36000, "ACTIVE"), (2, "TRUCK", "T-202", "TRACTOR", 36000, "ACTIVE"),
        (3, "TRUCK", "T-203", "TRACTOR", 36000, "ACTIVE"), (4, "TRUCK", "T-204", "TRACTOR", 36000, "ACTIVE"),
        (5, "TRAILER", "DV-31", "DRY_VAN", 20000, "ACTIVE"), (6, "TRAILER", "DV-32", "DRY_VAN", 20000, "ACTIVE"),
        (7, "TRAILER", "RF-11", "REEFER", 18000, "ACTIVE"), (8, "TRAILER", "FB-05", "FLATBED", 24000, "ACTIVE"),
    ])
    n = now_str

    def load(i, no, broker, ref, status, pc, pa, pap, dc, da, dap, com, w, eq, miles, rate, instr, hold=None):
        db.execute("""INSERT INTO loads(id,load_no,broker_id,broker_ref,status,hold_reason,pickup_city,pickup_address,
            pickup_appt,delivery_city,delivery_address,delivery_appt,commodity,weight_kg,equipment_type,loaded_miles,
            agreed_rate,special_instructions,created_by,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (i, no, broker, ref, status, hold, pc, pa, pap, dc, da, dap, com, w, eq, miles, rate, instr, 1, n(-120)))

    # L-1001 completed and paid
    load(1, "L-1001", 1, "AFB-47001", "COMPLETED", "Chicago, IL", "410 Industrial Dr", n(-150), "Atlanta, GA",
         "88 Peachtree Logistics Park", n(-100), "Packaged food", 17800, "DRY_VAN", 716, 2150.0, "Call 1 hour before arrival")
    # L-1002 invoiced, overdue
    load(2, "L-1002", 2, "NBL-9915", "INVOICED", "Omaha, NE", "12 Packer Rd", n(-300), "Denver, CO",
         "700 Mile High Way", n(-250), "Paper rolls", 16200, "DRY_VAN", 540, 1780.0, "Dock appointment required")
    # L-1003 in transit with a delay
    load(3, "L-1003", 1, "AFB-47388", "IN_TRANSIT", "Memphis, TN", "55 River Port Rd", n(-30), "Dallas, TX",
         "2100 Commerce St", n(6), "Auto parts", 14500, "DRY_VAN", 452, 1890.0, "No lumpers")
    # L-1004 delivered, POD awaiting check
    load(4, "L-1004", 3, "SLS-2201", "DELIVERED", "Houston, TX", "9 Gulf Fwy", n(-60), "New Orleans, LA",
         "1 Canal St Warehouse", n(-30), "Beverages", 15200, "DRY_VAN", 348, 1320.0, "Seal must be intact")
    # L-1005 booked, ready for assignment (reefer)
    load(5, "L-1005", 2, "NBL-10042", "BOOKED", "Kansas City, MO", "300 Stockyards Ave", n(40), "Minneapolis, MN",
         "45 Cold Chain Blvd", n(66), "Dairy products", 12400, "REEFER", 440, 2260.0, "Hold at 3 C")
    # L-1006 on hold, Rate Confirmation mismatch
    load(6, "L-1006", 3, "SLS-2260", "ON_HOLD", "Nashville, TN", "21 Music Row Depot", n(30), "Charlotte, NC",
         "600 Freight Ln", n(56), "Furniture", 11000, "DRY_VAN", 409, 1540.0, "Two-person unload",
         hold="Rate on Rate Confirmation (USD 1,440.00) differs from the agreed rate (USD 1,540.00)")
    # L-1007 assigned, awaiting driver acceptance
    load(7, "L-1007", 1, "AFB-47410", "BOOKED", "Dallas, TX", "77 Trade Center", n(20), "Oklahoma City, OK",
         "1500 Heartland Pkwy", n(32), "Steel fittings", 19000, "FLATBED", 206, 980.0, "Tarps required")

    # documents
    def doc(i, lid, kind, ver, fname, stored, status, remarks=None, extracted=None, disc=None, ago=-100):
        db.execute("""INSERT INTO documents(id,load_id,kind,version,file_name,storage_path,status,remarks,extracted_json,
            discrepancies,uploaded_by,uploaded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
                   (i, lid, kind, ver, fname, stored, status, remarks, extracted, disc, 1, n(ago)))
    import json
    for i, (lid, ref) in enumerate([(1, "AFB-47001"), (3, "AFB-47388"), (5, "NBL-10042"), (7, "AFB-47410")], start=1):
        doc(i, lid, "RATE_CONFIRMATION", 1, f"RateConfirmation_{ref}.pdf", f"seed_rc_{lid}.pdf", "VERIFIED",
            extracted=json.dumps({"reference": ref}), ago=-118)
    doc(5, 6, "RATE_CONFIRMATION", 1, "RateConfirmation_SLS-2260.pdf", "seed_rc_6.pdf", "DISCREPANCY",
        extracted=json.dumps({"reference": "SLS-2260", "rate": 1440.0}),
        disc=json.dumps([{"field": "Rate", "booked": "USD 1,540.00", "rc": "USD 1,440.00"}]), ago=-20)
    doc(6, 1, "POD", 1, "POD_L-1001.pdf", _copy_sample(app, "POD_AFB-48213.pdf", "seed_pod_1.pdf"), "VERIFIED", ago=-98)
    doc(7, 4, "POD", 1, "POD_L-1004.pdf", _copy_sample(app, "POD_AFB-48213.pdf", "seed_pod_4.pdf"), "PENDING", ago=-28)
    for i in (1, 3, 5, 7):
        _copy_sample(app, "RateConfirmation_AFB-48213.pdf", f"seed_rc_{i}.pdf")
    _copy_sample(app, "RateConfirmation_SLS-2260_mismatch.pdf", "seed_rc_6.pdf")

    # assignments (id, load, driver, truck, trailer, accepted)
    db.executemany("INSERT INTO assignments(id,load_id,driver_id,truck_id,trailer_id,assigned_at,accepted,decided_at,active) VALUES (?,?,?,?,?,?,?,?,?)", [
        (1, 1, 3, 1, 5, n(-140), 1, n(-139), 0), (2, 2, 2, 2, 6, n(-290), 1, n(-289), 0),
        (3, 3, 1, 3, 6, n(-45), 1, n(-44), 1), (4, 4, 2, 2, 5, n(-80), 1, n(-79), 0),
    ])
    ev = [(1, "BOOKED", -118), (1, "ASSIGNED", -139), (1, "DELIVERED", -100), (1, "INVOICED", -96), (1, "COMPLETED", -60),
          (2, "BOOKED", -310), (2, "DELIVERED", -250), (2, "INVOICED", -240),
          (3, "BOOKED", -50), (3, "ASSIGNED", -44), (3, "DISPATCHED", -40), (3, "AT_PICKUP", -32), (3, "LOADED", -30), (3, "IN_TRANSIT", -29),
          (4, "BOOKED", -90), (4, "ASSIGNED", -79), (4, "DELIVERED", -30),
          (5, "BOOKED", -30), (6, "ON_HOLD", -20), (7, "BOOKED", -10)]
    db.executemany("INSERT INTO status_events(load_id,status,occurred_at,recorded_at,note,recorded_by) VALUES (?,?,?,?,?,?)",
                   [(l, s, n(h), n(h), None, 1) for l, s, h in ev])
    db.execute("""INSERT INTO load_exceptions(load_id,kind,reported_at,cause,revised_eta,details,reported_by)
        VALUES (3,'DELAY',?,?,?,?,4)""", (n(-8), "Heavy traffic after accident on I-40", n(6), "Broker informed by phone"))
    db.execute("INSERT INTO comm_log(load_id,channel,direction,message,logged_at,logged_by) VALUES (3,'MESSENGER','IN','Stuck behind an accident, ETA +3h',?,4)", (n(-8),))
    db.execute("INSERT INTO comm_log(load_id,channel,direction,message,logged_at,logged_by) VALUES (3,'PHONE','OUT','Called broker: delivery moved to the new ETA',?,1)", (n(-7),))
    db.executemany("INSERT INTO invoices(id,invoice_no,load_id,issue_date,due_date,total,status,submitted_at,created_by) VALUES (?,?,?,?,?,?,?,?,2)", [
        (1, "INV-2026-0001", 1, n(-96)[:10], n(-96 + 30 * 24)[:10], 2150.0, "PAID", n(-96), ),
        (2, "INV-2026-0002", 2, n(-240)[:10], n(-240 + 21 * 24)[:10], 1780.0, "SENT", n(-240)),
    ])
    db.executemany("INSERT INTO invoice_lines(invoice_id,description,amount) VALUES (?,?,?)", [
        (1, "Transportation charge, load AFB-47001", 2150.0), (2, "Transportation charge, load NBL-9915", 1780.0)])
    db.execute("INSERT INTO payments(invoice_id,amount,received_on,reference,recorded_by) VALUES (1,2150.0,?,?,2)", (n(-60)[:10], "ACH-88213"))
    db.execute("INSERT INTO factoring_submissions(invoice_id,company,submitted_at,submission_ref) VALUES (1,'Keystone Factoring',?,'KF-20511')", (n(-96),))
    db.execute("INSERT INTO audit_log(entity,entity_id,load_no,action,detail,user_id,ts) VALUES ('load',1,'L-1001','CREATE','Demo data loaded',1,?)", (n(-120),))
