# TransportationOne TMS – prototype (SAD CW2)

High-fidelity, executable prototype (Flask + SQLite) plus the low-fidelity wireframes.

## Run
```
pip install -r requirements.txt
python run.py          # then open http://127.0.0.1:5000
```
The database and demo data are created automatically on first start (folder `instance/`). Delete `instance/` to reset.

## Demo accounts (password `demo123`)
| Role | User name |
|---|---|
| Dispatcher | dispatcher |
| Driver | tom, luis, sam, ana |
| Billing | billing |
| Manager | manager |

## Suggested demo path
1. `dispatcher` → Book a load → "Fill demo booking" → attach `samples/RateConfirmation_AFB-48213.pdf` → Extract and compare → Book load.
2. Assign Luis Ortega, T-201, DV-31.
3. Log in as `luis` → accept → tap through the milestones → upload `samples/POD_AFB-48213.pdf`.
4. `billing` → verify the POD → Create invoice → Send → record payment.
5. `manager` → Reports and Audit log.
Use `samples/RateConfirmation_AFB-48213_mismatch.pdf` to see the on-hold path.

## Folder map
- `tms/domain.py` – enumerations and entity classes (Load, Invoice) with business rules
- `tms/services.py` – application services (one class per area)
- `tms/integration.py` – PdfExtractor, MessengerGateway (simulated), InvoiceDeliveryStrategy
- `tms/db.py` – SQLite schema, connection, demo data
- `tms/web.py` – routes and role-based access
- `tms/templates`, `tms/static` – screens
- `lowfi/SAD_CW2_lowfi.drawio` – low-fidelity wireframes (open at https://app.diagrams.net); `lowfi/lowfi_drawio.png` is the exported image
- `tools/make_samples.py` – regenerates the sample PDFs and image

## Limitations (prototype)
- The PDF extractor is rule-based and reads the sample layout; real broker layouts need OCR/ML. Every value can be corrected before saving.
- Messenger, factoring company and e-mail are simulated (recorded in the log, nothing is sent).
- Single-company demo data; no password reset; not security-hardened for production.
