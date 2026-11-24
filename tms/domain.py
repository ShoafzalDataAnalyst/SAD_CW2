"""Domain layer.

Enumerations and entity classes of the UML domain model (report, Section 3).
Entities wrap a database row and carry the business rules that belong to them,
so the services above this layer do not repeat those rules.
"""
from datetime import datetime
from enum import Enum

DT_FORMAT = "%Y-%m-%d %H:%M"


def parse_dt(value):
    """Parse the 'YYYY-MM-DD HH:MM' strings stored in the database."""
    if not value:
        return None
    return datetime.strptime(value[:16].replace("T", " "), DT_FORMAT)


class LoadStatus(Enum):
    ON_HOLD = "On hold"
    BOOKED = "Booked"
    ASSIGNED = "Assigned"
    DISPATCHED = "Dispatched"
    AT_PICKUP = "At pickup"
    LOADED = "Loaded"
    IN_TRANSIT = "In transit"
    AT_DELIVERY = "At delivery"
    DELIVERED = "Delivered"
    INVOICED = "Invoiced"
    COMPLETED = "Completed"


# Milestones the driver reports, in order (FR-03).
DRIVER_FLOW = [
    LoadStatus.DISPATCHED, LoadStatus.AT_PICKUP, LoadStatus.LOADED,
    LoadStatus.IN_TRANSIT, LoadStatus.AT_DELIVERY, LoadStatus.DELIVERED,
]
DRIVER_BUTTONS = {
    "DISPATCHED": "Start trip to pickup",
    "AT_PICKUP": "Arrived at pickup",
    "LOADED": "Loaded",
    "IN_TRANSIT": "In transit",
    "AT_DELIVERY": "Arrived at delivery",
    "DELIVERED": "Delivered",
}
ACTIVE_STATUSES = {"ASSIGNED", "DISPATCHED", "AT_PICKUP", "LOADED", "IN_TRANSIT", "AT_DELIVERY"}


class EquipmentType(Enum):
    DRY_VAN = "Dry van"
    REEFER = "Reefer"
    FLATBED = "Flatbed"


class InvoiceStatus(Enum):
    DRAFT = "Draft"
    SENT = "Sent to broker"
    FACTORED = "Submitted to factoring"
    PARTIALLY_PAID = "Partially paid"
    PAID = "Paid"


class PodStatus(Enum):
    PENDING = "Pending check"
    VERIFIED = "Verified"
    UNCLEAR = "Missing or unclear"
    EXCEPTION = "Damage or shortage noted"


class Entity:
    """Base class: exposes the columns of a row as attributes."""

    def __init__(self, row):
        self.__dict__.update(dict(row))


class Load(Entity):
    @property
    def status_label(self):
        return LoadStatus[self.status].value

    def is_active(self):
        return self.status in ACTIVE_STATUSES

    def can_be_assigned(self):
        """Business rule 1 and 3: only a booked, verified load may be assigned."""
        return self.status == "BOOKED"

    def next_driver_status(self):
        """The next milestone a driver can report, or None."""
        if self.status == "ASSIGNED":
            return LoadStatus.DISPATCHED
        names = [s.name for s in DRIVER_FLOW]
        if self.status in names and names.index(self.status) < len(names) - 1:
            return DRIVER_FLOW[names.index(self.status) + 1]
        return None

    def is_ready_for_invoicing(self, pod_verified):
        """Business rule 5: delivered and proof of delivery verified."""
        return self.status == "DELIVERED" and pod_verified

    def lateness(self, now):
        """Return a short alert text if the load is behind schedule, else ''."""
        pickup, delivery = parse_dt(self.pickup_appt), parse_dt(self.delivery_appt)
        if self.status in ("ASSIGNED", "DISPATCHED") and pickup and pickup < now:
            return "Pickup appointment missed"
        if self.status in ("AT_PICKUP", "LOADED", "IN_TRANSIT", "AT_DELIVERY") and delivery and delivery < now:
            return "Delivery appointment missed"
        return ""


class Invoice(Entity):
    @property
    def status_label(self):
        return InvoiceStatus[self.status].value

    def outstanding(self, paid):
        return round(self.total - paid, 2)

    def is_overdue(self, today, paid):
        return self.outstanding(paid) > 0 and parse_dt(self.due_date + " 00:00").date() < today
