# AU Client Offer Manager

A local-first Streamlit application for managing multiple electricity/gas tender clients, their current rates, supplier offers, uplift settings, declines and Excel comparison exports.

## Run on Windows
1. Install Python 3.11 or newer.
2. Open a terminal in this folder.
3. Run `python -m pip install -r requirements.txt`.
4. Run `python -m streamlit run app.py`.
5. Streamlit opens the app in your browser.

Data is stored in `au_offers.db` in the same folder. **Back up this file regularly.** You can move it to another computer with the app. Files are parsed when uploaded; original attachments are not retained. No external AI API is used, and nothing is sent to a supplier.

## Workflow
- **Clients**: create and switch clients, set a different electricity / gas uplift for each client (p/kWh). Interhaze Limited starts at 0p.
- **Quick uplift editor (sidebar)**: select any client, enter new electricity and gas uplift values in p/kWh and click **Save uplift**. This can be changed repeatedly, including back to 0p. Saved offers retain their original quoted rates; costs, ranks and Excel exports recalculate with the new uplift (without compounding previous uplifts).
- **Current Rates**: upload CSV/XLSX or paste Excel rows (first row must be headers), preview and save. Supports MPAN/MPRN, site, fuel, usage, kVA, rates and dates. Matching supply numbers are updated rather than duplicated.
- **Offers**: upload PDF/EML/CSV/XLSX/TXT or paste offer text. Extraction is a *draft*; check the supplier, supply number, term, unit rates and charges in the review form before saving. Embedded PDF, spreadsheet and text email attachments are parsed. Image-only PDFs require manual entry. The tool does NOT guess a contract term if absent.
- **Supplier Terms**: save supplier-specific generator matching, volume tolerance, minimum volume/take-or-pay, pass-through, termination, payment, credit, validity, and additional contractual conditions. Entries are per client and supplier/fuel, editable through save/replace.
- **Declines**: record suppliers that declined with reasons; declines remain in the app but are not a separate Excel export tab.
- **Comparison & Export**: compare offers by client, site and term (12/24/36/48/60/custom), including optional 1/2/3-year equivalents. Uplifts are applied *once in calculations* and do not modify supplier base rates. Export a workbook with exactly four tabs: Current Rates, Electricity Offers, Gas Offers and Supplier Terms. Ranks are computed on a like-for-like annual basis for comparable offers.

## Important notes
- If an offer includes no MPAN/MPRN and you have multiple supplies, select the correct supply before saving.
- Supplier quotes may have different time periods. The app stores `term_months` and also labels actual start/end dates separately. Always check dates against the proposal.
- For dual-rate meters, `day_usage` and `night_usage` are applied to day/night unit rates. Where single-rate offers exist, fill `day_rate` and leave `night_rate` blank; the system applies it to total kWh.
- Current electricity costs exclude non-standard pass-through items unless you record them as `other_annual_cost_gbp`.
- If no annual usage is available, annual cost and ranking will be blank instead of fabricated.
- Review extracted figures: PDF/EML documents may contain old quotes in email chains or multiple offers. Use the upload review form or manually enter individual offers as needed.


For online deployment see **README_WEBSITE.md**.
