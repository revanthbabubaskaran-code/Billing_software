# Ledgerline · GST Billing Desk

A local billing application for managing company profiles and creating GST invoices. Company details and finalized bill records are stored in a SQLite database on this computer. Each finalized bill is also rendered as a PDF in the folder selected in Company settings.

## Start the app

Install Python 3.14, [uv](https://docs.astral.sh/uv/), and the [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/microsoft-edge/webview2/), then double-click `start-billing-software.bat` or run from this folder:

```powershell
uv run billing-software
```

The app opens in its own desktop window and listens only on this computer. It uses the WebView2 runtime inside that window and does not open a separate browser. The launch screen lets you select a company profile or add another. Each profile has its own company details, optional logo, bill-copy folder, and invoice history. If a profile has no logo, its tile displays initials from the company name. Use **Company settings** to edit the selected profile. Existing single-company data is migrated to the first profile automatically.

The app uses Waitress to serve the local interface. GSTIN values are converted to uppercase when entered and saved. Use the **Appearance** button at the lower left to cycle between automatic OS appearance, dark, and light modes; the choice is remembered in the desktop app. The layout adapts for desktop, tablet, and narrow phone screens.

The editable HSN master is seeded once from the bundled four-digit starter catalog at `src/billing_software/data/hsn_starter.json`. The catalog includes source links, official heading-level descriptions, and practical common-use search notes. Common-use notes are examples only; verify the applicable code and tax treatment against the GST Portal and current CBIC material. Changes made in the HSN master are stored locally and are not overwritten by later application starts.

## Create a bill

Enter customer and optional ship-to details, place of supply, reverse-charge status, and invoice terms. Add line items with optional HSN/SAC codes and units, quantities, rates, and GST rates. Choose intra-state (CGST plus SGST) or inter-state (IGST) tax treatment. The app shows a tax-rate summary, editable round-off, grand total, and amount in words. The company profile can include phone and email, in addition to GSTIN, address, logo, bank details, and header/footer text.

Each profile sets its invoice number format and starting number. You can optionally enable an annual billing cycle and select its reset day and month. When enabled, numbering restarts at the beginning of each cycle; otherwise it continues from the latest bill. Finalization saves an A4 PDF styled after the sample invoice and opens a bill summary with print and PDF actions. **Bill management** stays in the sidebar and shows cancellation deadlines. A bill can be cancelled through the end of the month after its issue date. Removing a bill from the app hides its entry while retaining the saved PDF and reserving its bill number.

## Local data

By default, the database and uploaded logo are stored in `~/.billing-software` (on Windows, the user profile folder). Set the `BILLING_SOFTWARE_HOME` environment variable before starting the app to choose a different application-data directory. Set `BILLING_SOFTWARE_PORT` to use another local port.

Changing the PDF destination affects future bills. Existing PDF copies remain in their original folder. Back up both the application-data directory and the selected bill-copy folder.

Bills print on A4 portrait paper. The display-size buttons in the sidebar increase or decrease the on-screen interface scale and remember the selected size in the browser.

