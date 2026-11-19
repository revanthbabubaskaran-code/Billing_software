# Ledgerline · GST Billing Desk

A local billing application for managing one company profile and creating GST invoices. Company details and finalized bill records are stored in a SQLite database on this computer. Each finalized bill is also rendered as a PDF in the folder selected in Company settings.

## Start the app

Install Python 3.14 and [uv](https://docs.astral.sh/uv/), then run from this folder:

```powershell
uv run billing-software
```

The app opens in your browser at `http://127.0.0.1:8765`. It listens only on this computer. On first launch, enter company details and select the folder for bill copies. Use **Company settings** to edit those details or change the destination later.

## Create a bill

Enter customer and optional ship-to details, place of supply, reverse-charge status, and invoice terms. Add line items with optional HSN/SAC codes and units, quantities, rates, and GST rates. Choose intra-state (CGST plus SGST) or inter-state (IGST) tax treatment. The app shows a tax-rate summary, editable round-off, grand total, and amount in words. The company profile can include phone and email, in addition to GSTIN, address, logo, bank details, and header/footer text.

The app assigns the next invoice number when you finalize. A bill date may be today, a future date, or the same date as the latest finalized bill; it cannot be earlier than the latest finalized bill. Finalization saves a PDF copy and opens a bill summary with print and PDF actions.

## Local data

By default, the database and uploaded logo are stored in `~/.billing-software` (on Windows, the user profile folder). Set the `BILLING_SOFTWARE_HOME` environment variable before starting the app to choose a different application-data directory. Set `BILLING_SOFTWARE_PORT` to use another local port.

Changing the PDF destination affects future bills. Existing PDF copies remain in their original folder. Back up both the application-data directory and the selected bill-copy folder.
