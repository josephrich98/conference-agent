# Field taxonomy (draft)

This is a **draft** of the field taxonomy that drives discovery. It is the
review surface for extending the agent across **medicine**, **genomics**, and
**data science**. The seeds it describes live in `SEED_CONFERENCES`
(`conference_agent/config.py`); this document explains *why* each field and its
flagships were chosen and flags the decisions that need your sign-off.

## How the taxonomy maps to the code

- **Two levels: category → subcategory.** Each conference carries one or more
  granular **subcategory** tags (the specific field) and a broad **category** (one
  of ten top-level buckets: humanities, social science, medicine, biology,
  chemistry, physics, mathematics, stats, computer science, artificial intelligence). The
  category is **derived**, never hand-set: every subcategory maps to exactly one
  category via `models.SUBCATEGORY_TO_CATEGORY`, and a series' category is the
  de-duplicated set of its subcategories' buckets (so MICCAI — radiology +
  machine learning — is `medicine, artificial intelligence`). Editing a
  subcategory's bucket is one map entry.
- **Subcategory = one or more lowercase tags.** A seed's subcategory element is
  either a single string (e.g. `cardiology`) or a tuple when a conference spans
  fields (e.g. SPR is `("radiology", "pediatrics")`; MICCAI is `("radiology",
  "machine learning")`; every CSHL meeting carries a `genomics` tag alongside any
  clinical field). `normalize_subcategories` flattens either form, and a
  conference is covered by every field it is tagged with. Subspecialties *within*
  a single field are not separate tags; they ride inside the parent field's seeds
  (as radiology does).
- **Coverage is flagship-only.** Each field carries ~2-5 marquee meetings; the
  discovery agent finds the long tail and verifies dates against official sites.
- **Adding a field = adding its flagship seeds (+ one map entry).**
  `seed_subcategories()` derives the standing subcategory list from the seed
  table; `weekly_subcategories()` / `monthly_subcategories()` derive the refresh
  schedule. A brand-new subcategory also needs a `SUBCATEGORY_TO_CATEGORY` entry
  so its category derives (a test enforces this). No second list to maintain.

## Refresh cadence

Discovery runs **per field** (one web-search pass per subcategory), so cadence is
set per field, not per conference.

- **Weekly** — flagship, fast-moving fields. Controlled by `WEEKLY_SUBCATEGORIES`:
  `radiology`, `cardiology`, `oncology`, `genomics`, `machine learning`.
  Run locally via `daily_update.py --cadence weekly`.
- **Monthly** — every other field. Run locally via
  `daily_update.py --cadence monthly`.

These per-field cadences can be scheduled from the AWS SAM stack as EventBridge
Scheduler rules (daily `due` / weekly / monthly) targeting a refresh Lambda
(`web/refresh_handler.py`); see `infra/template.yaml` and `DEPLOY_AWS.md`. That
stack is torn down, so the live schedule is the local cron job running the watch
cadence (below).

Weekly and monthly are disjoint and together cover every seeded field. To change
a field's cadence, move it in/out of `WEEKLY_SUBCATEGORIES` (one edit).

### Auto-check (per-series, targeted)

Layered on top of the per-field cadence is a per-conference policy
(`conference_agent.refresh`) that spends discovery calls only when a new edition
is plausibly about to be announced. A series is **due for a check** once its most
recent known edition is between `CHECK_WINDOW_MIN_MONTHS` (6) and
`CHECK_WINDOW_MAX_MONTHS` (24) old, measured from that edition's earliest
submission deadline (or its start date when no deadline is known). That is old
enough that next year's dates may be out soon, and recent enough to assume the
series is still active; the two-year ceiling keeps biennial meetings in view.
Within that window it is re-checked every `RECHECK_INTERVAL_DAYS` (14) days
(tracked per row via `last_checked`) until either a future edition is found (at
which point it is **updated** and dropped) or the edition ages past two years,
at which point checking stops. A never-checked, date-less row is checked once so
freshly seeded rows get an initial pass.

`daily_update.py --cadence due` refreshes the whole fields that contain a due
series. The scheduled job uses the finer **watch** cadence instead.

### Watch (per-series, page-gated) — the scheduled job

`daily_update.py --cadence watch` runs daily from cron
(`scripts/scheduled_discovery.sh`). Each series falls in at most one tier:

- **daily**: an upcoming submission deadline within `WATCH_DAILY_WINDOW_DAYS`
  (14) days before or after today, when extensions are usually announced.
  Checked every run.
- **soon**: an upcoming deadline or the meeting's start date within the next
  `WATCH_SOON_WINDOW_DAYS` (30) days. Checked every 14 days.
- **stale**: in the 6–24-month window above. Checked every 14 days.

A check is agent-free: `conference_agent/page_watch.py` fetches the official link
and up to three same-site dates / deadlines / call-for-abstracts pages and hashes
the set of dates they mention (stored as `watch_fingerprint`, with the check date
in `watch_checked`). The agent re-researches a series, in a targeted run of up
to `WATCH_BATCH_SIZE` (6) named series (`discover.refresh_conferences`), only when
the fingerprint changed; when the pages could not be read and the series has not
been researched in 14 days; or when `WATCH_BACKSTOP_DAYS` (28) have passed
without research. At most `WATCH_MAX_AGENT_PER_RUN` (18) series are researched
per run, in tier order; the rest are deferred to the next run. Results are merged
fill-only (`database.apply_refreshed_conferences`), so a targeted run never
clears a field it did not re-find.

> **Decision for review:** the weekly set is currently the five highest-velocity
> fields. Tell me which others should be weekly (each weekly field is one extra
> LLM discovery run per week).

## Domains and fields

### Medicine

Seeded specialties (flagships from
[Med School Insiders — Medical Conferences by Specialty](https://medschoolinsiders.com/medical-student/medical-conferences-by-specialty/);
the requested r/medicalschool thread is host-blocked to automated fetches, so
this sourced equivalent was used):

| Field | Flagship seeds |
|---|---|
| radiology | RSNA, ECR, ARRS, ACR, + subspecialty societies (16 total) |
| allergy and immunology | AAAAI, ACAAI, EAACI |
| anesthesiology | ASA, IARS, ESAIC |
| cardiology | ACC, AHA, ESC, HRS, TCT |
| critical care medicine | SCCM, ESICM |
| dermatology | AAD, SID, EADV |
| emergency medicine | ACEP, SAEM, ICEM |
| endocrinology | ENDO, ADA, EASD |
| family medicine | AAFP, STFM, WONCA |
| gastroenterology | DDW, ACG, UEGW, AASLD, EASL |
| geriatrics | AGS, GSA, IAGG |
| hematology | ASH, EHA, ISTH |
| infectious disease | IDWeek, CROI, ECCMID |
| internal medicine | ACP, SHM, EFIM |
| medical physics | AAPM |
| nephrology | ASN, ERA, WCN |
| neurology | AAN, SfN, EAN, AES, ISC, MDS (+ CSHL neuro meetings) |
| neurosurgery | AANS, CNS, WFNS |
| obstetrics and gynecology | ACOG, SMFM, FIGO |
| oncology | ASCO, ESMO, AACR, SABCS, SITC, SGO (+ CSHL cancer meeting) |
| ophthalmology | AAO, ARVO, ESCRS |
| orthopedics | AAOS, ORS, EFORT |
| otolaryngology | AAOHNS, COSM, IFOS |
| palliative care | AAHPM, EAPC |
| pathology | USCAP, CAP, ECP |
| pediatrics | AAP, PAS, EAP |
| physical medicine and rehabilitation | AAPMR, ISPRM |
| plastic surgery | ASPS, AAPS, IPRAS |
| psychiatry | APA, EPA |
| public health | APHA |
| pulmonology | ATS, CHEST, ERS |
| radiation oncology | ASTRO, ESTRO |
| rheumatology | ACR-RHEUM, EULAR |
| sports medicine | AMSSM, ACSM |
| surgery | ACS, STS, VAM |
| urology | AUA, EAU |

The `ACR` acronym collision is resolved: American College of Radiology keeps
`ACR` (radiology); American College of Rheumatology Convergence is seeded as
`ACR-RHEUM` (rheumatology), since the acronym is the upsert key and must be
unique.

**Reddit sourcing note.** The requested
[r/medicalschool thread](https://www.reddit.com/r/medicalschool/comments/133c95c/which_conferences_to_go_to_for_each_specialty/)
is host-blocked to automated fetches (direct, old.reddit, JSON, and proxy access
all refused). Its well-known per-specialty recommendations were transcribed from
domain knowledge and cross-checked against the Med School Insiders specialty
list; both are recorded in `SEED_CONFERENCE_SOURCES`.

### Genomics / bioinformatics

Human/medical genetics and computational-biology flagships, plus the Cold Spring
Harbor Laboratory meetings you asked to include in full. The seed set follows the
requested
[r/bioinformatics thread](https://www.reddit.com/r/bioinformatics/comments/x3g2da/what_are_some_of_the_top_bioinformatics/)
(also host-blocked to automated fetches; its recommendations were transcribed and
verified against ISCB and official conference sites).

- **Genetics / clinical-genomics flagships:** ASHG, ESHG, AGBT, ACMG, HUGO, TAGC.
- **Computational-biology flagships:** ISMB, RECOMB (+ satellites RECOMB-SEQ,
  RECOMB-CG, RECOMB-GENETICS), ECCB, PSB, APBC, GLBIO, BOSC, GCC, JOBIM, GIW.
- **Specialized genomics:** PAG (plant/animal), SCG (single-cell), Bio-IT World.
- **All CSHL meetings** ([meetings.cshl.edu](https://meetings.cshl.edu/meetingshome.aspx)),
  each given a stable `CSHL-*` id (CSHL meetings have no official acronyms).

> **Decision for review — CSHL routing.** CSHL meetings span many fields. I filed
> the genomics/genetics/comp-bio/molecular-biology ones under `genomics`, and
> routed the unambiguous clinical ones to their field instead of `genomics`:
> - → `oncology`: Mechanisms & Models of Cancer
> - → `neurology`: Glia in Health & Disease; Molecular Mechanisms of Neuronal
>   Connectivity; Neurodegenerative Diseases; Development & 3D Modeling of the
>   Human Brain; Brain Barriers
>
> A few CSHL meetings under `genomics` are really basic biology rather than
> genomics proper (Mechanisms of Aging, Metabolic Signaling, Systems Immunology,
> Social Insects, Cell & Membrane Fusion). Tell me whether to (a) keep them under
> `genomics`, (b) give them their own fields, or (c) drop them.

### Data science / machine learning / AI

The conference venues from a field-grouped list of top ML/AI venues. Series that
are also core machine-learning venues (COLM, CVPR, ICCV, ECCV, CoRL, KDD, COLT)
carry a second `machine learning` tag.

| Field | Category | Flagship seeds |
|---|---|---|
| machine learning | artificial intelligence | NeurIPS, ICML, ICLR, UAI, AISTATS (+ `statistics`), AAAI, IJCAI |
| natural language processing | artificial intelligence | ACL, EMNLP, EACL, NAACL, IJCNLP-AACL, COLM |
| computer vision | artificial intelligence | CVPR, ICCV, ECCV, 3DV |
| robotics | computer science | ICRA, IROS, RSS, CoRL |
| data mining | computer science | KDD |
| learning theory | artificial intelligence | COLT |

> **Not seeded:** the journals on that list (JMLR, TMLR, CL, TACL, PAMI, JAIR)
> accept rolling submissions, so they have no deadlines or meeting dates for the
> table or calendar feed. Named sub-tracks (NeurIPS Datasets & Benchmarks,
> ACL/EMNLP "Findings") are part of their parent conference's row.

## Seed coverage at a glance

228 seed conferences across 62 fields (medicine, genomics + CSHL, ML/AI,
chemistry, physics, biology, statistics, computer science, and mathematics). The seeds bootstrap discovery; the agent finds each
field's long tail and verifies dates against official sites.

## Open decisions (summary)

1. **Weekly set** — the weekly fields are `radiology`, `cardiology`, `oncology`,
   `genomics`, `machine learning`; every other field refreshes monthly. Tell me
   which others (e.g. `hematology`) should move to weekly.
2. **CSHL routing** — keep the basic-biology meetings under `genomics`, split, or drop?
3. **Data-science scope** — the conference venues listed under
   "Data science / machine learning / AI" above, split into machine learning,
   NLP, computer vision, robotics, data mining, and learning theory; journals
   are excluded.
