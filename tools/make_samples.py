"""Create the sample Rate Confirmation and Proof of Delivery files used in the demo.

    python tools/make_samples.py
"""
import os

from PIL import Image, ImageDraw, ImageFilter
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "samples")


def rate_confirmation(name, broker, ref, rate, pickup, delivery, equipment, commodity, weight, notes):
    c = canvas.Canvas(os.path.join(OUT, name), pagesize=letter)
    w, h = letter
    c.setFillColor(colors.HexColor("#14365d"))
    c.rect(0, h - 90, w, 90, fill=1, stroke=0)
    c.setFillColor(colors.white)
    c.setFont("Helvetica-Bold", 20)
    c.drawString(48, h - 52, broker.upper())
    c.setFont("Helvetica", 11)
    c.drawString(48, h - 72, "Carrier Rate Confirmation")
    c.setFillColor(colors.black)
    y = h - 135
    rows = [("Broker", broker), ("Load / Reference #", ref), ("Carrier", "TransportationOne"), ("Rate", f"USD {rate:,.2f}"),
            ("Pickup", pickup), ("Delivery", delivery), ("Equipment", equipment), ("Commodity", commodity),
            ("Weight", f"{weight:,} kg"), ("Special instructions", notes)]
    for label, value in rows:
        c.setFont("Helvetica-Bold", 11)
        c.drawString(48, y, f"{label}:")
        c.setFont("Helvetica", 11)
        c.drawString(210, y, value)
        y -= 26
    c.setStrokeColor(colors.HexColor("#c9d2de"))
    c.line(48, y - 6, w - 48, y - 6)
    c.setFont("Helvetica", 9)
    c.setFillColor(colors.HexColor("#555555"))
    for i, t in enumerate(["Payment terms: 30 days from receipt of invoice and signed proof of delivery.",
                           "Accessorial charges (detention, layover) are paid only with prior written approval and time records.",
                           "Carrier must notify the broker of any delay before the appointment time."]):
        c.drawString(48, y - 28 - i * 14, t)
    c.save()


def pod(name):
    c = canvas.Canvas(os.path.join(OUT, name), pagesize=letter)
    w, h = letter
    c.setFont("Helvetica-Bold", 18)
    c.drawString(48, h - 70, "DELIVERY RECEIPT / PROOF OF DELIVERY")
    c.setFont("Helvetica", 11)
    lines = ["Consignee: Lone Star Distribution Center", "Reference #: AFB-48213", "Carrier: TransportationOne",
             "Received: 18 pallets, packaged food", "Condition: Received in good order, seal intact",
             "Delivered: 2026-11-29 13:40"]
    for i, t in enumerate(lines):
        c.drawString(48, h - 110 - i * 24, t)
    c.setFont("Helvetica", 10)
    c.drawString(48, h - 300, "Receiver signature:")
    c.setStrokeColor(colors.HexColor("#1b2a6b"))
    c.setLineWidth(1.6)
    p = c.beginPath()
    p.moveTo(170, h - 320)
    for x, y in [(190, h - 290), (210, h - 330), (235, h - 285), (255, h - 322), (285, h - 296), (330, h - 318), (390, h - 300)]:
        p.lineTo(x, y)
    c.drawPath(p, stroke=1, fill=0)
    c.line(165, h - 332, 420, h - 332)
    c.save()


def unclear_pod(name):
    img = Image.new("RGB", (900, 1100), "#f1efe9")
    d = ImageDraw.Draw(img)
    for i, t in enumerate(["DELIVERY RECEIPT", "Consignee: Lone Star Distribution", "Ref: AFB-48213", "Received: 18 pallets"]):
        d.text((70, 90 + i * 60), t, fill="#8a8a8a")
    d.line((120, 520, 520, 500), fill="#9a9aa8", width=3)
    img.filter(ImageFilter.GaussianBlur(7)).save(os.path.join(OUT, name))


if __name__ == "__main__":
    os.makedirs(OUT, exist_ok=True)
    common = dict(broker="Atlas Freight Brokers", ref="AFB-48213", pickup="Chicago, IL | 2026-11-27 08:00",
                  equipment="Dry Van", commodity="Packaged food", weight=18500,
                  notes="Call dispatcher 1 hour before arrival. Seal must stay intact.")
    rate_confirmation("RateConfirmation_AFB-48213.pdf", rate=2450.0, delivery="Dallas, TX | 2026-11-29 14:00", **common)
    rate_confirmation("RateConfirmation_AFB-48213_mismatch.pdf", rate=2350.0, delivery="Dallas, TX | 2026-11-29 16:00", **common)
    rate_confirmation("RateConfirmation_SLS-2260_mismatch.pdf", broker="Summit Load Services", ref="SLS-2260", rate=1440.0,
                      pickup="Nashville, TN | 2026-11-27 09:00", delivery="Charlotte, NC | 2026-11-29 12:00",
                      equipment="Dry Van", commodity="Furniture", weight=11000, notes="Two-person unload.")
    pod("POD_AFB-48213.pdf")
    unclear_pod("POD_unclear.png")
    print("samples written to", OUT)
