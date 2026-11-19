from __future__ import annotations

import json
import os
import re
import sqlite3
import webbrowser
from datetime import date
from pathlib import Path
from threading import Timer
from xml.sax.saxutils import escape

from flask import Flask, abort, redirect, render_template, request, send_file, url_for
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import mm
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle, Image
from werkzeug.utils import secure_filename

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
        CREATE TABLE IF NOT EXISTS invoices (id INTEGER PRIMARY KEY AUTOINCREMENT, number TEXT UNIQUE NOT NULL, invoice_date TEXT NOT NULL, customer TEXT NOT NULL, items TEXT NOT NULL, totals TEXT NOT NULL, company_snapshot TEXT NOT NULL, pdf_path TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
        INSERT OR IGNORE INTO settings(id) VALUES(1);
        """)


init_db()


def settings():
    with db() as con:
        row = con.execute("SELECT * FROM settings WHERE id=1").fetchone()
    return {"company": json.loads(row["company"]), "output_dir": row["output_dir"]}


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


def make_pdf(path, invoice, company, logo_path=None):
    doc = SimpleDocTemplate(str(path), pagesize=A4, rightMargin=17*mm, leftMargin=17*mm, topMargin=15*mm, bottomMargin=15*mm, title=f"Tax invoice {invoice['number']}")
    styles = getSampleStyleSheet()
    styles.add(ParagraphStyle(name="Small", parent=styles["BodyText"], fontSize=8, leading=11, textColor=colors.HexColor("#526174")))
    styles.add(ParagraphStyle(name="Right", parent=styles["BodyText"], alignment=2))
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
    company_lines = [safe(company.get("gstin")), f"<b>{safe(company.get('name'))}</b>", safe(company.get("address")).replace("\n", "<br/>")]
    if company.get("phone") or company.get("email"):
        company_lines.append(" · ".join(safe(company.get(k)) for k in ("phone", "email") if company.get(k)))
    if company.get("bank"):
        bank = company["bank"]
        company_lines.append("<br/><b>Bank details</b>: " + " · ".join(safe(bank.get(k)) for k in ("name", "account", "ifsc", "branch") if bank.get(k)))
    customer = invoice["customer"]
    bill_to = ["<b>Bill to</b>", f"<b>{safe(customer['name'])}</b>", safe(customer.get("gstin")), safe(customer.get("address")).replace("\n", "<br/>")]
    if customer.get("phone"):
        bill_to.append("Contact: " + safe(customer["phone"]))
    ship_to = invoice.get("ship_to", {})
    if ship_to.get("name") or ship_to.get("address"):
        bill_to += ["<br/><b>Ship / work location</b>", f"<b>{safe(ship_to.get('name'))}</b>" if ship_to.get("name") else "", safe(ship_to.get("gstin")), safe(ship_to.get("address")).replace("\n", "<br/>"), ("Contact: " + safe(ship_to.get("phone"))) if ship_to.get("phone") else ""]
    parties = Table([[Paragraph("<br/>".join(x for x in company_lines if x), styles["BodyText"]), Paragraph("<br/>".join(x for x in bill_to if x), styles["BodyText"])]], colWidths=[82*mm, 88*mm])
    parties.setStyle(TableStyle([("BACKGROUND",(0,0),(0,0),colors.HexColor("#F2F5F9")),("BACKGROUND",(1,0),(1,0),colors.HexColor("#F2F5F9")),("BOX",(0,0),(-1,-1),.5,colors.HexColor("#DCE3EC")),("VALIGN",(0,0),(-1,-1),"TOP"),("LEFTPADDING",(0,0),(-1,-1),9),("RIGHTPADDING",(0,0),(-1,-1),9),("TOPPADDING",(0,0),(-1,-1),9),("BOTTOMPADDING",(0,0),(-1,-1),9)]))
    story += [parties, Spacer(1, 7*mm)]
    rows = [["Description", "HSN/SAC", "Qty", "Unit", "Rate (INR)", "Amount (INR)"]]
    for item in invoice["items"]:
        rows.append([item["description"], item.get("hsn", ""), f"{item['quantity']:g}", item.get("unit", ""), f"{item['rate']:,.2f}", f"{item['taxable']:,.2f}"])
    table = Table(rows, colWidths=[49*mm, 21*mm, 15*mm, 16*mm, 26*mm, 29*mm], repeatRows=1)
    table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#163A5F")),("TEXTCOLOR",(0,0),(-1,0),colors.white),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),8),("GRID",(0,0),(-1,-1),.4,colors.HexColor("#DCE3EC")),("ALIGN",(1,1),(-1,-1),"RIGHT"),("ALIGN",(0,0),(0,-1),"LEFT"),("VALIGN",(0,0),(-1,-1),"TOP"),("TOPPADDING",(0,0),(-1,-1),7),("BOTTOMPADDING",(0,0),(-1,-1),7)]))
    story += [table, Spacer(1, 3*mm)]
    totals = invoice["totals"]
    tax_rows = [["Tax rate", "Taxable value", "CGST", "SGST", "IGST"]]
    for rate in sorted({item["gst"] for item in invoice["items"]}):
        group = [item for item in invoice["items"] if item["gst"] == rate]
        taxable = sum(item["taxable"] for item in group)
        tax_rows.append([f"{rate:g}%", f"INR {taxable:,.2f}", f"INR {sum(item['cgst'] for item in group):,.2f}" if invoice["tax_type"] == "intra" else "-", f"INR {sum(item['sgst'] for item in group):,.2f}" if invoice["tax_type"] == "intra" else "-", f"INR {sum(item['igst'] for item in group):,.2f}" if invoice["tax_type"] == "inter" else "-"])
    tax_rows.append(["Total", f"INR {totals['taxable']:,.2f}", f"INR {totals['cgst']:,.2f}" if invoice["tax_type"] == "intra" else "-", f"INR {totals['sgst']:,.2f}" if invoice["tax_type"] == "intra" else "-", f"INR {totals['igst']:,.2f}" if invoice["tax_type"] == "inter" else "-"])
    tax_table = Table(tax_rows, colWidths=[22*mm, 42*mm, 36*mm, 36*mm, 36*mm])
    tax_table.setStyle(TableStyle([("BACKGROUND",(0,0),(-1,0),colors.HexColor("#F2F5F9")),("FONTNAME",(0,0),(-1,0),"Helvetica-Bold"),("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),("FONTSIZE",(0,0),(-1,-1),8),("LINEBELOW",(0,0),(-1,-1),.4,colors.HexColor("#DCE3EC")),("ALIGN",(1,1),(-1,-1),"RIGHT"),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6)]))
    story += [Paragraph(f"Place of supply: {safe(invoice.get('place_of_supply'))} · {'Intra-state (CGST + SGST)' if invoice['tax_type'] == 'intra' else 'Inter-state (IGST)'} · Reverse charge: {safe(invoice.get('reverse_charge') or 'No')}", styles["Small"]), Spacer(1, 2*mm), tax_table]
    summary_rows = [["Taxable value", f"INR {totals['taxable']:,.2f}"], ["GST total", f"INR {totals['tax']:,.2f}"], ["Round off", f"INR {totals['roundoff']:,.2f}"], ["Grand total", f"INR {totals['total']:,.2f}"]]
    summary = Table(summary_rows, colWidths=[38*mm, 38*mm], hAlign="RIGHT")
    summary.setStyle(TableStyle([("ALIGN",(0,0),(-1,-1),"RIGHT"),("FONTNAME",(0,0),(-1,-1),"Helvetica"),("FONTNAME",(0,-1),(-1,-1),"Helvetica-Bold"),("LINEABOVE",(0,-1),(-1,-1),.8,colors.HexColor("#163A5F")),("FONTSIZE",(0,0),(-1,-1),9),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)]))
    story += [Spacer(1, 2*mm), summary, Spacer(1, 3*mm), Paragraph(f"<b>{escape(amount_in_words(totals['total']))}</b>", styles["BodyText"]), Spacer(1, 8*mm), Paragraph("This is a computer-generated tax invoice.", styles["Small"])]
    if invoice.get("warranty"):
        story += [Spacer(1, 2*mm), Paragraph("Warranty: " + safe(invoice["warranty"]), styles["Small"])]
    if invoice.get("terms"):
        story += [Spacer(1, 2*mm), Paragraph("<b>Terms &amp; Conditions</b><br/>" + safe(invoice["terms"]).replace("\n", "<br/>"), styles["Small"])]
    if company.get("footer"):
        story += [Spacer(1, 4*mm), Paragraph(safe(company["footer"]), styles["Small"])]
    doc.build(story)


@app.get("/")
def home():
    cfg = settings()
    if not cfg["company"].get("name"):
        return redirect(url_for("setup"))
    with db() as con:
        invoices = con.execute("SELECT number, invoice_date, customer, totals FROM invoices ORDER BY id DESC LIMIT 8").fetchall()
        count = con.execute("SELECT count(*) FROM invoices").fetchone()[0]
        total = con.execute("SELECT COALESCE(sum(json_extract(totals,'$.total')),0) FROM invoices").fetchone()[0]
    return render_template("home.html", cfg=cfg, invoices=[{**dict(x), "customer": json.loads(x["customer"]), "totals": json.loads(x["totals"])} for x in invoices], count=count, total=total)


@app.route("/setup", methods=["GET", "POST"])
def setup():
    cfg = settings()
    if request.method == "POST":
        company = {k: request.form.get(k, "").strip() for k in ("name", "gstin", "address", "phone", "email", "header", "footer")}
        company["bank"] = {k: request.form.get("bank_" + k, "").strip() for k in ("name", "account", "ifsc", "branch")}
        if not company["name"] or not company["gstin"] or not company["address"]:
            return render_template("setup.html", cfg=cfg, error="Company name, GSTIN, and address are required.")
        logo = request.files.get("logo")
        if logo and logo.filename:
            ext = Path(secure_filename(logo.filename)).suffix.lower()
            if ext not in (".png", ".jpg", ".jpeg"):
                return render_template("setup.html", cfg=cfg, error="Logo must be a PNG or JPG image.")
            logo_path = UPLOADS / f"company-logo{ext}"
            logo.save(logo_path)
            company["logo"] = str(logo_path)
        else:
            company["logo"] = cfg["company"].get("logo", "")
        out = Path(request.form.get("output_dir", "").strip()).expanduser()
        try:
            out.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return render_template("setup.html", cfg=cfg, error=f"Could not use that bill folder: {exc}")
        with db() as con:
            con.execute("UPDATE settings SET company=?, output_dir=? WHERE id=1", (json.dumps(company), str(out.resolve())))
        return redirect(url_for("home"))
    return render_template("setup.html", cfg=cfg, error=None)


@app.get("/new")
def new_invoice():
    cfg = settings()
    if not cfg["company"].get("name"):
        return redirect(url_for("setup"))
    with db() as con:
        last = con.execute("SELECT invoice_date FROM invoices ORDER BY id DESC LIMIT 1").fetchone()
        next_num = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM invoices").fetchone()[0]
    return render_template("invoice.html", cfg=cfg, today=date.today().isoformat(), last_date=last[0] if last else "", preview=f"INV-{date.today().year}-{next_num:04d}", error=None)


@app.post("/finalize")
def finalize():
    cfg = settings()
    try:
        customer = {k: request.form.get(k, "").strip() for k in ("name", "gstin", "address", "phone")}
        ship_to = {k: request.form.get("ship_" + k, "").strip() for k in ("name", "gstin", "address", "phone")}
        place_of_supply = request.form.get("place_of_supply", "").strip()
        tax_type = request.form.get("tax_type", "intra")
        if tax_type not in ("intra", "inter"):
            raise ValueError("Choose intra-state or inter-state tax treatment.")
        reverse_charge = request.form.get("reverse_charge", "No").strip()
        warranty = request.form.get("warranty", "").strip()
        terms = request.form.get("terms", "").strip()
        invoice_date = request.form.get("invoice_date", "")
        if not customer["name"] or not customer["address"]:
            raise ValueError("Customer name and address are required.")
        date.fromisoformat(invoice_date)
        items, totals = totals_for(json.loads(request.form.get("items", "[]")), tax_type, request.form.get("roundoff", 0))
        customer.update({"ship_to": ship_to, "place_of_supply": place_of_supply, "tax_type": tax_type, "reverse_charge": reverse_charge, "warranty": warranty, "terms": terms})
        with db() as con:
            last = con.execute("SELECT invoice_date FROM invoices ORDER BY id DESC LIMIT 1").fetchone()
            if last and invoice_date < last[0]:
                raise ValueError(f"Bill date cannot be earlier than the last finalized bill ({last[0]}).")
            number = f"INV-{date.fromisoformat(invoice_date).year}-{con.execute('SELECT COALESCE(MAX(id),0)+1 FROM invoices').fetchone()[0]:04d}"
            output_dir = Path(cfg["output_dir"] or APP_HOME / "Bills")
            output_dir.mkdir(parents=True, exist_ok=True)
            safe_number = re.sub(r"[^A-Za-z0-9_-]", "", number)
            pdf_path = output_dir / f"{safe_number}.pdf"
            record = {"number": number, "date": invoice_date, "customer": customer, "ship_to": ship_to, "place_of_supply": place_of_supply, "tax_type": tax_type, "reverse_charge": reverse_charge, "warranty": warranty, "terms": terms, "items": items, "totals": totals}
            make_pdf(pdf_path, record, cfg["company"], cfg["company"].get("logo"))
            con.execute("INSERT INTO invoices(number,invoice_date,customer,items,totals,company_snapshot,pdf_path) VALUES(?,?,?,?,?,?,?)", (number, invoice_date, json.dumps(customer), json.dumps(items), json.dumps(totals), json.dumps(cfg["company"]), str(pdf_path)))
        return redirect(url_for("bill", invoice_id=number))
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        with db() as con:
            last = con.execute("SELECT invoice_date FROM invoices ORDER BY id DESC LIMIT 1").fetchone()
            next_num = con.execute("SELECT COALESCE(MAX(id),0)+1 FROM invoices").fetchone()[0]
        return render_template("invoice.html", cfg=cfg, today=date.today().isoformat(), last_date=last[0] if last else "", preview=f"INV-{date.today().year}-{next_num:04d}", error=str(exc)), 400


@app.get("/bill/<invoice_id>")
def bill(invoice_id):
    with db() as con:
        row = con.execute("SELECT * FROM invoices WHERE number=?", (invoice_id,)).fetchone()
    if not row:
        abort(404)
    customer = json.loads(row["customer"])
    tax_type = customer.get("tax_type", "intra")
    totals = json.loads(row["totals"])
    totals.setdefault("roundoff", 0)
    totals.setdefault("cgst", round(totals.get("tax", 0) / 2, 2) if tax_type == "intra" else 0)
    totals.setdefault("sgst", round(totals.get("tax", 0) - totals["cgst"], 2) if tax_type == "intra" else 0)
    totals.setdefault("igst", totals.get("tax", 0) if tax_type == "inter" else 0)
    return render_template("bill.html", invoice={**dict(row), "customer": customer, "ship_to": customer.get("ship_to", {}), "place_of_supply": customer.get("place_of_supply", ""), "tax_type": tax_type, "reverse_charge": customer.get("reverse_charge", "No"), "warranty": customer.get("warranty", ""), "terms": customer.get("terms", ""), "items": json.loads(row["items"]), "totals": totals, "amount_words": amount_in_words(totals["total"]), "company": json.loads(row["company_snapshot"])}, cfg=settings())


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


def main():
    port = int(os.environ.get("BILLING_SOFTWARE_PORT", "8765"))
    url = f"http://127.0.0.1:{port}"
    Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False)
