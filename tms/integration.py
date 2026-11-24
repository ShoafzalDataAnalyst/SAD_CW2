"""Integration layer: PDF extraction and the external parties (Messenger, factoring).

In this prototype Messenger and the factoring company are simulated: the call is
recorded in the database instead of reaching a real service. Each gateway sits
behind a small interface so the real service can replace it later.
"""
import re
from abc import ABC, abstractmethod

from pypdf import PdfReader

FIELD_PATTERNS = {
    "reference": r"Load\s*/\s*Reference\s*#?\s*:\s*(.+)",
    "broker": r"Broker\s*:\s*(.+)",
    "rate": r"Rate\s*:\s*USD\s*([\d,]+(?:\.\d{1,2})?)",
    "pickup_city": r"Pickup\s*:\s*(.+?)\s*\|",
    "pickup_appt": r"Pickup\s*:\s*.+?\|\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})",
    "delivery_city": r"Delivery\s*:\s*(.+?)\s*\|",
    "delivery_appt": r"Delivery\s*:\s*.+?\|\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})",
    "equipment": r"Equipment\s*:\s*(.+)",
    "commodity": r"Commodity\s*:\s*(.+?)(?:\s{2,}|\s*Weight|$)",
    "special_instructions": r"Special instructions\s*:\s*(.+)",
}
EQUIPMENT_NAMES = {"dry van": "DRY_VAN", "reefer": "REEFER", "flatbed": "FLATBED"}


class PdfExtractor:
    """Reads the key fields of a Rate Confirmation PDF (FR-01).

    Rule-based on purpose: it recognises the labelled layout used by the sample
    documents. Real broker layouts vary, so a production version needs an
    OCR/ML extractor; the dispatcher can always correct the result.
    """

    def extract(self, path):
        try:
            text = "\n".join(page.extract_text() or "" for page in PdfReader(path).pages)
        except Exception:
            return {}, ["The file could not be read. Enter the fields manually."]
        found = {}
        for key, pattern in FIELD_PATTERNS.items():
            m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if m:
                found[key] = m.group(1).strip()
        if "rate" in found:
            found["rate"] = float(found["rate"].replace(",", ""))
        if "equipment" in found:
            found["equipment"] = EQUIPMENT_NAMES.get(found["equipment"].lower(), found["equipment"])
        missing = [k for k in ("reference", "rate", "pickup_city", "delivery_city", "pickup_appt", "delivery_appt", "equipment")
                   if k not in found]
        warnings = ["Not found in the document: " + ", ".join(missing) + ". Please complete them."] if missing else []
        return found, warnings


class MessengerGateway:
    """Sends load instructions to the driver's Messenger chat (simulated)."""

    def send(self, db, load_id, driver_name, text, user_id, when):
        db.execute("INSERT INTO comm_log(load_id,channel,direction,message,logged_at,logged_by) VALUES (?,?,?,?,?,?)",
                   (load_id, "MESSENGER", "OUT", f"To {driver_name}: {text}", when, user_id))


class InvoiceDeliveryStrategy(ABC):
    """How a finished invoice reaches the party that pays it (UC-15, UC-16)."""

    new_status = ""

    @abstractmethod
    def deliver(self, db, invoice, when):
        ...


class DirectToBrokerDelivery(InvoiceDeliveryStrategy):
    new_status = "SENT"

    def deliver(self, db, invoice, when):
        return "Invoice and supporting documents sent to the broker"


class FactoringDelivery(InvoiceDeliveryStrategy):
    new_status = "FACTORED"

    def deliver(self, db, invoice, when):
        ref = f"KF-{invoice['id']:05d}"
        db.execute("INSERT INTO factoring_submissions(invoice_id,company,submitted_at,submission_ref) VALUES (?,?,?,?)",
                   (invoice["id"], "Keystone Factoring", when, ref))
        return f"Invoice package submitted to Keystone Factoring (ref {ref})"
