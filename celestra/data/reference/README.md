# Reference datasets

Four sources in the registry are read from disk rather than an API, because
that is what their publishers support. Drop the files here and the matching
connector switches from "reference file not installed" to live.

| Dataset key | Expected file | Where it comes from |
|---|---|---|
| `icd10cm` | `icd10cm_order_*.txt` or `icd10cm_codes_*.txt` | CMS ICD-10-CM release files. Use the file set effective on your run date. |
| `hcpcs` | `hcpcs_*.txt` or `HCPC*.csv` | CMS HCPCS quarterly release. |
| `gems` | `*gem*.txt` | CMS ICD-9-CM to ICD-10-CM General Equivalence Mappings. Supplies the ICD-9 leg of the three-system crosswalk. |
| `purple_book` | `purplebook-search-*.csv` | https://purplebooksearch.fda.gov/downloads — each monthly report contains every product, not only that month's changes. |

Two further coding sources cannot be installed this way:

- **CPT** is licensed by the AMA. There is no anonymous full-code API and the
  pages must not be scraped. Questions that name CPT will report the licence as
  the blocker rather than guessing a code.
- **NCCN** guideline content is licensed. The registry keeps it as a source so
  the gap is visible, and the run substitutes NCI PDQ, iwCLL, ASH and the
  current European guideline.
