# AU Pricing Portal — Website Deployment

This version adds a password gate and persistent database configuration to the four-tab AU Offer Manager. **It is not yet live.** You must deploy it using your own hosting account.

## Recommended path: Render (paid instance + persistent disk)

1. Create a **private** GitHub repository (e.g. `au-pricing-portal`) and upload the files from this folder to the root of that repository. Do not upload any client information, old `.db` files, supplier quotations or passwords.
2. Sign in to Render and choose **New > Blueprint**. Connect the private GitHub repository containing `render.yaml`.
3. Review the service and persistent disk plan (this configuration uses a paid `starter` instance and a 1 GB disk; check Render's current pricing before creating it).
4. When prompted for the environment variable `AU_APP_PASSWORD`, set a strong unique password. Keep it private; do not put the password in GitHub or chat.
5. Deploy. Render will show a live `https://...onrender.com` URL when ready. Open the URL and sign in using your password.
6. Test a dummy client, sign out, sign back in, and restart the service to ensure the data remains saved.

**Security:** This version has one shared password, not individual employee accounts, audit trails or role-based permissions. Restrict access to trusted staff, use HTTPS, and ask AU IT to approve deployment and data handling before uploading real client pricing. For production use with multiple staff members, use SSO, a managed database, user permissions and automated backups. A persistent disk is **not** a backup. Store off-site backups of `/var/data/au_offers.db`.

**Deployment caveats:** A disk-backed SQLite application should run as a single instance. Do not scale to multiple replicas. Some provider settings/plan names may change. Avoid free ephemeral hosts: client data may disappear when instances restart.

## Local test

Set `AU_APP_PASSWORD` in your local environment first. Optional: `AU_DB_PATH` sets where the SQLite file is saved.

```bash
python -m pip install -r requirements.txt
python -m streamlit run app.py
```

This retains client creation/import, current-rate upload/paste, supplier-offer upload/paste/manual review, per-client electricity/gas uplift controls, supplier terms, term-wise rankings and four-sheet Excel export (Current Rates, Electricity Offers, Gas Offers, Supplier Terms).

The quote extractor is **heuristic**, not a fully automated AI reader. Always verify offer rates, dates, meter references and contract terms before saving.
