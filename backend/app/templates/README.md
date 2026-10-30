# Billing by Service: optional master workbook

If you keep a multi-tab Excel workbook with one tab per AWS account (tab name `<account id>-<alias>`), put a copy here as `billing_by_service_template.xlsx`. The Billing by Service export then writes the new month's numbers into your workbook, keeping your own formulas and notes.

Without this file the export falls back to a plain workbook generated from scratch, so nothing breaks. Do not commit a real workbook to a public repository: it usually contains account IDs and spend figures.
