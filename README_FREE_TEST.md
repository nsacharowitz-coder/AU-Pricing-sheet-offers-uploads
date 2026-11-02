# AU Pricing Portal — Free test deployment

This is an **ephemeral demonstration**, not a secure production deployment. Do not upload real client contracts or confidential data. Render free instances may spin down, restart, and lose the SQLite database.

1. Create a private GitHub repository and upload these files to its root. Do not upload credentials or real client data.
2. Render Dashboard > New > Blueprint > connect the repository.
3. Check the Blueprint displays a **Free** web service, with **no disk**. If it displays a paid service, cancel and review `render.yaml`.
4. Supply `AU_APP_PASSWORD` as a secret environment variable when prompted. Never commit it.
5. Deploy; open the onrender.com site link that Render supplies.
6. Test with invented client data only. Data can disappear whenever the instance restarts or redeploys.

This build has one shared password, not production authentication or user roles. Render account policies, including any card verification request, are controlled by Render and may vary. If Render still requests a card, review the on-screen reason rather than entering one assuming it is free.
