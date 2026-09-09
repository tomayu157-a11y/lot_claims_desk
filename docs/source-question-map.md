# Source-to-Question Map (Phase 1 Clinical Foundation)

Reconciles three overlapping layers into one execution plan:

| Layer | Unit | Count | Purpose |
|---|---|---|---|
| Celestra flow (docx / md) | Steps 0-15 with concrete API calls | 16 | HOW to fetch |
| `framework.yaml` | Steps 1-15, buckets A-G | 15 steps / 7 buckets | WHEN to fetch (dependency graph) |
| `research_questions.yaml` | Stages 1-6, seed questions | 6 stages / 62 questions | WHAT to answer |
| `sources.yaml` | Source registry | 24 sources, 5 tiers | WHERE from, and how much to trust it |

Indication scope is **ALL and CLL**. The Celestra flow was written CLL-only, so every
adapter must take the indication as a parameter. Nothing may be hardcoded to CLL.

---

## 1. Verified endpoint status

Probed 2026-09-09 from the session container. Status is what the endpoint actually
returned, not what the source doc claims.

| Endpoint | Status | Notes |
|---|---|---|
| openFDA `drug/label.json` | 200 | Returns `set_id`. OR-joined synonyms work in one call. |
| openFDA `drug/drugsfda.json` | 200 | `application_number` accepts OR-batched values. |
| openFDA `drug/ndc.json` | 200 | Filter by `application_number`. |
| openFDA `drug/event.json` (FAERS) | 200 | `count=patient.reaction.reactionmeddrapt.exact` works. |
| Europe PMC `/search?resultType=core` | 200 | Title + abstract + PMID + PMCID + DOI + pubType in ONE call. |
| Orphadata cross-referencing | 200 | Resolves name to ORPHA code plus ICD-10/ICD-11/MeSH/UMLS. |
| Orphadata epidemiology / natural history | 200 | Keyed on ORPHA code. |
| ClinicalTrials.gov v2 | 200 | `query.cond` is loose; needs post-filtering. |
| DailyMed `/spls.json?application_number=` | 200 | Works. Returns 1 element for a real NDA. |
| DailyMed `/spls.json?drug_name=` | 200 | Works. |
| DailyMed `/spls.json?search_string=` | 200 | **Silently ignored. See correction C1.** |
| PMC ID converter | 200 | Warns that `tool` and `email` params are missing. |
| WHO GHO `/api/Indicator` | 200 | 414 KB catalogue. Cache it. |
| SEER Stat Facts HTML | 200 | Scrape target, no API. |
| Crossref `/works` | 200 | Used for ASCO discovery via DOI prefix 10.1200. |
| Purple Book downloads | 302 | Redirects. Treat as a local CSV drop. |
| CDC WONDER `D158` | 500 on GET | POST-only, needs an XML request body. |
| WHO ICD-11 `/mms/search` | **401** | OAuth client credentials not present. |
| LOINC `/searchapi/loincs` | **401** | Regenstrief account not present. |

---

## 2. Corrections to the Celestra flow

**C1. DailyMed `search_string` does not filter.** Steps 9 and 11 both instruct
`spls.json?search_string="chronic+lymphocytic+leukemia"`. Verified against three
requests: no param, a CLL string, and a nonsense string all return
`total_elements = 159125` with the same first record, a menthol gel. The parameter is
ignored and the caller silently receives the entire SPL catalogue.
Use `drug_name=` (verified working) or reach DailyMed via the `set_id` and
`application_number` that openFDA already returns.

**C2. Step 6 Call 3 is redundant.** The chain says openFDA label -> Drugs@FDA ->
DailyMed `/spls` to obtain a SETID. openFDA's label response already contains
`set_id`. Call 3 can be dropped, saving one request per drug. This is the
"use less api calls" note on the Step 6 heading.

**C3. Synonym searches collapse into one call.** Step 6 runs separate searches for
CLL, CLL/SLL, small lymphocytic lymphoma and B-cell CLL. openFDA supports OR inside a
field, so one query does it. Measured hit counts: 149 for the CLL synonym union, 266
for the ALL synonym union. Four to five calls become one.

**C4. Drugs@FDA batches.** `application_number:("NDA208573" OR "NDA205552")` returns
both applications in one response. Batch application numbers rather than looping.

**C5. Europe PMC replaces the three-call PubMed ranking chain.** Steps 2 and 8A both
carry a note asking for an API that returns titles alongside IDs so articles can be
ranked. `resultType=core` returns title, abstract, PMID, PMCID, DOI, journal and
publication type in a single response. That removes the ESearch -> ESummary -> EFetch
sequence for the ranking stage. Keep NCBI EFetch for full PubMed XML on selected
records only.

**C6. The ESMO PMID is stale and should not be hardcoded.** The doc pins 33091559
(2021) and asks "how to get this id ?? research". A publication-type query answers it.
Sorted by date it surfaces an ESMO interim update in Annals of Oncology, PMID
38969011 (2024), which is newer than the pinned record. Query shape:

```
(TITLE:"chronic lymphocytic leukaemia" OR TITLE:"chronic lymphocytic leukemia")
AND PUB_TYPE:"Practice Guideline"
sort=P_PDATE_D desc
```

Same shape resolves the ALL guideline and the 2026 EHA CLL guideline. Guideline
records are discovered per run, never pinned.

**C7. Step 8 ISPOR query is malformed.** The ESearch URL reads
`"Value+Health")[Journal]` with a stray closing parenthesis.

**C8. Circular dependency between epidemiology and codes.** Celestra Step 1C feeds
Step 4 ICD codes into the CDC WONDER POST, but `framework.yaml` has bucket B (codes)
depending on bucket A (epidemiology). A cannot wait for B and B cannot wait for A.
Resolution: bucket A takes mortality from SEER Stat Facts, which already publishes it,
and uses the Orphanet crosswalk as a provisional anchor. The exact WONDER pull moves
to bucket B or E once the authoritative code family exists, and is reconciled at the
code gate.

**C9. NCBI E-utilities etiquette.** The ID converter explicitly warned that `tool` and
`email` are missing. Set both on every NCBI request. Without an API key the rate limit
is 3 requests per second; with one it is 10.

---

## 3. Provisional terminology anchors

Retrieved live from Orphadata, not asserted from memory. These are provisional inputs
to bucket A only. Bucket B's local CMS files remain authoritative for billable codes.

| Indication | ORPHA | Preferred term | ICD-10 | ICD-11 | MeSH | UMLS |
|---|---|---|---|---|---|---|
| CLL | 67038 | B-cell chronic lymphocytic leukemia | C91.1 | 2A82.0 | D015451 | C0023434 |
| ALL | 513 | Acute lymphoblastic leukemia | C91.0 | *absent* | D054198 | C1961102 |

ALL has no ICD-11 crosswalk in Orphanet. Its ICD-11 stem must come from the WHO API,
which is currently returning 401. That blocks two stage 3 questions for ALL.

---

## 4. Bucket-by-bucket acquisition plan

Buckets are the execution unit. Wave order from `framework.yaml`:
**wave 1 = A + C** (no dependencies), **wave 2 = B + D**, **wave 3 = E**,
**wave 4 = F**, then **G**. Bucket G is governance and runs alongside everything.

### Bucket A — steps 1, 2 -> stage 1 (`DiseaseDiagnosisProfile`)

| Question theme | Source | Call |
|---|---|---|
| Definition, natural history | Orphadata | `rd-cross-referencing/orphacodes/names/{name}`, then `rd-natural_history/orphacodes/{code}` |
| Incidence, prevalence, survival, mortality | SEER Stat Facts | Scrape `statfacts/html/clyl.html` (CLL) and the ALL page; preserve the reporting period |
| Prevalence class, geographic area | Orphadata | `rd-epidemiology/orphacodes/{code}` |
| Global context | WHO GHO | Indicator catalogue; allowed to return `no_specific_indicator` |
| Subtype and molecular classification | Europe PMC + NCI PDQ | `resultType=core` ranking, then EFetch on selected records |
| Diagnostic criteria and workup | Europe PMC -> NCBI EFetch | iwCLL for CLL. ALL has no iwCLL equivalent; use NCCN substitute chain in section 6 |
| Risk stratification (Rai, Binet, CLL-IPI) | Europe PMC | Guideline-typed literature |

Output feeds B (test names to code), C (context) and E.

### Bucket C — steps 3, 6 -> stage 2 (`TreatmentEvidenceMaster`)

Runs concurrently with A.

| Question theme | Source | Call |
|---|---|---|
| Governing guidelines and versions | Europe PMC | Publication-type discovery per C6, never a pinned PMID |
| NCCN recommendations | NCCN | **Licensed.** No scraping. Placeholder adapter until an agreement exists |
| European guideline | EHA / ESMO via PubMed | Discovered, not pinned |
| Guideline substitute while NCCN is unavailable | NCI PDQ, ASH/Blood | Scrape PDQ health-professional version |
| FDA-approved agents and label indications | openFDA label | One OR-joined synonym query, then LLM validation of the indications text |
| Application and approval history | openFDA Drugs@FDA | OR-batched `application_number` |
| Full label sections | DailyMed SPL XML | `/spls/{SETID}.xml` using the `set_id` openFDA already returned |
| Recent approvals and label expansions | DailyMed history | `/spls/{SETID}/history.json`, then diff versions for first appearance of the indication |
| Class and mechanism | Label + literature | SPL sections plus ranked literature |

The indication-dating trick from Celestra is worth keeping: the original NDA date is not
the date the indication was added. Walk the label history and diff.

### Bucket B — steps 4, 5 -> stage 3 (`DiagnosticObservabilityCodebook`)

Depends on A, because step 2 supplies the test names that step 5 maps to codes.

| Question theme | Source | Access |
|---|---|---|
| ICD-10-CM diagnosis universe | CMS release files | **Local text files. No web search.** Active file set depends on run date |
| ICD-11 stem and extension codes | WHO ICD-11 API | OAuth then `/release/11/2026-01/mms/search`. **Currently 401** |
| ICD-9-CM legacy crosswalk | *no source registered* | **Gap.** Needs CMS GEMs files added to the registry |
| Lab and molecular test codes | LOINC Search API | `/searchapi/loincs?query=`. **Currently 401.** Validate the chosen code via FHIR, never let the model invent one |
| Procedure and service codes (HCPCS) | HCPCS release files | **Local text files. No web search** |
| Procedure codes (CPT) | AMA | **Licensed.** No anonymous full-code API. Never scrape |
| Remission and relapse coding conventions | ICD-10-CM tabular + literature | Local files plus guideline text |

This bucket carries the most blockers. Three of its five question themes need either a
credential or a file drop.

### Bucket D — steps 7, 8, 9, 10 -> stage 4 (`RegimenAndLOTLogic`)

Depends on C.

| Question theme | Source | Call |
|---|---|---|
| Line-of-therapy rules | Guideline + real-world literature | Europe PMC ranked, then full text |
| Regimen library by line and phase | Guideline, trials, literature | ClinicalTrials v2 `query.cond` then per-NCT hydration |
| Real-world treatment patterns | PubMed, JMCP | Journal-scoped: `"J Manag Care Spec Pharm"` |
| Health-economics patterns | ISPOR / Value in Health | Europe PMC `JOURNAL:"Value Health"`, one call with core |
| Component to J-code mapping | HCPCS files | Local. Bucket B output |
| NDC universe and labelers | openFDA NDC, DailyMed | `/spls/{SETID}/ndcs.json` or NDC by application number |
| Biosimilar and reference product | Purple Book | **Local CSV.** Monthly report contains all products, not just changes |
| Dosing, route, administration | DailyMed SPL | Dosage and administration section |
| Pharmacy vs medical benefit | *derived* | **No source states this.** Derive from J-code existence plus route of administration |
| Supportive-care exclusions | Crossref -> PubMed, DailyMed | Crossref DOI prefix 10.1200 for ASCO, then the 8-way classification |

Celestra's warning stands: a clinical-trial arm alone is not evidence that a regimen is
standard of care.

### Bucket E — steps 11, 12, 13 -> stage 5 (`PatientJourneyStateModel`)

Depends on B, C and D.

| Question theme | Source | Call |
|---|---|---|
| Discontinuation and switching signals | FAERS + DailyMed | Per-drug `count=patient.reaction.reactionmeddrapt.exact` |
| Adverse events and toxicity management | DailyMed SPL | Warnings, adverse reactions, dose modification, discontinuation sections |
| Response, relapse, progression | iwCLL / guideline | EFetch on the selected guideline record |
| MRD assessment | Guideline + literature | Same chain |
| Monitoring cadence, lab concepts | LOINC | Blocked at 401 |
| Transplant and cellular therapy end states | CIBMTR | Scrape summary slides. **No API** |
| Death and mortality | CDC WONDER | POST `D158` with the bucket B ICD family |

FAERS discipline is non-negotiable. One report carries multiple drugs and multiple
reactions with no drug-to-reaction linkage and no established causality. Use it for
signal frequency and hypothesis generation, then confirm against the label. Never phrase
a FAERS count as "drug X caused event Y".

### Bucket F — step 14 -> stage 6 (`ClinicalGapMatrix`)

Depends on every substantive bucket. Mostly a diff engine over prior outputs rather
than a fetch stage.

| Question theme | Source |
|---|---|
| Unmet need and treatment gaps | Europe PMC / PubMed, unmet-need and treatment-gap terms |
| Guideline vs label divergence | Diff bucket C guideline output against bucket C label output |
| Real-world evidence gaps | Diff bucket D against bucket C |
| Claims observability limits | Diff bucket B code coverage against buckets A, D and E concepts |
| Advocacy perspective | LLS, ACS. Site-specific scrapers, kept out of the API adapters |

### Bucket G — step 15 (`ValidatedClinicalFoundationPackage`)

No external API by design. 15A defines QA rules before any research runs, 15B runs
continuously, 15C is human SME review. Every record carries the provenance envelope:
status, source ids, source urls, evidence snippets, retrieval timestamp, source version,
extraction version, reviewer, approval timestamp.

---

## 5. Registry changes needed

**Sources used by the Celestra flow but missing from `sources.yaml`.** Each needs an id
before the flow can reference it:

`orphanet` (api, tier 1), `europepmc` (api, tier 2), `crossref` (api, tier 2),
`openfda` (api, tier 1), `faers` (api, tier 2), `cdc_wonder` (api, tier 1),
`who_gho` (api, tier 1), `purple_book` (local file, tier 1), `pmc` (api, tier 2),
`eha` (guideline, tier 1), `cms_icd10_files` and `cms_hcpcs_files` (local, tier 1),
`cms_gems` (local, tier 1, for the ICD-9 crosswalk gap).

**`access_method` values that understate what exists.** These are registered as
`targeted_search` but have a real API or a defined file source:

| Source id | Registered | Should be |
|---|---|---|
| `fda`, `fda_accessdata` | targeted_search | api (openFDA) |
| `loinc` | targeted_search | api, credentialed |
| `icd11` | targeted_search | api, OAuth |
| `nlm_dailymed_ndc` | targeted_search | api (openFDA NDC) |
| `cdc_icd10` | targeted_search | split: local files for codes, api for WONDER mortality |
| `cms` | targeted_search | local files. Celestra says explicitly not to web search these |
| `ama_cpt` | targeted_search | licensed dataset |
| `esmo`, `iwcll`, `ashpublications` | targeted_search | api via Europe PMC discovery |

`cdc_icd10` currently conflates two unrelated things, the ICD-10-CM code set and NCHS
mortality. They have different access methods and belong to different buckets.

**Tier policy note.** `tier_policy.approved_max_tier: 4` combined with `open_web` at
tier 5 and `fallback_only: true` means the general web can never be a primary citation.
That is the correct posture and the QA service should enforce it.

---

## 6. Question answerability

62 seed questions total, 31 per indication.

| Class | Count | Meaning |
|---|---|---|
| Answerable now from a working API | ~38 | Every endpoint returns 200 today |
| Blocked on a credential | ~8 | ICD-11 OAuth, LOINC account |
| Blocked on a local file drop | ~7 | ICD-10-CM, HCPCS, Purple Book, GEMs |
| Blocked on a license | ~4 | CPT full code set, NCCN guideline content |
| Derived, no source states it | ~5 | Benefit split, subtype share, observability limits |

**Hard blockers, in priority order:**

1. **CPT.** Four stage 3 questions name CPT directly. AMA requires a license and there
   is no anonymous full-code API. Without it those questions return partial answers
   from HCPCS and LOINC only.
2. **NCCN.** It is the default source for stages 2, 4 and 5 in both indications, and it
   is the one source the flow explicitly forbids scraping. Substitute NCI PDQ, iwCLL,
   ASH and the current European guideline, and label the gap in the report.
3. **ICD-11 OAuth and LOINC credentials.** Both returning 401. Between them they gate
   the stage 3 coding questions and the stage 5 monitoring questions.
4. **ICD-9-CM.** Both indications ask for a three-system crosswalk. No registered source
   provides ICD-9. CMS GEMs files must be added.
5. **ALL has no iwCLL equivalent.** The CLL flow leans on a single high-value consensus
   document for diagnosis, response and monitoring. ALL has no direct counterpart, so
   its bucket A and E extraction depends on NCCN or PDQ, which is exactly where the
   licensing blocker sits.

**Questions no listed source answers directly**, which must be derived and labelled as
derived rather than cited:

- Pharmacy versus medical benefit billing, both indications, stage 4.
- Approximate share per subtype, stage 1 expected output.
- Which concepts are not observable from administrative claims, stage 6. This is a
  property of the bucket B codebook, not a fact to look up.
- Treatment-duration conventions for continuous BTK inhibitors versus fixed-duration
  venetoclax, CLL stage 4. Inferred from label dosing plus guideline text.

---

## 7. Call-budget effect of the corrections

Per indication, for the drug universe alone:

| Stage | As written | With C2 + C3 + C4 |
|---|---|---|
| Label discovery | 4-5 synonym calls | 1 |
| SETID lookup | 1 per drug | 0, reuse `set_id` |
| Drugs@FDA | 1 per application | 1 per batch |

The literature pipeline drops from three calls per ranking round (ESearch, ESummary,
EFetch) to one Europe PMC core search, with NCBI EFetch reserved for records that
survive ranking.
