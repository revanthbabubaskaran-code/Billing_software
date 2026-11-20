from __future__ import annotations

import json
import os
import re
import sqlite3
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
        CREATE TABLE IF NOT EXISTS invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE NOT NULL, invoice_date TEXT NOT NULL, customer TEXT NOT NULL, items TEXT NOT NULL, totals TEXT NOT NULL, company_snapshot TEXT NOT NULL, pdf_path TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, cancelled INTEGER NOT NULL DEFAULT 0);
        CREATE TABLE IF NOT EXISTS hsn_master (code TEXT PRIMARY KEY, description TEXT NOT NULL, usage TEXT NOT NULL DEFAULT '');
        INSERT OR IGNORE INTO settings(id) VALUES(1);
        """)
        hsn_seed = [
            ("8423", "Weighing machinery, including scales, weighbridges and weighing parts", "Trade, platform, table and industrial weighing equipment"),
            ("9031", "Other measuring or checking instruments and appliances", "Measurement or checking instruments not classified under a more specific heading"),
            ("8504", "Electrical transformers and static converters", "Power adapters, transformers and conversion equipment; confirm the product's specific classification"),
            ("8507", "Electric accumulators", "Rechargeable batteries and battery packs"),
            ("8536", "Electrical apparatus for switching or protecting circuits and making connections", "Switches, relays, fuses, plugs, sockets and connectors"),
            ("8537", "Boards, panels and bases equipped for electric control or distribution", "Electrical control and distribution panels"),
            ("8544", "Insulated wire, cable and other insulated electrical conductors", "Electrical cables and insulated wiring"),
            ("7318", "Screws, bolts, nuts, washers and similar iron or steel fasteners", "Fasteners and mounting hardware"),
            ("7326", "Other articles of iron or steel", "Fabricated iron or steel articles not covered by a more specific heading"),
            ("3926", "Other articles of plastics", "Plastic articles not covered by a more specific heading"),
            ("4016", "Other articles of vulcanised rubber", "Rubber articles such as pads or feet, subject to product details"),
            ("8483", "Transmission shafts, gears, bearings and related transmission elements", "Mechanical power-transmission components"),
        ]
        con.executemany("INSERT OR IGNORE INTO hsn_master(code,description,usage) VALUES(?,?,?)", hsn_seed)
        setting_columns = {row[1] for row in con.execute("PRAGMA table_info(settings)")}
        if "active_profile_id" not in setting_columns:
            con.execute("ALTER TABLE settings ADD COLUMN active_profile_id INTEGER")
        invoice_columns = {row[1] for row in con.execute("PRAGMA table_info(invoices)")}
        if "profile_id" not in invoice_columns:
            con.execute("ALTER TABLE invoices ADD COLUMN profile_id INTEGER")
        if "cancelled" not in invoice_columns:
            con.execute("ALTER TABLE invoices ADD COLUMN cancelled INTEGER NOT NULL DEFAULT 0")
        legacy = con.execute("SELECT company, output_dir, active_profile_id FROM settings WHERE id=1").fetchone()
        profile_count = con.execute("SELECT count(*) FROM profiles").fetchone()[0]
        if not profile_count and json.loads(legacy["company"]).get("name"):
            cur = con.execute("INSERT INTO profiles(company,output_dir) VALUES(?,?)", (legacy["company"], legacy["output_dir"]))
            con.execute("UPDATE settings SET active_profile_id=? WHERE id=1", (cur.lastrowid,))
            con.execute("UPDATE invoices SET profile_id=? WHERE profile_id IS NULL", (cur.lastrowid,))


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


def totals_for(items, tax_type="intra", roundoff=0):
    base = tax = cgst = sgst = igst = 0.0
    cleaned = []
    for item in items:
        name = str(item.get("description", "")).strip()
        qty, rate, gst = float(item.get("quantity", 0)), money(item.get("rate")), float(item.get("gst", 0))
        hsn, unit = str(item.get("hsn", "")).strip(), str(item.get("unit", "")).strip()
        if not name or qty <= 0 or rate < 0 or gst < 0 or gst > 100:
            raise ValueError("Enter a description, a quantity above zero, and valid rate and GST values for every item.")
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
        words = []
        if number >= 100:
            words.extend((ones[number // 100], "Hundred"))
            number %= 100
        if number >= 20:
            words.append(tens[number // 10])
            number %= 10
        if number:
            words.append(ones[number])
        return " ".join(words)
    amount = int(round(float(value) * 100))
    rupees, paise = divmod(amount, 100)
    parts = []
    for size, label in ((10000000, "Crore"), (100000, "Lakh"), (1000, "Thousand"), (1, "")):
        count, rupees = divmod(rupees, size)
        if count:
            parts.extend((under_thousand(count), label) if label else (under_thousand(count),))
    words = " ".join(parts) or "Zero"
    if paise:
        return f"Rupees {words} and {under_thousand(paise)} Paise only"
    return f"Rupees {words} only"


def cancellation_deadline(invoice_date):
    issued = date.fromisoformat(invoice_date)
    next_year, next_month = (issued.year + (issued.month == 12), 1 if issued.month == 12 else issued.month + 1)
    return date(next_year, next_month, 1) - timedelta(days=1)


def next_bill_number(con, profile_id, company, bill_year):
    sequence = int(company.get("starting_bill_number", 1)) + con.execute("SELECT count(*) FROM invoices WHERE profile_id=?", (profile_id,)).fetchone()[0]
    while con.execute("SELECT 1 FROM invoices WHERE number=?", (f"INV-{bill_year}-{sequence:04d}",)).fetchone():
        sequence += 1
    return f"INV-{bill_year}-{sequence:04d}"


def make_pdf(path, invoice, company, logo_path=None, cancelled=False):
    def draw_watermark(canvas, doc):
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
        canvas.setFillColor(colors.HexColor("#C62828"))
        canvas.setFillAlpha(.30)
        canvas.setFont("Helvetica-Bold", 50)
        canvas.drawCentredString(0, 0, "CANCELLED")
        canvas.restoreState()

    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=17*mm, leftMargin=17*mm, topMargin=15*mm, bottomMargin=15*mm, title=f"Tax invoice {invoice['number']}")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=9, leading=12, textColor=colors.HexColor("#526174")))
    styles.add(ParagraphStyle(name="Right", parent=styles["BodyText"], fontSize=10, alignment=2))
    story = []
    safe = lambda value: escape(str(value or ""))
    if company.get("header"):
        story += [Paragraph(safe(company["header"]), styles["Small"]), Spacer(1, 3*mm)]
    heading = [Paragraph("TAX INVOICE", styles["Title"]), Paragraph(f"<b>{invoice['number']}</b><br/>Date: {invoice['date']}", styles["Right"])]
    if logo_path and Path(logo_path).is_file():
        try:
            logo = Image(logo_path, width=28*mm, height=18*mm, kind="proportional")
            heading.insert(0, logo)
        except Exception:
            pass
    head = Table([heading], colWidths=[35*mm, 75*mm, 55*mm] if len(heading) == 3 else [95*mm, 70*mm])
    head.setStyle(TableStyle([("VALIGN", (0,0), (-1,-1), "MIDDLE"), ("ALIGN", (-1,0), (-1,-1), "RIGHT"), ("BOTTOMPADDING",(0,0),(-1,-1),8)]))
    story.append(head)
    company_name = str(company.get("name") or "Company")
    content_width = A4[0] - 34*mm
    try:
        identity_width = min(content_width, max(55*mm, stringWidth(company_name, "Helvetica-Bold", 18) + 6*mm))
    except Exception:
        identity_width = content_width
    styles.add(ParagraphStyle(name="CompanyTitle", parent=styles["Title"], fontName="Helvetica-Bold", fontSize=18, leading=22, alignment=1, textColor=colors.HexColor("#172536"), spaceAfter=2*mm))
    styles.add(ParagraphStyle(name="CompanyDetails", parent=styles["Small"], fontSize=9, leading=12, alignment=1))
    styles.add(ParagraphStyle(name="CompanyContact", parent=styles["Small"], fontSize=8, leading=10, alignment=1))
    identity_lines = [safe(company.get("address")).replace("\n", "<br/>")]
    identity = Table([[Paragraph(safe(company_name), styles["CompanyTitle"])], [Paragraph("<br/>".join(identity_lines), styles["CompanyDetails"])]], colWidths=[identity_width], hAlign="CENTER")
    identity.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"CENTER"),("LEFTPADDING",(0,0),(-1,-1),0),("RIGHTPADDING",(0,0),(-1,-1),0),("TOPPADDING",(0,0),(-1,-1),0),("BOTTOMPADDING",(0,0),(-1,-1),0)]))
    contact_line = " &nbsp; | &nbsp; ".join((
        "<b>Cell No.</b> " + safe(company.get("phone")),
        "<b>Email Id:</b> " + safe(company.get("email")),
        "<b>GSTIN:</b> " + safe(company.get("gstin")),
    ))
    contact = Table([[Paragraph(contact_line, styles["CompanyContact"])]], colWidths=[content_width])
    contact.setStyle(TableStyle([("BOX",(0,0),(-1,-1),.6,colors.HexColor("#AAB7C4")),("VALIGN",(0,0),(-1,-1),"MIDDLE"),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
    story += [identity, Spacer(1, 2*mm), contact, Spacer(1, 5*mm)]
    company_lines = []
    if company.get("bank"):
        bank = company["bank"]
        company_lines.append("<br/><b>Bank details</b>: " + " · ".join(safe(bank.get(k)) for k in ("name", "account", "ifsc", "branch") if bank.get(k)))
    customer = invoice["customer"]
    bill_to = ["<b>Bill to</b>", f"<b>{safe(customer['name'])}</b>", safe(customer.get("gstin") or "-Nil-"), safe(customer.get("address")).replace("\n", "<br/>")]
    if customer.get("phone"):
        bill_to.append("Contact: " + safe(customer["phone"]))
    ship_to = invoice.get("ship_to", {})
    if ship_to.get("name") or ship_to.get("address"):
        bill_to += ["<br/><b>Ship / work location</b>", f"<b>{safe(ship_to.get('name'))}</b>" if ship_to.get("name") else "", safe(ship_to.get("gstin") or "-Nil-"), safe(ship_to.get("address")).replace("\n", "<br/>"), ("Contact: " + safe(ship_to.get("phone"))) if ship_to.get("phone") else ""]
    if company_lines:
        parties = Table([[Paragraph("<br/>".join(company_lines), styles["BodyText"]), Paragraph("<br/>".join(x for x in bill_to if x), styles["BodyText"])]], colWidths=[82*mm, 88*mm])
    else:
        parties = Table([[Paragraph("<br/>".join(x for x in bill_to if x), styles["BodyText"])]], colWidths=[170*mm])
    parties.setStyle(TableStyle([("BACKGROUND",(0,0),(0,0),colors.HexColor("#F2F5F9")),("BACKGROUND",(1,0),(1,0),colors.HexColor("#F2F5F9")),("BOX",(0,0),(-1,-1),.5,colors.HexColor("#DCE3EC")),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),9),("RIGHTPADDING",(0,0),(-1,-1),9),("TOPPADDING",(0,0),(-1,-1),9),("BOTTOMPADDING",(0,0),(-1,-1),9)]))
    story += [parties, Spacer(1, 7*mm)]
    rows = [["Description", "HSN/SAC", "Qty", "Unit", "Rate (INR)", "Amount (INR)"]]
    for item in invoice["items"]:
        rows.append([item["description"], item.get("hsn", ""), f"{item['quantity']:g}", item.get("unit", ""), f"{item['rate']:,.2f}", f"{item['taxable']:,.2f}"])
    table = Table(rows, colWidths=[49*mm, 21*mm, 15*mm, 16*mm, 26*mm, 29*mm], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#163A5F")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,0),9),("FONTSIZE",(0,1),(-1,-1),10),("GRID",(0,0),(-1,-1),.4,colors.HexColor("#DCE3EC")),("ALIGN",(1,1),(-1,-1),"RIGHT"),("ALIGN",(0,0),(0,-1),"LEFT"),("VALIGN",(0,0),(-1,-1),"TOP"),("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7)]))
    story += [table, Spacer(1, 3*mm)]
    totals = invoice["totals"]
    tax_rows = [["Tax rate", "Taxable value", "CGST", "SGST", "IGST"]]
    for rate in sorted({item["gst"] for item in invoice["items"]}):
        group = [item for item in invoice["items"] if item["gst"] == rate]
        taxable = sum(item["taxable"] for item in group)
        tax_rows.append([f"{rate:g}%", f"INR {taxable:,.2f}", f"INR {sum(item['cgst'] for item in group):,.2f}" if invoice["tax_type"] == "intra" else "-", f"INR {sum(item['sgst'] for item in group):,.2f}" if invoice["tax_type"] == "intra" else "-", f"INR {sum(item['igst'] for item in group):,.2f}" if invoice["tax_type"] == "inter" else "-"])
    tax_rows.append(["Total", f"INR {totals['taxable']:,.2f}", f"INR {totals['cgst']:,.2f}" if invoice["tax_type"] == "intra" else "-", f"INR {totals['sgst']:,.2f}" if invoice["tax_type"] == "intra" else "-", f"INR {totals['igst']:,.2f}" if invoice["tax_type"] == "inter" else "-"])
    tax_table = Table(tax_rows, colWidths=[22*mm, 42*mm, 36*mm, 36*mm, 36*mm])
    tax_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#F2F5F9")),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,0),9),("FONTSIZE",(0,1),(-1,-1),10),("LINEBELOW",(0,0),(-1,-1),.4,colors.HexColor("#DCE3EC")),("ALIGN",(1,1),(-1,-1),"RIGHT"),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story += [Paragraph(f"Place of supply: {safe(invoice.get('place_of_supply'))} · {'Intra-state (CGST + SGST)' if invoice['tax_type'] == 'intra' else 'Inter-state (IGST)'} · Reverse charge: {safe(invoice.get('reverse_charge') or 'No')}", styles["Small"]), Spacer(1, 2*mm), tax_table]
    summary_rows = [["Taxable value", f"INR {totals['taxable']:,.2f}"], ["GST total", f"INR {totals['tax']:,.2f}"], ["Round off", f"INR {totals['roundoff']:,.2f}"], ["Grand total", f"INR {totals['total']:,.2f}"]]
    summary = Table(summary_rows, colWidths=[38*mm, 38*mm], hAlign="RIGHT")
    summary.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"RIGHT"),("FONTNAME",(0,0),(-1,-1),"Helvetica"),("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),("LINEABOVE",(0,-1),(-1,-1),.8,colors.HexColor("#163A5F")),("FONTSIZE",(0,0),(-1,-2),12),("FONTSIZE",(0,-1),(-1,-1),14),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story += [Spacer(1, 2*mm), summary, Spacer(1, 3*mm), Paragraph(f"<b>{escape(amount_in_words(totals['total']))}</b>", styles["BodyText"]), Spacer(1, 8*mm), Paragraph("This is a computer-generated tax invoice.", styles["Small"])]
    if invoice.get("warranty"):
        story += [Spacer(1, 2*mm), Paragraph("Warranty: " + safe(invoice["warranty"]), styles["Small"])]
    if invoice.get("terms"):
        story += [Spacer(1, 2*mm), Paragraph("<b>Terms &amp; Conditions</b><br/>" + safe(invoice["terms"]).replace("\n", "<br/>"), styles["Small"])]
    if company.get("footer"):
        story += [Spacer(1, 4*mm), Paragraph(safe(company["footer"]), styles["Small"])]
    doc.pageTemplates[0].onPage = draw_watermark
    doc.pageTemplates[0].onPageEnd = draw_cancelled
    doc.build(story)


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
        invoices = con.execute("SELECT number, invoice_date, customer, totals FROM invoices WHERE profile_id=? AND cancelled=0 ORDER BY id DESC LIMIT 8", (cfg["profile_id"],)).fetchall()
        count = con.execute("SELECT count(*) FROM invoices WHERE profile_id=? AND cancelled=0", (cfg["profile_id"],)).fetchone()[0]
        total = con.execute("SELECT COALESCE(sum(json_extract(totals,'$.total')),0) FROM invoices WHERE profile_id=? AND cancelled=0", (cfg["profile_id"],)).fetchone()[0]
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


@app.get("/hsn-suggest")
def hsn_suggest():
    query = request.args.get("q", "").strip().lower()
    if len(query) < 3:
        return jsonify([])
    pattern = f"%{query}%"
    with db() as con:
        rows = con.execute("SELECT code,description,usage FROM hsn_master WHERE code LIKE ? OR lower(description) LIKE ? OR lower(usage) LIKE ? ORDER BY CASE WHEN code=? THEN 0 WHEN code LIKE ? THEN 1 ELSE 2 END, code LIMIT 12", (pattern, pattern, pattern, query, f"{query}%")).fetchall()
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
        preview_number = next_bill_number(con, cfg["profile_id"], cfg["company"], date.today().year)
        last = con.execute("SELECT invoice_date FROM invoices WHERE profile_id=? ORDER BY id DESC LIMIT 1", (cfg["profile_id"],)).fetchone()
        cancellations = con.execute("SELECT number,invoice_date,customer FROM invoices WHERE profile_id=? AND cancelled=0 ORDER BY id DESC", (cfg["profile_id"],)).fetchall()
    available_cancellations = [dict(row) | {"customer": json.loads(row["customer"])} for row in cancellations if date.today() <= cancellation_deadline(row["invoice_date"])]
    return render_template("invoice.html", cfg=cfg, today=date.today().isoformat(), preview=preview_number, last_date=last[0] if last else "", error=None, cancellable_count=len(available_cancellations))


@app.post("/finalize")
def finalize():
    cfg = settings()
    try:
        customer = {k: request.form.get(k, "").strip() for k in ("name", "gstin", "address", "phone")}
        customer["gstin"] = customer["gstin"].upper()
        ship_to = {k: request.form.get("ship_" + k, "").strip() for k in ("name", "gstin", "address", "phone")}
        ship_to["gstin"] = ship_to["gstin"].upper()
        place_of_supply = request.form.get("place_of_supply", "").strip()
        tax_type = request.form.get("tax_type", "intra")
        if tax_type not in ("intra", "inter"):
            raise ValueError("Choose intra-state or inter-state tax treatment.")
        reverse_charge = request.form.get("reverse_charge", "No").strip()
        warranty = request.form.get("warranty", "").strip()
        terms = request.form.get("terms", "").strip() or cfg["company"].get("terms", "")
        invoice_date = request.form.get("invoice_date", "")
        if not customer["name"] or not customer["address"]:
            raise ValueError("Customer name and address are required.")
        date.fromisoformat(invoice_date)
        items, totals = totals_for(json.loads(request.form.get("items", "[]")), tax_type, request.form.get("roundoff", 0))
        customer.update({"ship_to": ship_to, "place_of_supply": place_of_supply, "tax_type": tax_type, "reverse_charge": reverse_charge, "warranty": warranty, "terms": terms})
        with db() as con:
            last = con.execute("SELECT invoice_date FROM invoices WHERE profile_id=? ORDER BY id DESC LIMIT 1", (cfg["profile_id"],)).fetchone()
            if last and invoice_date < last[0]:
                raise ValueError(f"Bill date cannot be earlier than the last finalized bill ({last[0]}).")
            number = next_bill_number(con, cfg["profile_id"], cfg["company"], date.fromisoformat(invoice_date).year)
            output_dir = Path(cfg["output_dir"] or APP_HOME / "Bills")
            month_dir = output_dir / date.fromisoformat(invoice_date).strftime("%B %Y")
            month_dir.mkdir(parents=True, exist_ok=True)
            address_words = re.findall(r"[A-Za-z0-9]+", customer["address"])[:3]
            address_label = "_".join(address_words) or "Customer"
            safe_stem = re.sub(r"[^A-Za-z0-9_-]", "", f"{address_label}_{number}")
            pdf_path = month_dir / f"{safe_stem}.pdf"
            record = {"number": number, "date": invoice_date, "customer": customer, "ship_to": ship_to, "place_of_supply": place_of_supply, "tax_type": tax_type, "reverse_charge": reverse_charge, "warranty": warranty, "terms": terms, "items": items, "totals": totals}
            make_pdf(pdf_path, record, cfg["company"], cfg["company"].get("logo"))
            con.execute("INSERT INTO invoices(number,invoice_date,customer,items,totals,company_snapshot,pdf_path,profile_id) VALUES(?,?,?,?,?,?,?,?)", (number, invoice_date, json.dumps(customer), json.dumps(items), json.dumps(totals), json.dumps(cfg["company"]), str(pdf_path), cfg["profile_id"]))
        return redirect(url_for("bill", invoice_id=number))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        with db() as con:
            preview_number = next_bill_number(con, cfg["profile_id"], cfg["company"], date.today().year)
            last = con.execute("SELECT invoice_date FROM invoices WHERE profile_id=? ORDER BY id DESC LIMIT 1", (cfg["profile_id"],)).fetchone()
        return render_template("invoice.html", cfg=cfg, today=date.today().isoformat(), preview=preview_number, last_date=last[0] if last else "", error=str(exc), cancellable_count=0), 400


@app.get("/bill/<invoice_id>")
def bill(invoice_id):
    with db() as con:
        row = con.execute("SELECT * FROM invoices WHERE number=?", (invoice_id,)).fetchone()
    if not row:
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


@app.get("/cancellations")
def cancellations():
    cfg = settings()
    with db() as con:
        rows = con.execute("SELECT number,invoice_date,customer,pdf_path FROM invoices WHERE profile_id=? AND cancelled=0 ORDER BY id DESC", (cfg["profile_id"],)).fetchall()
    bills = []
    for row in rows:
        deadline = cancellation_deadline(row["invoice_date"])
        if date.today() <= deadline:
            bills.append({**dict(row), "customer": json.loads(row["customer"]), "cancel_deadline": deadline.isoformat()})
    return render_template("cancellations.html", cfg=cfg, bills=bills)


@app.post("/bill/<invoice_id>/cancel")
def cancel_bill(invoice_id):
    cfg = settings()
    with db() as con:
        row = con.execute("SELECT * FROM invoices WHERE number=? AND profile_id=?", (invoice_id, cfg["profile_id"])).fetchone()
        if not row:
            abort(404)
        if row["cancelled"]:
            return redirect(url_for("bill", invoice_id=invoice_id))
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
    return redirect(url_for("bill", invoice_id=invoice_id))


@app.get("/bill/<invoice_id>/pdf")
def pdf(invoice_id):
    with db() as con:
        row = con.execute("SELECT pdf_path FROM invoices WHERE number=?", (invoice_id,)).fetchone()
    if not row or not Path(row[0]).is_file():
        abort(404)
    return send_file(row[0], mimetype="application/pdf", as_attachment=False, download_name=f"{invoice_id}.pdf")


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


@app.get("/bill/<invoice_id>/company-logo")
def company_logo_for(invoice_id):
    with db() as con:
        row = con.execute("SELECT company_snapshot FROM invoices WHERE number=?", (invoice_id,)).fetchone()
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
