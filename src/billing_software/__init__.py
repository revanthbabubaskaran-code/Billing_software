from __future__ import annotations

import json
import os
import re
import sqlite3
import calendar
from datetime import date, timedelta
from pathlib import Path
from uuid import uuid4
from xml.sax.saxutils import escape

from flask import Flask, abort, jsonify, redirect, render_template, request, send_file, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.pdfbase.pdfmetrics import stringWidth
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from werkzeug.utils import secure_filename
from waitress import serve

APP_HOME = Path(os.environ.get("BILLING_SOFTWARE_HOME", Path.home() / ".billing-software"))
APP_HOME.mkdir(parents=True, exist_ok=True)
DB_PATH = APP_HOME / "billing.sqlite3"
UPLOADS = APP_HOME / "uploads"
UPLOADS.mkdir(exist_ok=True)
app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as con:
        con.executescript("""
        CREATE TABLE IF NOT EXISTS settings (id INTEGER PRIMARY KEY CHECK (id=1), company TEXT NOT NULL DEFAULT '{}', output_dir TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS profiles (id INTEGER PRIMARY KEY AUTOINCREMENT, company TEXT NOT NULL, output_dir TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL, invoice_date TEXT NOT NULL, customer TEXT NOT NULL, items TEXT NOT NULL, totals TEXT NOT NULL, company_snapshot TEXT NOT NULL, pdf_path TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, cancelled INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS hsn_master (code TEXT PRIMARY KEY, description TEXT NOT NULL, usage TEXT NOT NULL DEFAULT '');
        CREATE TABLE IF NOT EXISTS hsn_catalog_imports (catalog TEXT PRIMARY KEY, imported_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        CREATE TABLE IF NOT EXISTS item_catalog (id INTEGER PRIMARY KEY AUTOINCREMENT, profile_id INTEGER NOT NULL, normalized_description TEXT NOT NULL, description TEXT NOT NULL, hsn TEXT NOT NULL DEFAULT '', unit TEXT NOT NULL DEFAULT '', rate REAL NOT NULL DEFAULT 0, gst REAL NOT NULL DEFAULT 0, uses INTEGER NOT NULL DEFAULT 1, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(profile_id, normalized_description));
        CREATE TABLE IF NOT EXISTS app_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        INSERT OR IGNORE INTO settings(id) VALUES(1);
        """)
        # Import the bundled, GSTN/CBIC-sourced starter headings once. The separate
        # marker lets users edit the master later without startup overwriting edits.
        if not con.execute("SELECT 1 FROM hsn_catalog_imports WHERE catalog='india-gst-hsn-starter-2026-11'").fetchone():
            catalog_path = Path(__file__).with_name("data") / "hsn_starter.json"
            if catalog_path.exists():
                catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
                con.executemany(
                    "INSERT INTO hsn_master(code,description,usage) VALUES(?,?,?) "
                    "ON CONFLICT(code) DO UPDATE SET description=excluded.description, usage=excluded.usage",
                    [(item["code"], item["description"], item["usage"]) for item in catalog["entries"]],
                )
                con.execute("INSERT INTO hsn_catalog_imports(catalog) VALUES('india-gst-hsn-starter-2026-11')")
        # Add the commonly used battery heading without replacing a user's local edit.
        con.execute("INSERT OR IGNORE INTO hsn_master(code,description,usage) VALUES(?,?,?)", ("8506", "Primary cells and primary batteries", "Non-rechargeable primary cells and batteries; examples include alkaline, zinc-carbon and lithium primary cells."))
        sample_headings = [
            ("9987", "Maintenance, repair and installation services (except construction)", "Sample bill examples: P/S service charges, E/W/S service charges, stamping and other expenses; verify the exact SAC for the service."),
            ("8541", "Semiconductor devices and photosensitive semiconductor devices", "Sample bill uses this for Display; check whether the item is a display component or a complete display unit."),
            ("8534", "Printed circuits", "Sample bill uses this for Mother Board; confirm whether the item is a printed circuit board or a complete assembly."),
        ]
        con.executemany("INSERT OR IGNORE INTO hsn_master(code,description,usage) VALUES(?,?,?)", sample_headings)
        setting_columns = {row[1] for row in con.execute("PRAGMA table_info(settings)")}
        if "active_profile_id" not in setting_columns:
            con.execute("ALTER TABLE settings ADD COLUMN active_profile_id INTEGER")
        invoice_columns = {row[1] for row in con.execute("PRAGMA table_info(invoices)")}
        if "profile_id" not in invoice_columns:
            con.execute("ALTER TABLE invoices ADD COLUMN profile_id INTEGER")
        if "cancelled" not in invoice_columns:
            con.execute("ALTER TABLE invoices ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0")
        if "deleted" not in invoice_columns:
            con.execute("ALTER TABLE invoices ADD COLUMN deleted INTEGER NOT NULL DEFAULT 0")
        legacy = con.execute("SELECT company, output_dir, active_profile_id FROM settings WHERE id=1").fetchone()
        profile_count = con.execute("SELECT count(*) FROM profiles").fetchone()[0]
        if not profile_count and json.loads(legacy["company"]).get("name"):
            cur = con.execute("INSERT INTO profiles(company,output_dir) VALUES(?,?)", (legacy["company"], legacy["output_dir"]))
            con.execute("UPDATE settings SET active_profile_id=? WHERE id=1", (cur.lastrowid,))
            con.execute("UPDATE invoices SET profile_id=? WHERE profile_id IS NULL", (cur.lastrowid,))
        # Earlier versions enforced globally unique visible invoice numbers. Annual
        # cycles reuse numbers, so migrate that constraint while preserving row IDs.
        unique_number_index = False
        for index in con.execute("PRAGMA index_list(invoices)").fetchall():
            if index["unique"]:
                columns = [info["name"] for info in con.execute(f"PRAGMA index_info('{index['name']}')").fetchall()]
                if columns == ["number"]:
                    unique_number_index = True
                    break
        if unique_number_index:
            con.execute("ALTER TABLE invoices RENAME TO invoices_unique_number_old")
            con.execute("CREATE TABLE invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT NOT NULL, invoice_date TEXT NOT NULL, customer TEXT NOT NULL, items TEXT NOT NULL, totals TEXT NOT NULL, company_snapshot TEXT NOT NULL, pdf_path TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, cancelled INTEGER NOT NULL DEFAULT 0, deleted INTEGER NOT NULL DEFAULT 0, profile_id INTEGER)")
            con.execute("INSERT INTO invoices(id,number,invoice_date,customer,items,totals,company_snapshot,pdf_path,created_at,cancelled,deleted,profile_id) SELECT id,number,invoice_date,customer,items,totals,company_snapshot,pdf_path,created_at,cancelled,deleted,profile_id FROM invoices_unique_number_old")
            con.execute("DROP TABLE invoices_unique_number_old")
        con.execute("CREATE INDEX IF NOT EXISTS invoices_profile_cycle_lookup ON invoices(profile_id,invoice_date,id)")
        # Seed suggestions from saved invoices once so existing history is useful immediately.
        if not con.execute("SELECT 1 FROM app_migrations WHERE name='item-catalog-from-invoices-v1'").fetchone():
            for row in con.execute("SELECT profile_id,items FROM invoices WHERE profile_id IS NOT NULL").fetchall():
                try:
                    saved_items = json.loads(row["items"])
                except (TypeError, json.JSONDecodeError):
                    continue
                for item in saved_items:
                    description = str(item.get("description", "")).strip()
                    if not description:
                        continue
                    con.execute(
                        "INSERT OR IGNORE INTO item_catalog(profile_id,normalized_description,description,hsn,unit,rate,gst) VALUES(?,?,?,?,?,?,?)",
                        (row["profile_id"], description.casefold(), description, str(item.get("hsn", "")), str(item.get("unit", "")), float(item.get("rate", 0) or 0), float(item.get("gst", 0) or 0)),
                    )
            con.execute("INSERT INTO app_migrations(name) VALUES('item-catalog-from-invoices-v1')")


def remember_items(con, profile_id, items):
    if not profile_id:
        return
    for item in items:
        description = str(item.get("description", "")).strip()
        normalized = description.casefold()
        if not normalized:
            continue
        con.execute(
            "INSERT INTO item_catalog(profile_id,normalized_description,description,hsn,unit,rate,gst) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(profile_id,normalized_description) DO UPDATE SET description=excluded.description, hsn=excluded.hsn, unit=excluded.unit, rate=excluded.rate, gst=excluded.gst, uses=item_catalog.uses+1, updated_at=CURRENT_TIMESTAMP",
            (profile_id, normalized, description, str(item.get("hsn", "")), str(item.get("unit", "")), float(item.get("rate", 0) or 0), float(item.get("gst", 0) or 0)),
        )


init_db()


def settings():
    with db() as con:
        active = con.execute("SELECT active_profile_id FROM settings WHERE id=1").fetchone()[0]
        row = con.execute("SELECT * FROM profiles WHERE id=?", (active,)).fetchone() if active else None
    if not row:
        return {"company": {}, "output_dir": "", "profile_id": None}
    company = json.loads(row["company"])
    company["gstin"] = str(company.get("gstin", "")).strip().upper()
    return {"company": company, "output_dir": row["output_dir"], "profile_id": row["id"]}


def profile_initials(name):
    words = re.findall(r"[A-Za-z0-9]+", name or "")
    if len(words) > 1:
        return "".join(word[0] for word in words[:2]).upper()
    return (words[0][:2] if words else "?").upper()


def profile_cards():
    with db() as con:
        rows = con.execute("SELECT id,company FROM profiles ORDER BY id").fetchall()
    result = []
    for row in rows:
        company = json.loads(row["company"])
        company["gstin"] = str(company.get("gstin", "")).strip().upper()
        result.append({"id": row["id"], "company": company, "initials": profile_initials(company.get("name", ""))})
    return result


def money(value):
    return round(float(value or 0), 2)


def capitalize_item_description(value):
    text = str(value or "").strip()
    return re.sub(r"(^\s*|[/,&]\s*)([^\W\d_])", lambda match: match.group(1) + match.group(2).upper(), text)


def totals_for(items, tax_type="intra", roundoff=0):
    base = tax = cgst = sgst = igst = 0.0
    cleaned = []
    for item in items:
        name = capitalize_item_description(item.get("description", ""))
        qty, rate, gst = float(item.get("quantity", 0)), money(item.get("rate")), float(item.get("gst", 0))
        hsn, unit = str(item.get("hsn", "")).strip(), str(item.get("unit", "")).strip()
        if not name or qty <= 0 or rate < 0 or gst not in (0, 5, 18):
            raise ValueError("Enter a description, quantity above zero, valid rate, and GST of 0%, 5%, or 18% for every item.")
        line_base = money(qty * rate)
        line_tax = money(line_base * gst / 100)
        line_cgst = money(line_tax / 2) if tax_type == "intra" else 0
        line_sgst = money(line_tax - line_cgst) if tax_type == "intra" else 0
        line_igst = money(line_tax) if tax_type == "inter" else 0
        cleaned.append({"description": name, "hsn": hsn, "quantity": qty, "unit": unit, "rate": rate, "gst": gst, "taxable": money(line_base), "tax": money(line_tax), "cgst": line_cgst, "sgst": line_sgst, "igst": line_igst, "total": money(line_base + line_tax)})
        base += money(line_base)
        tax += money(line_tax)
        cgst += line_cgst
        sgst += line_sgst
        igst += line_igst
    if not cleaned:
        raise ValueError("Add at least one line item.")
    roundoff = money(roundoff)
    return cleaned, {"taxable": money(base), "tax": money(tax), "cgst": money(cgst), "sgst": money(sgst), "igst": money(igst), "roundoff": roundoff, "total": money(base + tax + roundoff)}


def amount_in_words(value):
    ones = ("Zero", "One", "Two", "Three", "Four", "Five", "Six", "Seven", "Eight", "Nine", "Ten", "Eleven", "Twelve", "Thirteen", "Fourteen", "Fifteen", "Sixteen", "Seventeen", "Eighteen", "Nineteen")
    tens = ("", "", "Twenty", "Thirty", "Forty", "Fifty", "Sixty", "Seventy", "Eighty", "Ninety")
    def under_thousand(number):
        # Crore groups can themselves exceed 999 on large invoices. Split them
        # recursively before indexing the hundreds/tens word tables.
        if number >= 1000:
            thousands, remainder = divmod(number, 1000)
            words = under_thousand(thousands) + " Thousand"
            return words + (" " + under_thousand(remainder) if remainder else "")
        words = []
        if number >= 100:
            words.extend((ones[min(number // 100, 9)], "Hundred"))
            number %= 100
        if number >= 20:
            words.append(tens[min(number // 10, 9)])
            number %= 10
        if number > 0:
            words.append(ones[min(number, 19)])
        return " ".join(words)
    amount = int(round(float(value) * 100))
    sign = "Minus " if amount < 0 else ""
    rupees, paise = divmod(abs(amount), 100)
    parts = []
    for size, label in ((10000000, "Crore"), (100000, "Lakh"), (1000, "Thousand"), (1, "")):
        count, rupees = divmod(rupees, size)
        if count:
            parts.extend((under_thousand(count), label) if label else (under_thousand(count),))
    words = " ".join(parts) or "Zero"
    if paise:
        return f"{sign}Rupees {words} and {under_thousand(paise)} Paise only"
    return f"{sign}Rupees {words} only"


def cancellation_deadline(invoice_date):
    issued = date.fromisoformat(invoice_date)
    next_year, next_month = (issued.year + (issued.month == 12), 1 if issued.month == 12 else issued.month + 1)
    return date(next_year, next_month, 1) - timedelta(days=1)


def billing_cycle_period(invoice_date, company):
    if not company.get("billing_cycle_enabled"):
        return "continuous"
    issued = date.fromisoformat(str(invoice_date)) if not isinstance(invoice_date, date) else invoice_date
    month = int(company.get("billing_cycle_month", 4))
    day = int(company.get("billing_cycle_day", 1))
    start = date(issued.year, month, day)
    if issued < start:
        start = date(issued.year - 1, month, day)
    return start.isoformat()


def next_bill_sequence(con, profile_id, company, invoice_date):
    period = billing_cycle_period(invoice_date, company)
    invoices = con.execute("SELECT number,invoice_date FROM invoices WHERE profile_id=? ORDER BY id DESC", (profile_id,)).fetchall()
    for row in invoices:
        if billing_cycle_period(row["invoice_date"], company) != period:
            continue
        match = re.search(r"(\d+)$", row["number"])
        if match:
            return int(match.group(1)) + 1
    if invoices and company.get("billing_cycle_enabled"):
        return 1
    return int(company.get("starting_bill_number", 1))


def format_bill_number(company, bill_year, sequence):
    mode = company.get("invoice_format", "inv_year")
    if mode == "number":
        return str(sequence)
    if mode == "padded3":
        return str(sequence).zfill(3)
    return f"INV-{bill_year}-{sequence:03d}"


def next_bill_number(con, profile_id, company, invoice_date):
    issued = date.fromisoformat(str(invoice_date)) if not isinstance(invoice_date, date) else invoice_date
    period = billing_cycle_period(issued, company)
    sequence = next_bill_sequence(con, profile_id, company, issued)
    number = format_bill_number(company, issued.year, sequence)
    existing = con.execute("SELECT number,invoice_date FROM invoices WHERE profile_id=?", (profile_id,)).fetchall()
    while any(
        row["number"] == number and billing_cycle_period(row["invoice_date"], company) == period
        for row in existing
    ):
        sequence += 1
        number = format_bill_number(company, issued.year, sequence)
    return number


def invoice_address(details):
    lines = [str(details.get("address", "")).strip()]
    if details.get("district"):
        lines.append(str(details["district"]).strip())
    if details.get("pincode"):
        lines.append("PIN: " + str(details["pincode"]).strip())
    return "\n".join(line for line in lines if line)


def make_pdf(path, invoice, company, logo_path=None, cancelled=False):
    template = company.get("invoice_template", "clean")
    classic_template = template == "classic_boxed"
    boxed_parties = template in ("classic_boxed", "modern_panels", "compact_ledger")
    compact_template = template == "compact_ledger"
    formal_template = template == "formal_header"

    def draw_watermark(canvas, doc):
        # A bill is always rendered on a plain white sheet, independent of the
        # application theme. The uploaded image is the only color artwork.
        canvas.saveState()
        canvas.setFillColor(colors.white)
        canvas.rect(0, 0, A4[0], A4[1], fill=1, stroke=0)
        canvas.restoreState()
        if not logo_path or not Path(logo_path).is_file():
            return
        try:
            canvas.saveState()
            canvas.setFillAlpha(.10)
            mark_width = 95*mm
            mark_height = 60*mm
            canvas.drawImage(logo_path, (A4[0]-mark_width)/2, (A4[1]-mark_height)/2, width=mark_width, height=mark_height, preserveAspectRatio=True, anchor="c", mask="auto")
            canvas.restoreState()
        except Exception:
            try:
                canvas.restoreState()
            except Exception:
                pass

    def draw_cancelled(canvas, doc):
        if not cancelled:
            return
        canvas.saveState()
        canvas.translate(A4[0]/2, A4[1]/2)
        canvas.rotate(35)
        canvas.setFillColor(colors.black)
        canvas.setFillAlpha(.24)
        canvas.setFont("Helvetica-Bold", 50)
        canvas.drawCentredString(0, 0, "CANCELLED")
        canvas.restoreState()

    def draw_page(canvas, doc):
        draw_watermark(canvas, doc)
        draw_cancelled(canvas, doc)
        if classic_template:
            canvas.saveState()
            canvas.setStrokeColor(colors.black)
            canvas.setLineWidth(.6)
            canvas.rect(7*mm, 7*mm, A4[0]-14*mm, A4[1]-14*mm, fill=0, stroke=1)
            canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=17*mm, leftMargin=17*mm, topMargin=9*mm, bottomMargin=9*mm, title=f"Tax invoice {invoice['number']}")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="InvoiceSmall", parent=styles["BodyText"], fontSize=9, leading=11, textColor=colors.black))
    styles.add(ParagraphStyle(name="InvoiceBody", parent=styles["BodyText"], fontSize=9.5, leading=12, textColor=colors.black))
    styles.add(ParagraphStyle(name="InvoiceAddress", parent=styles["BodyText"], fontSize=9.5, leading=12, alignment=1, textColor=colors.black))
    styles.add(ParagraphStyle(name="InvoiceMeta", parent=styles["BodyText"], fontSize=9.5, leading=12, textColor=colors.black))
    styles.add(ParagraphStyle(name="InvoiceMetaRight", parent=styles["InvoiceMeta"], alignment=2))
    styles.add(ParagraphStyle(name="InvoiceContact", parent=styles["BodyText"], fontSize=8.5, leading=10, alignment=0, textColor=colors.black))
    styles.add(ParagraphStyle(name="InvoiceContactCenter", parent=styles["InvoiceContact"], alignment=1))
    styles.add(ParagraphStyle(name="InvoiceContactRight", parent=styles["InvoiceContact"], alignment=2))
    styles.add(ParagraphStyle(name="InvoiceFooter", parent=styles["InvoiceSmall"], alignment=1))
    styles.add(ParagraphStyle(name="InvoicePartyName", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=9.5, leading=12, textColor=colors.black))
    styles.add(ParagraphStyle(name="InvoiceGrand", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=14, leading=17, alignment=2, textColor=colors.black))
    styles.add(ParagraphStyle(name="InvoiceColumnHeading", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=9.5, leading=11, alignment=1, textColor=colors.black))
    styles.add(ParagraphStyle(name="InvoiceAmountWords", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=colors.black))
    styles.add(ParagraphStyle(name="InvoiceBankTitle", parent=styles["BodyText"], fontName="Helvetica-Bold", fontSize=11.5, leading=14, textColor=colors.black))
    story = []
    safe = lambda value: escape(str(value or ""))
    content_width = A4[0] - 34*mm
    company_name_text = str(company.get("name") or "Company")
    company_name = safe(company_name_text)
    address_text = str(company.get("address") or "")
    address = safe(address_text).replace("\n", "<br/>")
    company_info_width = content_width-64*mm
    address_line_width = max((stringWidth(line, "Helvetica", 9.5) for line in address_text.splitlines() if line.strip()), default=company_info_width)
    company_target_width = min(company_info_width-2*mm, address_line_width)
    company_font_size = min(40, max(10, company_target_width / max(1, stringWidth(company_name_text, "Helvetica-Bold", 1))))
    company_name_style = ParagraphStyle(name="InvoiceCompanyDynamic", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=company_font_size, leading=company_font_size*1.15, alignment=1, textColor=colors.black, spaceAfter=1*mm)
    company_info = Table([[Paragraph(company_name, company_name_style)], [Paragraph(address, styles["InvoiceAddress"])]] , colWidths=[company_info_width])
    company_info.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"CENTER"),("LEFTPADDING",(0,0),(-1,-1),1),("RIGHTPADDING",(0,0),(-1,-1),1),("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),3)]))
    logo_cell = ""
    if logo_path and Path(logo_path).is_file():
        try:
            logo_cell = Image(logo_path, width=29*mm, height=23*mm, kind="proportional")
        except Exception:
            logo_cell = ""
    header = Table([[logo_cell, company_info, Paragraph("Original Copy", styles["InvoiceMetaRight"])]], colWidths=[32*mm, company_info_width, 32*mm], hAlign="LEFT")
    header.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("RIGHTPADDING",(0,0),(0,0),4),("LEFTPADDING",(1,0),(1,0),2),("RIGHTPADDING",(1,0),(1,0),2),("LEFTPADDING",(2,0),(2,0),1),("RIGHTPADDING",(2,0),(2,0),0),("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1)]))
    story += [header, Spacer(1, 2*mm)]
    contact_cells = [[Paragraph("<b>Contact No:</b> " + safe(company.get("phone")), styles["InvoiceContact"]), Paragraph("<b>Email:</b> " + safe(company.get("email")), styles["InvoiceContactCenter"]), Paragraph("<b>GSTIN:</b> " + safe(company.get("gstin")), styles["InvoiceContactRight"])]]
    contact_widths = [content_width*.29, content_width*.42, content_width*.29]
    contact = Table(contact_cells, colWidths=contact_widths)
    contact.setStyle(TableStyle([("BOX",(0,0),(-1,-1),.6,colors.black),("LINEBEFORE",(1,0),(1,0),.5,colors.black),("LINEBEFORE",(2,0),(2,0),.5,colors.black),("ALIGN",(0,0),(0,0),"LEFT"),("ALIGN",(1,0),(1,0),"CENTER"),("ALIGN",(2,0),(2,0),"RIGHT"),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)]))
    story += [contact, Spacer(1, 2*mm)]
    if company.get("header"):
        story += [Paragraph(safe(company["header"]), styles["InvoiceSmall"]), Spacer(1, 1*mm)]
    raw_invoice_date = invoice.get("date", "")
    try:
        invoice_date = date.fromisoformat(str(raw_invoice_date)).strftime("%d-%m-%Y")
    except ValueError:
        invoice_date = str(raw_invoice_date)
    reverse_charge = safe(invoice.get("reverse_charge") or "No")
    warranty = safe(invoice.get("warranty") or "-Nil-")
    meta_left = Paragraph(f"<b>Invoice No:</b> {safe(invoice['number'])}<br/><b>Date:</b> {safe(invoice_date)}", styles["InvoiceMeta"])
    meta_right = Paragraph(f"<b>Reverse Charge:</b> {reverse_charge}<br/><b>Warranty:</b> {warranty}", styles["InvoiceMetaRight"])
    meta = Table([[meta_left, meta_right]], colWidths=[content_width/2, content_width/2])
    meta_style = [("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)]
    if formal_template:
        meta_style.append(("BOX",(0,0),(-1,-1),.5,colors.black))
    if compact_template:
        meta_style.extend([("TOPPADDING",(0,0),(-1,-1),2),("BOTTOMPADDING",(0,0),(-1,-1),2)])
    meta.setStyle(TableStyle(meta_style))
    story += [meta, Spacer(1, 1*mm)]
    customer = invoice["customer"]
    ship_to = invoice.get("ship_to", {})
    def party_block(label, details):
        address_lines = safe(invoice_address(details) or "-Nil-").replace("\n", "<br/>")
        lines = [Paragraph(safe(label), styles["InvoiceSmall"]), Paragraph(safe(details.get("name") or "-Nil-"), styles["InvoicePartyName"]), Paragraph(address_lines, styles["InvoiceBody"]), Paragraph("<b>GSTIN / UIN:</b> " + safe(details.get("gstin") or "-Nil-"), styles["InvoiceSmall"])]
        if details.get("phone"):
            lines.append(Paragraph("<b>Contact No:</b> " + safe(details["phone"]), styles["InvoiceSmall"]))
        return lines
    if boxed_parties:
        parties = Table([[party_block("Billed To:", customer), party_block("Shipped / work location:", ship_to)]], colWidths=[content_width/2,content_width/2])
        party_style = [("BOX",(0,0),(-1,-1),.55,colors.black),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]
        if classic_template or compact_template:
            party_style.append(("INNERGRID",(0,0),(-1,-1),.4,colors.black))
        parties.setStyle(TableStyle(party_style))
    else:
        party_gutter = 10*mm
        party_width = (content_width-party_gutter)/2
        parties = Table([[party_block("Billed To:", customer), "", party_block("Shipped / work location:", ship_to)]], colWidths=[party_width,party_gutter,party_width])
        parties.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),3),("RIGHTPADDING",(0,0),(-1,-1),3),("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3)]))
    story += [parties, Spacer(1, 2*mm)]
    item_rows = [[Paragraph("S.No", styles["InvoiceColumnHeading"]), Paragraph("Description of goods /<br/>Service charges", styles["InvoiceColumnHeading"]), Paragraph("HSN / SAC<br/>Code", styles["InvoiceColumnHeading"]), Paragraph("Qty", styles["InvoiceColumnHeading"]), Paragraph("Unit", styles["InvoiceColumnHeading"]), Paragraph("Price", styles["InvoiceColumnHeading"]), Paragraph("Amount", styles["InvoiceColumnHeading"])] ]
    for index, item in enumerate(invoice["items"], 1):
        item_description = safe(item.get("description")).replace("\n", "<br/>")
        item_rows.append([str(index), Paragraph(item_description, styles["InvoiceSmall"]), safe(item.get("hsn")), f"{item['quantity']:g}", safe(item.get("unit")), f"{item['rate']:,.2f}", f"{item['taxable']:,.2f}"])
    item_widths = [12*mm, 57*mm, 23*mm, 14*mm, 13*mm, 27*mm, 30*mm]
    item_table = Table(item_rows, colWidths=item_widths, repeatRows=1)
    item_table_style = [("BACKGROUND",(0,0),(-1,0),colors.white),("TEXTCOLOR",(0,0),(-1,-1),colors.black),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),8),("BOX",(0,0),(-1,-1),.55,colors.black),("LINEBELOW",(0,0),(-1,0),.55,colors.black),("ALIGN",(0,1),(-1,-1),"CENTER"),("ALIGN",(1,1),(1,-1),"LEFT"),("ALIGN",(5,1),(-1,-1),"RIGHT"),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)]
    if classic_template or compact_template:
        item_table_style.append(("GRID",(0,0),(-1,-1),.4,colors.black))
    else:
        item_table_style.extend(("LINEBEFORE",(column,0),(column,-1),.4,colors.black) for column in range(1,len(item_widths)))
    if compact_template:
        item_table_style.extend([("FONTSIZE",(0,0),(-1,-1),7.2),("TOPPADDING",(0,0),(-1,-1),2),("BOTTOMPADDING",(0,0),(-1,-1),2)])
    item_table.setStyle(TableStyle(item_table_style))
    story += [item_table, Spacer(1, 2*mm)]
    totals = invoice["totals"]
    groups = []
    for rate in sorted({item["gst"] for item in invoice["items"]}):
        group = [item for item in invoice["items"] if item["gst"] == rate]
        taxable = sum(item["taxable"] for item in group)
        if invoice["tax_type"] == "intra":
            tax_rows = [f"{sum(item['cgst'] for item in group):,.2f}", f"{sum(item['sgst'] for item in group):,.2f}"]
        else:
            tax_rows = [f"{sum(item['igst'] for item in group):,.2f}"]
        groups.append([f"{rate:g}%", f"{taxable:,.2f}", *tax_rows, "", f"{taxable + sum(item['tax'] for item in group):,.2f}"])
    if invoice["tax_type"] == "intra":
        tax_header = ["Tax Rate", "Taxable Amt.", "CGST Amt.", "SGST Amt.", "Round off", "Total"]
        tax_total = ["Total", f"{totals['taxable']:,.2f}", f"{totals['cgst']:,.2f}", f"{totals['sgst']:,.2f}", f"{totals['roundoff']:,.2f}", f"{totals['total']:,.2f}"]
        tax_widths = [20*mm, 37*mm, 31*mm, 31*mm, 28*mm, 19*mm]
    else:
        tax_header = ["Tax Rate", "Taxable Amt.", "IGST Amt.", "Round off", "Total"]
        tax_total = ["Total", f"{totals['taxable']:,.2f}", f"{totals['igst']:,.2f}", f"{totals['roundoff']:,.2f}", f"{totals['total']:,.2f}"]
        tax_widths = [24*mm, 43*mm, 39*mm, 32*mm, 28*mm]
    tax_table = Table([tax_header, *groups, tax_total], colWidths=tax_widths, hAlign="CENTER")
    tax_style = [("TEXTCOLOR",(0,0),(-1,-1),colors.black),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),8.5),("LINEBELOW",(0,0),(-1,0),.6,colors.black),("LINEABOVE",(0,-1),(-1,-1),.6,colors.black),("ALIGN",(0,0),(-1,-1),"CENTER"),("ALIGN",(1,1),(-1,-1),"RIGHT"),("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3)]
    tax_table.setStyle(TableStyle(tax_style))
    grand = Table([[Paragraph("Grand total:", styles["InvoiceGrand"]), Paragraph(f"Rs. {totals['total']:,.2f}", styles["InvoiceGrand"])]], colWidths=[content_width-54*mm,54*mm], hAlign="RIGHT")
    grand.setStyle(TableStyle([("LINEABOVE",(0,0),(-1,0),.6,colors.black),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),3)]))
    words = Paragraph(safe(amount_in_words(totals["total"])), styles["InvoiceAmountWords"])
    story += [tax_table, grand, Table([[words]], colWidths=[content_width], hAlign="RIGHT", style=TableStyle([("ALIGN",(0,0),(-1,-1),"RIGHT"), ("TOPPADDING",(0,0),(-1,-1),1), ("BOTTOMPADDING",(0,0),(-1,-1),2)]))]
    bank = company.get("bank") or {}
    reserve_width = content_width/2
    left_details = []
    if any(bank.get(key) for key in ("name", "account", "ifsc", "branch")):
        bank_rows = [
            [Paragraph("<b>Bank name:</b> " + safe(bank.get("name") or "-Nil-"), styles["InvoiceSmall"]), Paragraph("<b>Account no.:</b> " + safe(bank.get("account") or "-Nil-"), styles["InvoiceSmall"])],
            [Paragraph("<b>IFSC code:</b> " + safe(bank.get("ifsc") or "-Nil-"), styles["InvoiceSmall"]), Paragraph("<b>Branch:</b> " + safe(bank.get("branch") or "-Nil-"), styles["InvoiceSmall"])],
        ]
        bank_table = Table(bank_rows, colWidths=[content_width/2,content_width/2])
        bank_table.setStyle(TableStyle([("BOX",(0,0),(-1,-1),.45,colors.black),("INNERGRID",(0,0),(-1,-1),.35,colors.black),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),3),("RIGHTPADDING",(0,0),(-1,-1),3),("TOPPADDING",(0,0),(-1,-1),3),("BOTTOMPADDING",(0,0),(-1,-1),3)]))
        story += [Spacer(1,2*mm), Paragraph("Bank Details", styles["InvoiceBankTitle"]), bank_table, Spacer(1,2*mm)]
    if invoice.get("terms"):
        left_details.append(Paragraph("<b>Terms &amp; Conditions</b><br/>" + safe(invoice.get("terms")).replace("\n", "<br/>"), styles["InvoiceSmall"]))
    signing_name = safe(company.get("name") or "")
    signing_space = [Paragraph("<b>For " + signing_name + "</b>", styles["InvoiceMetaRight"]), Spacer(1, 4*mm)]
    signature_path = company.get("signature", "")
    if signature_path and Path(signature_path).is_file():
        try:
            signing_space += [Image(signature_path, width=32*mm, height=10*mm, kind="proportional"), Paragraph("Digitally signed", styles["InvoiceMetaRight"]), Spacer(1, 1*mm)]
        except Exception:
            signing_space.append(Spacer(1, 8*mm))
    else:
        signing_space.append(Spacer(1, 8*mm))
    signing_space.append(Paragraph("Authorized Signatory", styles["InvoiceMetaRight"]))
    detail_cells = [[left_details, signing_space]]
    details = Table(detail_cells, colWidths=[reserve_width,reserve_width], hAlign="LEFT")
    details.setStyle(TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),2),("BOTTOMPADDING",(0,0),(-1,-1),2)]))
    story += [Spacer(1,2*mm), details]
    if company.get("footer"):
        story += [Spacer(1, 1*mm), Paragraph(safe(company["footer"]), styles["InvoiceFooter"])]
    elif company.get("header"):
        story += [Spacer(1, 1*mm), Paragraph("This is a computer-generated tax invoice.", styles["InvoiceFooter"])]
    doc.build(story, onFirstPage=draw_page, onLaterPages=draw_page)


@app.get("/")
def home():
    return render_template("profiles.html", cfg=settings(), profiles=profile_cards())


@app.post("/select-profile/<int:profile_id>")
def select_profile(profile_id):
    with db() as con:
        exists = con.execute("SELECT 1 FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not exists:
            abort(404)
        con.execute("UPDATE settings SET active_profile_id=? WHERE id=1", (profile_id,))
    return redirect(url_for("dashboard"))


@app.get("/dashboard")
def dashboard():
    cfg = settings()
    if not cfg["company"].get("name"):
        return redirect(url_for("home"))
    with db() as con:
        invoices = con.execute("SELECT id,number,invoice_date,customer,totals FROM invoices WHERE profile_id=? AND cancelled=0 AND deleted=0 ORDER BY id DESC LIMIT 8", (cfg["profile_id"],)).fetchall()
        count = con.execute("SELECT count(*) FROM invoices WHERE profile_id=? AND cancelled=0 AND deleted=0", (cfg["profile_id"],)).fetchone()[0]
        total = con.execute("SELECT COALESCE(sum(json_extract(totals,'$.total')),0) FROM invoices WHERE profile_id=? AND cancelled=0 AND deleted=0", (cfg["profile_id"],)).fetchone()[0]
    return render_template("home.html", cfg=cfg, invoices=[{**dict(x), "customer": json.loads(x["customer"]), "totals": json.loads(x["totals"])} for x in invoices], count=count, total=total)


@app.route("/hsn-master", methods=["GET", "POST"])
def hsn_master():
    error = None
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        description = request.form.get("description", "").strip()
        usage = request.form.get("usage", "").strip()
        if not re.fullmatch(r"\d{4}", code):
            error = "Enter a four-digit HSN heading."
        elif not description or not usage:
            error = "Add both a description and a suggested use."
        else:
            with db() as con:
                con.execute("INSERT INTO hsn_master(code,description,usage) VALUES(?,?,?) ON CONFLICT(code) DO UPDATE SET description=excluded.description, usage=excluded.usage", (code, description, usage))
            return redirect(url_for("hsn_master"))
    with db() as con:
        codes = con.execute("SELECT code,description,usage FROM hsn_master ORDER BY code").fetchall()
    return render_template("hsn_master.html", cfg=settings(), codes=codes, error=error)


@app.get("/item-suggest")
def item_suggest():
    cfg = settings()
    query = request.args.get("q", "").strip().casefold()
    if len(query) < 3 or not cfg["profile_id"]:
        return jsonify([])
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    with db() as con:
        rows = con.execute(
            "SELECT description,hsn,unit,rate,gst,uses FROM item_catalog "
            "WHERE profile_id=? AND normalized_description LIKE ? ESCAPE '\\' "
            "ORDER BY uses DESC, updated_at DESC, description COLLATE NOCASE LIMIT 10",
            (cfg["profile_id"], escaped + "%"),
        ).fetchall()
    return jsonify([dict(row) for row in rows])


@app.get("/hsn-suggest")
def hsn_suggest():
    query = request.args.get("q", "").strip().lower()
    if len(query) < 3:
        return jsonify([])
    pattern = f"%{query}%"
    with db() as con:
        rows = con.execute("SELECT code,description,usage FROM hsn_master WHERE code LIKE ? OR lower(description) LIKE ? OR lower(usage) LIKE ? ORDER BY CASE WHEN code=? THEN 0 WHEN code LIKE ? THEN 1 ELSE 2 END, code LIMIT 12", (pattern, pattern, pattern, query, f"{query}%")).fetchall()
        preferred_codes = []
        if "batter" in query:
            preferred_codes.extend(("8507", "8506"))
        if "powercord" in query or "power cord" in query or "power-cord" in query:
            preferred_codes.append("8544")
        if "transformer" in query:
            preferred_codes.append("8504")
        if "display" in query:
            preferred_codes.append("8541")
        if "motherboard" in query or "mother board" in query:
            preferred_codes.append("8534")
        if "loadcell" in query or "load cell" in query:
            preferred_codes.append("8423")
        if "switch" in query:
            preferred_codes.append("8536")
        if (("service" in query and "charge" in query) or "stamping" in query
                or "p/s" in query or "e/w/s" in query or "other expenses" in query):
            preferred_codes.append("9987")
        if preferred_codes:
            preferred_codes = list(dict.fromkeys(preferred_codes))
            preferred_rows = con.execute(
                f"SELECT code,description,usage FROM hsn_master WHERE code IN ({','.join('?' for _ in preferred_codes)})",
                tuple(preferred_codes),
            ).fetchall()
            by_code = {row["code"]: row for row in preferred_rows}
            ordered = [by_code[code] for code in preferred_codes if code in by_code]
            seen = {row["code"] for row in ordered}
            rows = ordered + [row for row in rows if row["code"] not in seen]
    return jsonify([dict(row) for row in rows])


@app.route("/setup", methods=["GET", "POST"])
@app.route("/setup/new", methods=["GET", "POST"])
def setup():
    is_new = request.path.endswith("/new")
    current = settings()
    requested_id = request.args.get("profile_id", type=int)
    profile_id = None if is_new else (requested_id or current["profile_id"])
    cfg = {"company": {}, "output_dir": "", "profile_id": None}
    if profile_id:
        with db() as con:
            profile = con.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
        if not profile:
            abort(404)
        company = json.loads(profile["company"])
        company["gstin"] = str(company.get("gstin", "")).strip().upper()
        cfg = {"company": company, "output_dir": profile["output_dir"], "profile_id": profile["id"]}
    if request.method == "POST":
        company = {k: request.form.get(k, "").strip() for k in ("name", "gstin", "address", "phone", "email", "header", "footer", "terms")}
        company["invoice_format"] = request.form.get("invoice_format", "inv_year").strip()
        if company["invoice_format"] not in ("number", "padded3", "inv_year"):
            return render_template("setup.html", cfg=cfg, is_new=not profile_id, error="Choose a valid invoice number format.")
        company["invoice_template"] = request.form.get("invoice_template", "clean").strip()
        if company["invoice_template"] not in ("clean", "classic_boxed", "modern_panels", "compact_ledger", "formal_header"):
            return render_template("setup.html", cfg=cfg, is_new=not profile_id, error="Choose one of the available bill templates.")
        company["billing_cycle_enabled"] = request.form.get("billing_cycle_enabled") == "on"
        if company["billing_cycle_enabled"]:
            try:
                cycle_month = int(request.form.get("billing_cycle_month", "4"))
                cycle_day = int(request.form.get("billing_cycle_day", "1"))
                if not 1 <= cycle_month <= 12 or not 1 <= cycle_day <= calendar.monthrange(2001, cycle_month)[1]:
                    raise ValueError
            except ValueError:
                return render_template("setup.html", cfg=cfg, is_new=not profile_id, error="Choose a valid recurring billing cycle day and month.")
            company["billing_cycle_month"] = cycle_month
            company["billing_cycle_day"] = cycle_day
        else:
            company["billing_cycle_month"] = int(cfg["company"].get("billing_cycle_month", 4))
            company["billing_cycle_day"] = int(cfg["company"].get("billing_cycle_day", 1))
        company["gstin"] = company["gstin"].upper()
        company["bank"] = {k: request.form.get("bank_" + k, "").strip() for k in ("name", "account", "ifsc", "branch")}
        try:
            company["starting_bill_number"] = int(cfg["company"].get("starting_bill_number", 1)) if profile_id else int(request.form.get("starting_bill_number", "1"))
            if company["starting_bill_number"] < 1:
                raise ValueError
        except ValueError:
            return render_template("setup.html", cfg=cfg, is_new=not profile_id, error="Starting bill number must be a positive whole number.")
        if not company["name"] or not company["gstin"] or not company["address"]:
            return render_template("setup.html", cfg=cfg, is_new=not profile_id, error="Company name, GSTIN, and address are required.")
        logo = request.files.get("logo")
        if logo and logo.filename:
            ext = Path(secure_filename(logo.filename)).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg"):
                return render_template("setup.html", cfg=cfg, is_new=not profile_id, error="Logo must be a PNG or JPG image.")
            logo_path = UPLOADS / f"company-logo-{uuid4().hex}{ext}"
            logo.save(logo_path)
            company["logo"] = str(logo_path)
        else:
            company["logo"] = cfg["company"].get("logo", "")
        signature = request.files.get("signature")
        if signature and signature.filename:
            ext = Path(secure_filename(signature.filename)).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg"):
                return render_template("setup.html", cfg=cfg, is_new=not profile_id, error="Signature must be a PNG or JPG image.")
            signature_path = UPLOADS / f"company-signature-{uuid4().hex}{ext}"
            signature.save(signature_path)
            company["signature"] = str(signature_path)
        else:
            company["signature"] = cfg["company"].get("signature", "")
        out = Path(request.form.get("output_dir", "").strip()).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return render_template("setup.html", cfg=cfg, is_new=not profile_id, error=f"Could not use that bill folder: {exc}")
        with db() as con:
            if profile_id:
                con.execute("UPDATE profiles SET company=?, output_dir=? WHERE id=?", (json.dumps(company), str(out.resolve()), profile_id))
                saved_id = profile_id
            else:
                cur = con.execute("INSERT INTO profiles(company,output_dir) VALUES(?,?)", (json.dumps(company), str(out.resolve())))
                saved_id = cur.lastrowid
            con.execute("UPDATE settings SET active_profile_id=? WHERE id=1", (saved_id,))
        return redirect(url_for("dashboard"))
    return render_template("setup.html", cfg=cfg, is_new=not profile_id, error=None)


@app.get("/new")
def new_invoice():
    cfg = settings()
    if not cfg["company"].get("name"):
        return redirect(url_for("home"))
    with db() as con:
        today = date.today()
        preview_number = next_bill_number(con, cfg["profile_id"], cfg["company"], today)
        preview_sequence = int(re.search(r"(\d+)$", preview_number).group(1))
        last = con.execute("SELECT invoice_date FROM invoices WHERE profile_id=? ORDER BY id DESC LIMIT 1", (cfg["profile_id"],)).fetchone()
        cancellations = con.execute("SELECT id,number,invoice_date,customer FROM invoices WHERE profile_id=? AND cancelled=0 AND deleted=0 ORDER BY id DESC", (cfg["profile_id"],)).fetchall()
    available_cancellations = [dict(row) | {"customer": json.loads(row["customer"])} for row in cancellations if date.today() <= cancellation_deadline(row["invoice_date"])]
    return render_template("invoice.html", cfg=cfg, today=today.isoformat(), preview=preview_number, next_sequence=preview_sequence, last_date=last[0] if last else "", error=None, cancellable_count=len(available_cancellations))


@app.get("/invoice-number-preview")
def invoice_number_preview():
    cfg = settings()
    if not cfg["profile_id"]:
        abort(404)
    try:
        issued = date.fromisoformat(request.args.get("date", ""))
    except ValueError:
        return jsonify({"error": "Choose a valid bill date."}), 400
    with db() as con:
        number = next_bill_number(con, cfg["profile_id"], cfg["company"], issued)
    sequence = int(re.search(r"(\d+)$", number).group(1))
    return jsonify({"number": number, "sequence": sequence})


@app.post("/finalize")
def finalize():
    cfg = settings()
    save_stage = "reading bill details"
    try:
        customer = {k: request.form.get(k, "").strip() for k in ("name", "gstin", "address", "district", "pincode", "phone")}
        customer["gstin"] = customer["gstin"].upper()
        ship_to = {k: request.form.get("ship_" + k, "").strip() for k in ("name", "gstin", "address", "district", "pincode", "phone")}
        ship_to["gstin"] = ship_to["gstin"].upper()
        for party in (customer, ship_to):
            for field in ("name", "address", "district"):
                party[field] = party[field].title()
        tax_type = request.form.get("tax_type", "intra")
        if tax_type not in ("intra", "inter"):
            raise ValueError("Choose intra-state or inter-state tax treatment.")
        reverse_charge = request.form.get("reverse_charge", "No").strip()
        warranty = request.form.get("warranty", "").strip()
        if warranty not in ("1 Year", "6 Months", "3 Months", "-Nil-"):
            raise ValueError("Choose one of the listed warranty options.")
        terms = request.form.get("terms", "").strip() or cfg["company"].get("terms", "")
        invoice_date = request.form.get("invoice_date", "")
        number = request.form.get("invoice_number", "").strip()
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,39}", number):
            raise ValueError("Enter a bill number using letters, numbers, dots, underscores, or hyphens (up to 40 characters).")
        if not customer["name"] or not customer["address"]:
            raise ValueError("Customer name and address are required.")
        date.fromisoformat(invoice_date)
        save_stage = "calculating bill totals"
        items, totals = totals_for(json.loads(request.form.get("items", "[]")), tax_type, request.form.get("roundoff", 0))
        if request.form.get("same_shipping_address") == "on":
            ship_to.update({"name": customer["name"], "phone": customer["phone"], "address": customer["address"], "district": customer["district"], "pincode": customer["pincode"]})
        for pin in (customer["pincode"], ship_to["pincode"]):
            if pin and not re.fullmatch(r"\d{6}", pin):
                raise ValueError("Enter a six-digit pincode, or leave the field blank.")
        customer.update({"ship_to": ship_to, "tax_type": tax_type, "reverse_charge": reverse_charge, "warranty": warranty, "terms": terms})
        save_stage = "checking invoice date and number"
        with db() as con:
            last = con.execute("SELECT invoice_date FROM invoices WHERE profile_id=? ORDER BY id DESC LIMIT 1", (cfg["profile_id"],)).fetchone()
            if last and invoice_date < last[0]:
                raise ValueError(f"Bill date cannot be earlier than the last finalized bill ({last[0]}).")
            billing_period = billing_cycle_period(invoice_date, cfg["company"])
            duplicates = con.execute("SELECT number,invoice_date FROM invoices WHERE profile_id=?", (cfg["profile_id"],)).fetchall()
            if any(row["number"] == number and billing_cycle_period(row["invoice_date"], cfg["company"]) == billing_period for row in duplicates):
                raise ValueError("That bill number has already been used. Enter a different bill number.")
            output_dir = Path(cfg["output_dir"] or APP_HOME / "Bills")
            month_dir = output_dir / date.fromisoformat(invoice_date).strftime("%B %Y")
            month_dir.mkdir(parents=True, exist_ok=True)
            address_words = re.findall(r"[A-Za-z0-9]+", customer["address"])[:3]
            address_label = "_".join(address_words) or "Customer"
            safe_stem = re.sub(r"[^A-Za-z0-9_-]", "", f"{address_label}_{number}")
            pdf_path = month_dir / f"{safe_stem}.pdf"
            record = {"number": number, "date": invoice_date, "customer": customer, "ship_to": ship_to, "tax_type": tax_type, "reverse_charge": reverse_charge, "warranty": warranty, "terms": terms, "items": items, "totals": totals}
            save_stage = "rendering the PDF copy"
            make_pdf(pdf_path, record, cfg["company"], cfg["company"].get("logo"))
            save_stage = "saving the bill record"
            cur = con.execute("INSERT INTO invoices(number,invoice_date,customer,items,totals,company_snapshot,pdf_path,profile_id) VALUES(?,?,?,?,?,?,?,?)", (number, invoice_date, json.dumps(customer), json.dumps(items), json.dumps(totals), json.dumps(cfg["company"]), str(pdf_path), cfg["profile_id"]))
            remember_items(con, cfg["profile_id"], items)
        return redirect(url_for("bill_record", record_id=cur.lastrowid))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        with db() as con:
            preview_number = next_bill_number(con, cfg["profile_id"], cfg["company"], date.today())
            preview_sequence = int(re.search(r"(\d+)$", preview_number).group(1))
            last = con.execute("SELECT invoice_date FROM invoices WHERE profile_id=? ORDER BY id DESC LIMIT 1", (cfg["profile_id"],)).fetchone()
        return render_template("invoice.html", cfg=cfg, today=date.today().isoformat(), preview=preview_number, next_sequence=preview_sequence, last_date=last[0] if last else "", error=str(exc), cancellable_count=0), 400
    except Exception as exc:
        app.logger.exception("Bill finalization failed during %s for profile %s", save_stage, cfg.get("profile_id"))
        with db() as con:
            preview_number = next_bill_number(con, cfg["profile_id"], cfg["company"], date.today())
            preview_sequence = int(re.search(r"(\d+)$", preview_number).group(1))
            last = con.execute("SELECT invoice_date FROM invoices WHERE profile_id=? ORDER BY id DESC LIMIT 1", (cfg["profile_id"],)).fetchone()
        return render_template("invoice.html", cfg=cfg, today=date.today().isoformat(), preview=preview_number, next_sequence=preview_sequence, last_date=last[0] if last else "", error=f"The bill could not be saved while {save_stage}: {exc}", cancellable_count=0), 500


def render_bill_row(row):
    if not row or row["deleted"]:
        abort(404)
    customer = json.loads(row["customer"])
    customer["gstin"] = str(customer.get("gstin", "")).strip().upper()
    customer.setdefault("ship_to", {})["gstin"] = str(customer.get("ship_to", {}).get("gstin", "")).strip().upper()
    tax_type = customer.get("tax_type", "intra")
    totals = json.loads(row["totals"])
    totals.setdefault("roundoff", 0)
    totals.setdefault("cgst", round(totals.get("tax", 0) / 2, 2) if tax_type == "intra" else 0)
    totals.setdefault("sgst", round(totals.get("tax", 0) - totals["cgst"], 2) if tax_type == "intra" else 0)
    totals.setdefault("igst", totals.get("tax", 0) if tax_type == "inter" else 0)
    company = json.loads(row["company_snapshot"])
    company["gstin"] = str(company.get("gstin", "")).strip().upper()
    can_cancel = not row["cancelled"] and date.today() <= cancellation_deadline(row["invoice_date"])
    return render_template("bill.html", invoice={**dict(row), "customer": customer, "ship_to": customer.get("ship_to", {}), "place_of_supply": customer.get("place_of_supply", ""), "tax_type": tax_type, "reverse_charge": customer.get("reverse_charge", "No"), "warranty": customer.get("warranty", ""), "terms": customer.get("terms", ""), "items": json.loads(row["items"]), "totals": totals, "amount_words": amount_in_words(totals["total"]), "company": company, "can_cancel": can_cancel}, cfg=settings())


@app.get("/bill/record/<int:record_id>")
def bill_record(record_id):
    with db() as con:
        row = con.execute("SELECT * FROM invoices WHERE id=?", (record_id,)).fetchone()
    return render_bill_row(row)


@app.get("/bill/<invoice_id>")
def bill(invoice_id):
    # Keep old visible-number links working; the app's own links use the row ID.
    with db() as con:
        row = con.execute("SELECT * FROM invoices WHERE number=? AND deleted=0 ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
    return render_bill_row(row)


@app.get("/cancellations")
def cancellations():
    cfg = settings()
    with db() as con:
        rows = con.execute("SELECT id,number,invoice_date,customer,pdf_path FROM invoices WHERE profile_id=? AND cancelled=0 AND deleted=0 ORDER BY id DESC", (cfg["profile_id"],)).fetchall()
    bills = []
    for row in rows:
        deadline = cancellation_deadline(row["invoice_date"])
        bills.append({**dict(row), "customer": json.loads(row["customer"]), "cancel_deadline": deadline.isoformat(), "can_cancel": date.today() <= deadline})
    return render_template("cancellations.html", cfg=cfg, bills=bills)


@app.post("/bill/record/<int:record_id>/delete")
def delete_bill_record(record_id):
    cfg = settings()
    with db() as con:
        row = con.execute("SELECT id FROM invoices WHERE id=? AND profile_id=? AND deleted=0", (record_id, cfg["profile_id"])).fetchone()
        if not row:
            abort(404)
        # Hide the record in the app while retaining its saved PDF and number history.
        con.execute("UPDATE invoices SET deleted=1 WHERE id=?", (record_id,))
    return redirect(url_for("dashboard"))


def cancel_invoice_record(record_id):
    cfg = settings()
    with db() as con:
        row = con.execute("SELECT * FROM invoices WHERE id=? AND profile_id=? AND deleted=0", (record_id, cfg["profile_id"])).fetchone()
        if not row:
            abort(404)
        if row["cancelled"]:
            return redirect(url_for("bill_record", record_id=record_id))
        if date.today() > cancellation_deadline(row["invoice_date"]):
            abort(410, description="The cancellation period for this bill has ended.")
        customer = json.loads(row["customer"])
        totals = json.loads(row["totals"])
        company = json.loads(row["company_snapshot"])
        tax_type = customer.get("tax_type", "intra")
        totals.setdefault("roundoff", 0)
        totals.setdefault("cgst", round(totals.get("tax", 0) / 2, 2) if tax_type == "intra" else 0)
        totals.setdefault("sgst", round(totals.get("tax", 0) - totals["cgst"], 2) if tax_type == "intra" else 0)
        totals.setdefault("igst", totals.get("tax", 0) if tax_type == "inter" else 0)
        invoice_record = {"number": row["number"], "date": row["invoice_date"], "customer": customer, "ship_to": customer.get("ship_to", {}), "place_of_supply": customer.get("place_of_supply", ""), "tax_type": tax_type, "reverse_charge": customer.get("reverse_charge", "No"), "warranty": customer.get("warranty", ""), "terms": customer.get("terms", ""), "items": json.loads(row["items"]), "totals": totals}
        make_pdf(row["pdf_path"], invoice_record, company, company.get("logo"), cancelled=True)
        con.execute("UPDATE invoices SET cancelled=1 WHERE id=?", (row["id"],))
    return redirect(url_for("bill_record", record_id=record_id))


@app.post("/bill/record/<int:record_id>/cancel")
def cancel_bill_record(record_id):
    return cancel_invoice_record(record_id)


@app.post("/bill/<invoice_id>/cancel")
def cancel_bill(invoice_id):
    cfg = settings()
    with db() as con:
        row = con.execute("SELECT id FROM invoices WHERE number=? AND profile_id=? AND deleted=0 ORDER BY id DESC LIMIT 1", (invoice_id, cfg["profile_id"])).fetchone()
    if not row:
        abort(404)
    return cancel_invoice_record(row["id"])


@app.get("/bill/<invoice_id>/pdf")
def pdf(invoice_id):
    with db() as con:
        row = con.execute("SELECT pdf_path FROM invoices WHERE number=? AND deleted=0 ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
    if not row or not Path(row[0]).is_file():
        abort(404)
    return send_file(row[0], mimetype="application/pdf", as_attachment=False, download_name=f"{invoice_id}.pdf")


@app.get("/bill/record/<int:record_id>/pdf")
def pdf_record(record_id):
    with db() as con:
        row = con.execute("SELECT number,pdf_path FROM invoices WHERE id=? AND deleted=0", (record_id,)).fetchone()
    if not row or not Path(row["pdf_path"]).is_file():
        abort(404)
    return send_file(row["pdf_path"], mimetype="application/pdf", as_attachment=False, download_name=f"{row['number']}.pdf")


@app.get("/company-logo")
def company_logo():
    logo = settings()["company"].get("logo", "")
    if not logo or not Path(logo).is_file():
        abort(404)
    return send_file(logo)


@app.get("/profiles/<int:profile_id>/logo")
def profile_logo(profile_id):
    with db() as con:
        row = con.execute("SELECT company FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        abort(404)
    logo = json.loads(row[0]).get("logo", "")
    if not logo or not Path(logo).is_file():
        abort(404)
    return send_file(logo)


@app.get("/profiles/<int:profile_id>/signature")
def profile_signature(profile_id):
    with db() as con:
        row = con.execute("SELECT company FROM profiles WHERE id=?", (profile_id,)).fetchone()
    if not row:
        abort(404)
    signature = json.loads(row[0]).get("signature", "")
    if not signature or not Path(signature).is_file():
        abort(404)
    return send_file(signature)


@app.get("/bill/<invoice_id>/company-logo")
def company_logo_for(invoice_id):
    with db() as con:
        row = con.execute("SELECT company_snapshot FROM invoices WHERE number=? AND deleted=0 ORDER BY id DESC LIMIT 1", (invoice_id,)).fetchone()
    if not row:
        abort(404)
    logo = json.loads(row[0]).get("logo", "")
    if not logo or not Path(logo).is_file():
        abort(404)
    return send_file(logo)


@app.get("/bill/record/<int:record_id>/company-logo")
def company_logo_record(record_id):
    with db() as con:
        row = con.execute("SELECT company_snapshot FROM invoices WHERE id=? AND deleted=0", (record_id,)).fetchone()
    if not row:
        abort(404)
    logo = json.loads(row[0]).get("logo", "")
    if not logo or not Path(logo).is_file():
        abort(404)
    return send_file(logo)


@app.get("/favicon.ico")
def favicon():
    return send_file(Path(app.static_folder) / "favicon.svg", mimetype="image/svg+xml")


def main():
    port = int(os.environ.get("BILLING_SOFTWARE_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    import webview
    from threading import Thread

    server = Thread(target=lambda: serve(app, host="127.0.0.1", port=port, threads=8, ident="Ledgerline"), daemon=True)
    server.start()
    webview.create_window("Ledgerline · GST Billing", url, width=1360, height=900, min_size=(360, 600), resizable=True)
    webview.start(gui="edgechromium")

