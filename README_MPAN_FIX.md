# AU Pricing Portal — MPAN import correction

This version fixes recognition of MPAN / MPRN column labels and supports report title rows and a one-column list of meter numbers.

## Update your Render website
Replace files in your GitHub repository with the contents of this ZIP (especially `app.py`), commit and push. In Render select Manual Deploy > Deploy latest commit, if auto-deploy does not run. The website URL and password do not change.

## Test first
In Current Rates choose your client, then paste a table with columns `MPAN Bottom Line`, `Site Name`, `Day Usage (kWh)`, `Night Usage (kWh)`, `KVA`. Verify the recognition preview, then click Save supply records.

Free Render hosting does not preserve data across restarts; use dummy data only.
