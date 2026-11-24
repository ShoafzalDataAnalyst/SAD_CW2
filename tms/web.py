"""Presentation layer: Flask routes (one blueprint) and role-based access."""
import json
import os
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (Blueprint, abort, current_app, flash, g, redirect, render_template, request, send_from_directory,
                   session, url_for)

from .db import get_db, now_str
from .domain import DRIVER_BUTTONS, Invoice, Load, LoadStatus, PodStatus, parse_dt
from .services import (LABELS, AssignmentService, AuditService, AuthService, DocumentService, InvoiceService,
                       RateConfirmationService, ReportService, ServiceError, TrackingService)

bp = Blueprint("web", __name__)
HOME = {"DISPATCHER": "web.dispatch", "BILLING": "web.billing", "MANAGER": "web.reports", "DRIVER": "web.driver_home"}


def rc_service():
    return RateConfirmationService(current_app.config["UPLOAD_DIR"])


def doc_service():
    return DocumentService(current_app.config["UPLOAD_DIR"])


@bp.before_app_request
def load_user():
    g.user = None
    uid = session.get("uid")
    if uid:
        g.user = get_db().execute("SELECT * FROM users WHERE id=? AND active=1", (uid,)).fetchone()


def roles_required(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*a, **kw):
            if not g.user:
                return redirect(url_for("web.login"))
            if g.user["role"] not in roles:
                flash("Your role does not have access to that page.", "error")
                return redirect(url_for(HOME[g.user["role"]]))
            return fn(*a, **kw)
        return wrapper
    return deco


@bp.app_template_filter("status")
def status_label(value):
    try:
        return LoadStatus[value].value
    except KeyError:
        return value.replace("_", " ").title()


@bp.app_template_filter("money")
def money(value):
    return f"USD {float(value or 0):,.2f}"


@bp.app_template_filter("when")
def when(value):
    dt = parse_dt(value) if value else None
    return dt.strftime("%d %b, %H:%M") if dt else "-"


@bp.app_template_filter("day")
def day(value):
    return datetime.strptime(value[:10], "%Y-%m-%d").strftime("%d %b %Y") if value else "-"


@bp.app_template_filter("loads_json")
def loads_json(value):
    return json.loads(value) if value else []


@bp.app_context_processor
def inject():
    return {"user": g.get("user"), "DRIVER_BUTTONS": DRIVER_BUTTONS}


def one_load(db, load_id):
    row = db.execute("SELECT l.*, b.name AS broker_name FROM loads l JOIN brokers b ON b.id=l.broker_id WHERE l.id=?", (load_id,)).fetchone()
    if not row:
        abort(404)
    return Load(row)


def driver_of(db, user):
    return db.execute("SELECT * FROM drivers WHERE user_id=?", (user["id"],)).fetchone()


# ---------------------------------------------------------------- login
@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        try:
            row = AuthService().login(get_db(), request.form["username"], request.form["password"])
        except ServiceError as e:
            flash(str(e), "error")
            return render_template("login.html")
        session.clear()
        session["uid"] = row["id"]
        session.permanent = True
        return redirect(url_for(HOME[row["role"]]))
    return render_template("login.html")


@bp.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("web.login"))


@bp.route("/")
def index():
    return redirect(url_for(HOME[g.user["role"]]) if g.user else url_for("web.login"))


# ---------------------------------------------------------------- dispatcher
@bp.route("/dispatch")
@roles_required("DISPATCHER")
def dispatch():
    db = get_db()
    loads = db.execute("""SELECT l.*, b.name AS broker_name,
        (SELECT u.full_name FROM assignments a JOIN drivers d ON d.id=a.driver_id JOIN users u ON u.id=d.user_id
          WHERE a.load_id=l.id AND a.active=1) AS driver_name,
        (SELECT accepted FROM assignments a WHERE a.load_id=l.id AND a.active=1) AS accepted
        FROM loads l JOIN brokers b ON b.id=l.broker_id WHERE l.status NOT IN ('COMPLETED')
        ORDER BY CASE l.status WHEN 'ON_HOLD' THEN 0 WHEN 'BOOKED' THEN 1 ELSE 2 END, l.pickup_appt""").fetchall()
    summary = ReportService().summary(db, "2000-01-01", date.today().isoformat())
    return render_template("dispatch.html", loads=loads, alerts=TrackingService().alerts(db), summary=summary)


@bp.route("/loads")
@roles_required("DISPATCHER", "MANAGER", "BILLING")
def loads():
    db = get_db()
    status = request.args.get("status", "")
    sql = """SELECT l.*, b.name AS broker_name FROM loads l JOIN brokers b ON b.id=l.broker_id"""
    rows = db.execute(sql + (" WHERE l.status=?" if status else "") + " ORDER BY l.id DESC", (status,) if status else ()).fetchall()
    return render_template("loads.html", loads=rows, status=status, statuses=LoadStatus)


@bp.route("/loads/new", methods=["GET", "POST"])
@roles_required("DISPATCHER")
def new_load():
    db = get_db()
    brokers = db.execute("SELECT * FROM brokers ORDER BY name").fetchall()
    if request.method == "GET":
        return render_template("load_new.html", brokers=brokers, step=1, f={})
    f = request.form.to_dict()
    svc = rc_service()
    try:
        step = request.form.get("step")
        if step in ("check", "recheck"):
            if step == "check":
                upload = request.files.get("rc_file")
                if not upload or not upload.filename:
                    raise ServiceError("Attach the Rate Confirmation PDF received from the broker.")
                name, stored = svc.store_file(upload)
                rc, warnings = svc.extract(stored)
                for w in warnings:
                    flash(w, "warn")
                f.update({"file_name": name, "stored_name": stored})
                for key in ("reference", "rate", "pickup_city", "delivery_city", "pickup_appt", "delivery_appt", "equipment"):
                    f["rc_" + key] = rc.get(key, "")
            diffs = svc.compare(_terms(f), _rc_from_form(f))
            bad = {d["field"] for d in diffs}
            rows = [{"key": k, "label": LABELS[k], "ok": LABELS[k] not in bad} for k in LABELS]
            return render_template("load_new.html", brokers=brokers, step=2, f=f, diffs=diffs, rows=rows,
                                   duplicate=svc.duplicate_reference(db, int(f["broker_id"]), f["broker_ref"]))
        load_id, diffs = svc.register_load(db, g.user, _terms(f), _rc_from_form(f), f["file_name"], f["stored_name"])
        db.commit()
        flash("Load booked and the Rate Confirmation is verified." if not diffs else
              "Load saved ON HOLD: the Rate Confirmation differs from the agreed booking. Request a corrected document from the broker.",
              "ok" if not diffs else "warn")
        return redirect(url_for("web.load_detail", load_id=load_id))
    except (ServiceError, ValueError, KeyError) as e:
        flash(str(e) if isinstance(e, ServiceError) else "Please fill in all required fields.", "error")
        return render_template("load_new.html", brokers=brokers, step=1, f=f)


def _terms(f):
    return {"broker_id": int(f["broker_id"]), "broker_ref": f["broker_ref"].strip(), "agreed_rate": float(f["agreed_rate"]),
            "pickup_city": f["pickup_city"].strip(), "pickup_address": f.get("pickup_address", ""), "pickup_appt": f["pickup_appt"],
            "delivery_city": f["delivery_city"].strip(), "delivery_address": f.get("delivery_address", ""), "delivery_appt": f["delivery_appt"],
            "commodity": f.get("commodity", ""), "weight_kg": int(f.get("weight_kg") or 0), "equipment_type": f["equipment_type"],
            "loaded_miles": int(f.get("loaded_miles") or 0), "special_instructions": f.get("special_instructions", "")}


def _rc_from_form(f):
    rc = {k[3:]: v for k, v in f.items() if k.startswith("rc_") and v != ""}
    if "rate" in rc:
        rc["rate"] = float(str(rc["rate"]).replace(",", "").replace("USD", "").strip())
    return rc


@bp.route("/loads/<int:load_id>")
@roles_required("DISPATCHER", "MANAGER", "BILLING")
def load_detail(load_id):
    db = get_db()
    load = one_load(db, load_id)
    assignment = db.execute("""SELECT a.*, u.full_name AS driver_name, t.unit_no AS truck, tr.unit_no AS trailer
        FROM assignments a JOIN drivers d ON d.id=a.driver_id JOIN users u ON u.id=d.user_id
        LEFT JOIN equipment t ON t.id=a.truck_id LEFT JOIN equipment tr ON tr.id=a.trailer_id
        WHERE a.load_id=? AND a.active=1""", (load_id,)).fetchone()
    data = dict(load=load, assignment=assignment,
                events=db.execute("SELECT e.*, u.full_name FROM status_events e LEFT JOIN users u ON u.id=e.recorded_by WHERE load_id=? ORDER BY e.id", (load_id,)).fetchall(),
                docs=db.execute("SELECT * FROM documents WHERE load_id=? ORDER BY kind, version DESC", (load_id,)).fetchall(),
                exceptions=db.execute("SELECT x.*, u.full_name FROM load_exceptions x LEFT JOIN users u ON u.id=x.reported_by WHERE load_id=? ORDER BY x.id DESC", (load_id,)).fetchall(),
                charges=db.execute("SELECT * FROM additional_charges WHERE load_id=? ORDER BY id", (load_id,)).fetchall(),
                comms=db.execute("SELECT c.*, u.full_name FROM comm_log c LEFT JOIN users u ON u.id=c.logged_by WHERE load_id=? ORDER BY c.id DESC", (load_id,)).fetchall(),
                invoice=db.execute("SELECT * FROM invoices WHERE load_id=?", (load_id,)).fetchone(),
                late=load.lateness(datetime.now()))
    if load.can_be_assigned() and g.user["role"] == "DISPATCHER":
        data["drivers"], data["trucks"], data["trailers"] = AssignmentService().candidates(db, load)
    return render_template("load_detail.html", **data)


def _act(fn, ok_msg, load_id=None, back="web.load_detail", **back_kw):
    """Run a service call, commit on success, show the business-rule message on failure."""
    db = get_db()
    try:
        result = fn()
        db.commit()
        flash(result if isinstance(result, str) else ok_msg, "ok")
    except ServiceError as e:
        db.rollback()
        flash(str(e), "error")
    kw = back_kw or ({"load_id": load_id} if load_id is not None else {})
    return redirect(url_for(back, **kw))


@bp.post("/loads/<int:load_id>/rc")
@roles_required("DISPATCHER")
def upload_rc(load_id):
    db = get_db()
    upload = request.files.get("rc_file")
    if not upload or not upload.filename:
        flash("Attach the corrected Rate Confirmation.", "error")
        return redirect(url_for("web.load_detail", load_id=load_id))
    diffs = rc_service().upload_corrected(db, g.user, load_id, upload)
    db.commit()
    flash("The corrected Rate Confirmation still differs from the agreed booking." if diffs else
          "Corrected Rate Confirmation verified. The load is released for assignment.", "warn" if diffs else "ok")
    return redirect(url_for("web.load_detail", load_id=load_id))


@bp.post("/loads/<int:load_id>/assign")
@roles_required("DISPATCHER")
def assign(load_id):
    f = request.form
    return _act(lambda: AssignmentService().assign(get_db(), g.user, load_id, int(f.get("driver_id") or 0),
                                                   int(f.get("truck_id") or 0), int(f.get("trailer_id") or 0)),
                "Assigned. Instructions were sent to the driver through Messenger and the TMS app; waiting for the driver to accept.", load_id)


@bp.post("/loads/<int:load_id>/exception")
@roles_required("DISPATCHER")
def exception(load_id):
    f = request.form
    return _act(lambda: TrackingService().report_exception(get_db(), g.user, load_id, f["kind"], f["cause"], f.get("revised_eta", ""),
                                                           f.get("details", ""), f.get("approval_ref", "")), "Problem recorded.", load_id)


@bp.post("/loads/<int:load_id>/charge")
@roles_required("DISPATCHER")
def charge(load_id):
    f = request.form
    try:
        amount = float(f["amount"])
    except (KeyError, ValueError):
        flash("Enter the amount.", "error")
        return redirect(url_for("web.load_detail", load_id=load_id))
    return _act(lambda: TrackingService().add_charge(get_db(), g.user, load_id, f["kind"], amount, f.get("evidence", ""), bool(f.get("approved"))),
                "Extra charge recorded.", load_id)


@bp.post("/loads/<int:load_id>/message")
@roles_required("DISPATCHER")
def message(load_id):
    f = request.form
    return _act(lambda: TrackingService().log_message(get_db(), g.user, load_id, f["channel"], f["direction"], f["message"]),
                "Message added to the communication log.", load_id)


@bp.route("/files/<path:name>")
def files(name):
    if not g.user:
        abort(403)
    return send_from_directory(current_app.config["UPLOAD_DIR"], name)


# ---------------------------------------------------------------- driver (mobile)
@bp.route("/driver")
@roles_required("DRIVER")
def driver_home():
    db = get_db()
    d = driver_of(db, g.user)
    rows = db.execute("""SELECT l.*, a.accepted FROM loads l JOIN assignments a ON a.load_id=l.id
        WHERE a.driver_id=? AND a.active=1 ORDER BY (l.status='COMPLETED'), l.pickup_appt""", (d["id"],)).fetchall()
    return render_template("driver_home.html", loads=rows, driver=d)


@bp.route("/driver/loads/<int:load_id>")
@roles_required("DRIVER")
def driver_load(load_id):
    db = get_db()
    d = driver_of(db, g.user)
    a = db.execute("SELECT * FROM assignments WHERE load_id=? AND driver_id=? AND active=1", (load_id, d["id"])).fetchone()
    if not a:
        abort(404)
    load = one_load(db, load_id)
    pod = db.execute("SELECT * FROM documents WHERE load_id=? AND kind='POD' ORDER BY id DESC", (load_id,)).fetchone()
    return render_template("driver_load.html", load=load, a=a, next_status=load.next_driver_status() if a["accepted"] else None, pod=pod,
                           events=db.execute("SELECT * FROM status_events WHERE load_id=? ORDER BY id DESC LIMIT 5", (load_id,)).fetchall())


@bp.post("/driver/loads/<int:load_id>/respond")
@roles_required("DRIVER")
def driver_respond(load_id):
    accept = request.form.get("answer") == "accept"
    run = lambda: AssignmentService().respond(get_db(), g.user, load_id, accept, request.form.get("reason", ""))
    if accept:
        return _act(run, "Load accepted. Instructions are below.", load_id, back="web.driver_load")
    return _act(run, "You refused the load. The dispatcher has been informed.", None, back="web.driver_home")


@bp.post("/driver/loads/<int:load_id>/status")
@roles_required("DRIVER")
def driver_status(load_id):
    return _act(lambda: TrackingService().advance(get_db(), g.user, load_id, request.form["status"], request.form.get("note", "")),
                "Status updated.", load_id, back="web.driver_load")


@bp.post("/driver/loads/<int:load_id>/delay")
@roles_required("DRIVER")
def driver_delay(load_id):
    f = request.form
    return _act(lambda: TrackingService().report_exception(get_db(), g.user, load_id, f.get("kind", "DELAY"), f.get("cause", ""),
                                                           f.get("revised_eta", ""), "", ""), "The dispatcher has been informed.", load_id,
                back="web.driver_load")


@bp.post("/driver/loads/<int:load_id>/pod")
@roles_required("DRIVER")
def driver_pod(load_id):
    return _act(lambda: doc_service().upload_pod(get_db(), g.user, load_id, request.files.get("pod_file")),
                "Proof of delivery uploaded. Billing will check it.", load_id, back="web.driver_load")


# ---------------------------------------------------------------- billing
@bp.route("/billing")
@roles_required("BILLING")
def billing():
    db = get_db()
    pending_pod = db.execute("""SELECT d.*, l.load_no, l.broker_ref, l.status AS load_status, b.name AS broker_name
        FROM documents d JOIN loads l ON l.id=d.load_id JOIN brokers b ON b.id=l.broker_id
        WHERE d.kind='POD' AND d.status IN ('PENDING','UNCLEAR','EXCEPTION') ORDER BY d.id""").fetchall()
    ready = []
    for r in db.execute("SELECT l.*, b.name AS broker_name FROM loads l JOIN brokers b ON b.id=l.broker_id WHERE l.status='DELIVERED' ORDER BY l.id"):
        load = Load(r)
        ready.append({"load": load, "reasons": InvoiceService().can_generate(db, load)})
    return render_template("billing.html", pending_pod=pending_pod, ready=ready, invoices=InvoiceService().invoices_with_balance(db))


@bp.post("/billing/pod/<int:doc_id>/verify")
@roles_required("BILLING", "DISPATCHER")
def verify_pod(doc_id):
    f = request.form
    return _act(lambda: doc_service().verify(get_db(), g.user, doc_id, f["outcome"], f.get("remarks", "")),
                "Proof of delivery checked.", None, back="web.billing")


@bp.post("/billing/loads/<int:load_id>/invoice")
@roles_required("BILLING")
def make_invoice(load_id):
    db = get_db()
    try:
        inv_id = InvoiceService().generate(db, g.user, load_id)
        db.commit()
        flash("Invoice created from the verified load data.", "ok")
        return redirect(url_for("web.invoice_view", invoice_id=inv_id))
    except ServiceError as e:
        db.rollback()
        flash(str(e), "error")
        return redirect(url_for("web.billing"))


@bp.route("/invoices/<int:invoice_id>")
@roles_required("BILLING", "MANAGER")
def invoice_view(invoice_id):
    db = get_db()
    row = db.execute("""SELECT i.*, l.load_no, l.broker_ref, l.pickup_city, l.delivery_city, b.name AS broker_name, b.factoring_default
        FROM invoices i JOIN loads l ON l.id=i.load_id JOIN brokers b ON b.id=l.broker_id WHERE i.id=?""", (invoice_id,)).fetchone()
    if not row:
        abort(404)
    inv = Invoice(row)
    paid = db.execute("SELECT COALESCE(SUM(amount),0) AS p FROM payments WHERE invoice_id=?", (invoice_id,)).fetchone()["p"]
    return render_template("invoice.html", inv=inv, paid=paid, outstanding=inv.outstanding(paid), overdue=inv.is_overdue(date.today(), paid),
                           lines=db.execute("SELECT * FROM invoice_lines WHERE invoice_id=?", (invoice_id,)).fetchall(),
                           payments=db.execute("SELECT * FROM payments WHERE invoice_id=? ORDER BY id", (invoice_id,)).fetchall(),
                           docs=db.execute("SELECT * FROM documents WHERE load_id=? AND status='VERIFIED'", (row["load_id"],)).fetchall(),
                           today=date.today().isoformat())


@bp.post("/invoices/<int:invoice_id>/send")
@roles_required("BILLING")
def send_invoice(invoice_id):
    return _act(lambda: InvoiceService().deliver(get_db(), g.user, invoice_id, request.form.get("method", "BROKER")), "Sent.", 0,
                back="web.invoice_view", invoice_id=invoice_id)


@bp.post("/invoices/<int:invoice_id>/payment")
@roles_required("BILLING")
def payment(invoice_id):
    f = request.form
    try:
        amount = float(f["amount"])
    except (KeyError, ValueError):
        flash("Enter the amount received.", "error")
        return redirect(url_for("web.invoice_view", invoice_id=invoice_id))
    return _act(lambda: InvoiceService().record_payment(get_db(), g.user, invoice_id, amount, f.get("received_on") or date.today().isoformat(),
                                                        f.get("reference", "")), "Payment recorded.", 0, back="web.invoice_view", invoice_id=invoice_id)


# ---------------------------------------------------------------- manager
@bp.route("/reports")
@roles_required("MANAGER")
def reports():
    db = get_db()
    end = request.args.get("to") or date.today().isoformat()
    start = request.args.get("from") or (date.today() - timedelta(days=365)).isoformat()
    return render_template("reports.html", s=ReportService().summary(db, start, end), start=start, end=end)


@bp.route("/audit")
@roles_required("MANAGER")
def audit():
    q = request.args.get("q", "")
    return render_template("audit.html", rows=AuditService().search(get_db(), q), q=q)
