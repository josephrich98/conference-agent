"""Constants, controlled vocabularies, and seed lists.

Centralizes values that are referenced across modules so they are changed in one
place: the Anthropic model id used by the discovery agent, default database
location, and a seed list of conferences to bootstrap discovery.
"""

from __future__ import annotations

import os

from conference_agent.models import ConferenceTier

# --- Discovery agent -------------------------------------------------------

# Anthropic model id used by the discovery agent. Centralized here so it is
# updated in one place. Consult the `claude-api` skill for the current id.
ANTHROPIC_MODEL = os.environ.get("CONFERENCE_AGENT_MODEL", "claude-opus-4-8")

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# --- Storage ---------------------------------------------------------------

# Default SQLAlchemy URL. Overridable via env or the `--db` CLI flag. The file
# lives under data/, which is gitignored.
DEFAULT_DATABASE_URL = os.environ.get(
    "CONFERENCE_DATABASE_URL", "sqlite:///data/conferences.db"
)

# --- Notifications ---------------------------------------------------------

# Where to send the "table refreshed" email after a discovery / daily run.
# Defaults to the project owner; override via env. SMTP settings are read from
# the environment so no credentials are committed (see calendar_sync/notify).
NOTIFY_EMAIL = os.environ.get("CONFERENCE_NOTIFY_EMAIL", "josephrich98@gmail.com")
SMTP_HOST = os.environ.get("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER")  # e.g. a Gmail address
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")  # e.g. a Gmail app password

# --- Calendar feed (.ics) --------------------------------------------------

# Lead times (in days before the event) for the reminders attached to every
# event in the iCalendar feed -- four weeks, one week, and one day ahead.
CALENDAR_REMINDER_LEAD_DAYS = (28, 7, 1)
# Hour of day (local, 24h) the reminders fire on their target morning. An all-day
# event starts at midnight, so anchoring the alarm at this hour keeps calendar
# apps from labeling a midnight alarm as one day too early (see
# ``calendar_sync._alarm_trigger``).
CALENDAR_REMINDER_HOUR = 9

# --- Reputation policy ------------------------------------------------------

# Only these flagship meetings warrant the "big" tier. Every other conference is
# capped at "medium" (a model-assigned "big" is demoted); "medium"/"small"
# assignments are otherwise preserved. Centralized here so the rule is changed in
# one place and applied deterministically during discovery. Matching is
# case-insensitive (see ``normalize_reputation``), so entries are uppercase.
BIG_CONFERENCE_ACRONYMS = {
    # Radiology
    "RSNA", "ECR",
    # Data science / machine learning
    "NEURIPS", "ICML", "ICLR", "CVPR", "ICCV",
    # Genomics / bioinformatics (ISMB is ISCB's flagship meeting)
    "RECOMB", "ASHG", "ISMB",
    # Medicine (one or more flagships per specialty)
    "AAFP", "AAP", "ACP", "CORD", "AAN", "ASA", "ACS", "ASC",
    "SAGES", "STS", "ACNP", "APA", "PAS", "AAOS", "USCAP", "CAP", "ARVO",
    "AAO", "ACOG", "AAOHNS", "COSM", "VAM", "ASCO", "AACR", "CNS", "AAD",
    "AUA",
    # WARC (Western Anesthesia Residents' Conference) is intentionally excluded:
    # a regional residents' meeting, capped below the national flagships.
}


def normalize_reputation(
    acronym: str, tier: "ConferenceTier | None"
) -> "ConferenceTier | None":
    """Apply the house reputation policy to a (model- or seed-) assigned tier.

    Flagship conferences (:data:`BIG_CONFERENCE_ACRONYMS`) are always ``big``;
    any other conference assigned ``big`` is downgraded to ``medium``. Tiers of
    ``medium``/``small``/``None`` pass through unchanged.
    """
    if acronym and acronym.upper() in BIG_CONFERENCE_ACRONYMS:
        return ConferenceTier.BIG
    if tier == ConferenceTier.BIG:
        return ConferenceTier.MEDIUM
    return tier


# --- Seed list -------------------------------------------------------------

# A seed of flagship conferences to bootstrap and sanity-check the discovery
# agent. The ``category`` is a flat lowercase field string (e.g. "radiology",
# "cardiology", "machine learning"); subspecialties are noted inline rather than
# given their own category, so a single ``discover --category radiology`` run
# covers the whole field -- ``_seed_checklist`` filters seeds by the requested
# category. Each field carries only its flagship meetings (coverage is "flagship
# only"); the discovery agent finds the rest and verifies details against
# official sources.
#
# Adding a new field is just adding its flagship seeds here: ``seed_categories()``
# below derives the standing category list from this table, so a new field is
# automatically picked up by the daily refresh.
#
# Reputation tiers are illustrative starting points, not authoritative;
# ``normalize_reputation`` caps non-flagship meetings at "medium". The designated
# "big" meetings span every domain (see ``BIG_CONFERENCE_ACRONYMS``); remaining
# seeds are "medium" until designated as their field's apex meeting.
# (acronym, full name, category, reputation)
SEED_CONFERENCES = [
    # --- Radiology ---------------------------------------------------------
    # General / cross-subspecialty
    ("RSNA", "Radiological Society of North America Annual Meeting", "radiology", ConferenceTier.BIG),
    ("ECR", "European Congress of Radiology", "radiology", ConferenceTier.BIG),
    ("ARRS", "American Roentgen Ray Society Annual Meeting", "radiology", ConferenceTier.MEDIUM),
    ("ACR", "American College of Radiology Annual Meeting", "radiology", ConferenceTier.MEDIUM),
    ("AAR", "Association of Academic Radiology Annual Meeting", "radiology", ConferenceTier.MEDIUM),
    ("CAR", "Canadian Association of Radiologists Annual Scientific Meeting", "radiology", ConferenceTier.MEDIUM),
    ("RANZCR", "Royal Australian and New Zealand College of Radiologists Annual Scientific Meeting", "radiology", ConferenceTier.MEDIUM),
    # Subspecialty societies
    ("SPR", "Society for Pediatric Radiology Annual Meeting", "radiology", ConferenceTier.MEDIUM),
    ("ASNR", "American Society of Neuroradiology Annual Meeting", "radiology", ConferenceTier.MEDIUM),
    ("ASHNR", "American Society of Head and Neck Radiology Annual Meeting", "radiology", ConferenceTier.MEDIUM),
    ("SIR", "Society of Interventional Radiology Annual Scientific Meeting", "radiology", ConferenceTier.MEDIUM),
    ("SAR", "Society of Abdominal Radiology Annual Meeting", "radiology", ConferenceTier.MEDIUM),
    ("SBI", "Society of Breast Imaging Annual Symposium", "radiology", ConferenceTier.MEDIUM),
    ("ISS", "International Skeletal Society Annual Meeting", "radiology", ConferenceTier.MEDIUM),
    ("SNMMI", "Society of Nuclear Medicine and Molecular Imaging Annual Meeting", "radiology", ConferenceTier.MEDIUM),
    ("SIIM", "Society for Imaging Informatics in Medicine Annual Meeting", "radiology", ConferenceTier.MEDIUM),

    # --- Medicine (other specialties) --------------------------------------
    # Flagship conference(s) per specialty. Sourced from Med School Insiders'
    # "Medical Conferences by Specialty" (see SEED_CONFERENCE_SOURCES); the
    # discovery agent finds the rest of each field.
    # Anesthesiology
    ("ASA", "American Society of Anesthesiologists Annual Meeting", "anesthesiology", ConferenceTier.BIG),
    ("IARS", "International Anesthesia Research Society Annual Meeting", "anesthesiology", ConferenceTier.MEDIUM),
    ("ESAIC", "Euroanaesthesia (European Society of Anaesthesiology and Intensive Care)", "anesthesiology", ConferenceTier.MEDIUM),
    ("WARC", "Western Anesthesia Residents' Conference", "anesthesiology", ConferenceTier.SMALL),
    # Cardiology
    ("ACC", "American College of Cardiology Annual Scientific Session", "cardiology", ConferenceTier.MEDIUM),
    ("AHA", "American Heart Association Scientific Sessions", "cardiology", ConferenceTier.MEDIUM),
    ("ESC", "European Society of Cardiology Congress", "cardiology", ConferenceTier.MEDIUM),
    ("HRS", "Heart Rhythm Society Annual Scientific Sessions", "cardiology", ConferenceTier.MEDIUM),
    ("TCT", "Transcatheter Cardiovascular Therapeutics", "cardiology", ConferenceTier.MEDIUM),
    # Dermatology
    ("AAD", "American Academy of Dermatology Annual Meeting", "dermatology", ConferenceTier.BIG),
    ("SID", "Society for Investigative Dermatology Annual Meeting", "dermatology", ConferenceTier.MEDIUM),
    ("EADV", "European Academy of Dermatology and Venereology Congress", "dermatology", ConferenceTier.MEDIUM),
    # Emergency medicine
    ("ACEP", "American College of Emergency Physicians Scientific Assembly", "emergency medicine", ConferenceTier.MEDIUM),
    ("SAEM", "Society for Academic Emergency Medicine Annual Meeting", "emergency medicine", ConferenceTier.MEDIUM),
    ("ICEM", "International Conference on Emergency Medicine", "emergency medicine", ConferenceTier.MEDIUM),
    ("CORD", "Council of Residency Directors in Emergency Medicine Academic Assembly", "emergency medicine", ConferenceTier.BIG),
    # Endocrinology
    ("ENDO", "Endocrine Society Annual Meeting", "endocrinology", ConferenceTier.MEDIUM),
    ("ADA", "American Diabetes Association Scientific Sessions", "endocrinology", ConferenceTier.MEDIUM),
    ("EASD", "European Association for the Study of Diabetes Annual Meeting", "endocrinology", ConferenceTier.MEDIUM),
    # Family medicine
    ("AAFP", "American Academy of Family Physicians (FMX)", "family medicine", ConferenceTier.BIG),
    ("STFM", "Society of Teachers of Family Medicine Annual Spring Conference", "family medicine", ConferenceTier.MEDIUM),
    ("WONCA", "World Organization of Family Doctors World Conference", "family medicine", ConferenceTier.MEDIUM),
    # Gastroenterology
    ("DDW", "Digestive Disease Week", "gastroenterology", ConferenceTier.MEDIUM),
    ("ACG", "American College of Gastroenterology Annual Scientific Meeting", "gastroenterology", ConferenceTier.MEDIUM),
    ("UEGW", "United European Gastroenterology Week", "gastroenterology", ConferenceTier.MEDIUM),
    ("AASLD", "American Association for the Study of Liver Diseases (The Liver Meeting)", "gastroenterology", ConferenceTier.MEDIUM),
    ("EASL", "European Association for the Study of the Liver (International Liver Congress)", "gastroenterology", ConferenceTier.MEDIUM),
    # Internal medicine
    ("ACP", "American College of Physicians Internal Medicine Meeting", "internal medicine", ConferenceTier.BIG),
    ("SHM", "Society of Hospital Medicine Annual Conference", "internal medicine", ConferenceTier.MEDIUM),
    ("EFIM", "European Federation of Internal Medicine Congress", "internal medicine", ConferenceTier.MEDIUM),
    # Neurology
    ("AAN", "American Academy of Neurology Annual Meeting", "neurology", ConferenceTier.BIG),
    ("SfN", "Society for Neuroscience Annual Meeting", "neurology", ConferenceTier.MEDIUM),
    ("EAN", "European Academy of Neurology Congress", "neurology", ConferenceTier.MEDIUM),
    ("AES", "American Epilepsy Society Annual Meeting", "neurology", ConferenceTier.MEDIUM),
    ("ISC", "International Stroke Conference", "neurology", ConferenceTier.MEDIUM),
    ("MDS", "International Parkinson and Movement Disorder Society Congress", "neurology", ConferenceTier.MEDIUM),
    # Obstetrics & gynecology
    ("ACOG", "American College of Obstetricians and Gynecologists Annual Meeting", "obstetrics and gynecology", ConferenceTier.BIG),
    ("SMFM", "Society for Maternal-Fetal Medicine Annual Pregnancy Meeting", "obstetrics and gynecology", ConferenceTier.MEDIUM),
    ("FIGO", "International Federation of Gynecology and Obstetrics World Congress", "obstetrics and gynecology", ConferenceTier.MEDIUM),
    # Oncology
    ("ASCO", "American Society of Clinical Oncology Annual Meeting", "oncology", ConferenceTier.BIG),
    ("ESMO", "European Society for Medical Oncology Congress", "oncology", ConferenceTier.MEDIUM),
    ("AACR", "American Association for Cancer Research Annual Meeting", "oncology", ConferenceTier.BIG),
    ("SABCS", "San Antonio Breast Cancer Symposium", "oncology", ConferenceTier.MEDIUM),
    ("SITC", "Society for Immunotherapy of Cancer Annual Meeting", "oncology", ConferenceTier.MEDIUM),
    ("SGO", "Society of Gynecologic Oncology Annual Meeting on Women's Cancer", "oncology", ConferenceTier.MEDIUM),
    # Ophthalmology
    ("AAO", "American Academy of Ophthalmology Annual Meeting", "ophthalmology", ConferenceTier.BIG),
    ("ARVO", "Association for Research in Vision and Ophthalmology Annual Meeting", "ophthalmology", ConferenceTier.BIG),
    ("ESCRS", "European Society of Cataract and Refractive Surgeons Congress", "ophthalmology", ConferenceTier.MEDIUM),
    # Orthopedics
    ("AAOS", "American Academy of Orthopaedic Surgeons Annual Meeting", "orthopedics", ConferenceTier.BIG),
    ("ORS", "Orthopaedic Research Society Annual Meeting", "orthopedics", ConferenceTier.MEDIUM),
    ("EFORT", "European Federation of National Associations of Orthopaedics and Traumatology Congress", "orthopedics", ConferenceTier.MEDIUM),
    # Pediatrics
    ("AAP", "American Academy of Pediatrics National Conference & Exhibition", "pediatrics", ConferenceTier.BIG),
    ("PAS", "Pediatric Academic Societies Annual Meeting", "pediatrics", ConferenceTier.BIG),
    ("EAP", "European Academy of Pediatrics Congress", "pediatrics", ConferenceTier.MEDIUM),
    # Psychiatry
    ("APA", "American Psychiatric Association Annual Meeting", "psychiatry", ConferenceTier.BIG),
    ("EPA", "European Congress of Psychiatry", "psychiatry", ConferenceTier.MEDIUM),
    ("ACNP", "American College of Neuropsychopharmacology Annual Meeting", "psychiatry", ConferenceTier.BIG),
    # Pulmonology / critical care
    ("ATS", "American Thoracic Society International Conference", "pulmonology", ConferenceTier.MEDIUM),
    ("CHEST", "American College of Chest Physicians Annual Meeting (CHEST)", "pulmonology", ConferenceTier.MEDIUM),
    ("ERS", "European Respiratory Society International Congress", "pulmonology", ConferenceTier.MEDIUM),
    # Surgery
    ("ACS", "American College of Surgeons Clinical Congress", "surgery", ConferenceTier.BIG),
    ("STS", "Society of Thoracic Surgeons Annual Meeting", "surgery", ConferenceTier.BIG),
    ("VAM", "Vascular Annual Meeting (Society for Vascular Surgery)", "surgery", ConferenceTier.BIG),
    ("ASC", "Academic Surgical Congress", "surgery", ConferenceTier.BIG),
    ("SAGES", "Society of American Gastrointestinal and Endoscopic Surgeons Annual Meeting", "surgery", ConferenceTier.BIG),
    # Urology
    ("AUA", "American Urological Association Annual Meeting", "urology", ConferenceTier.BIG),
    ("EAU", "European Association of Urology Annual Congress", "urology", ConferenceTier.MEDIUM),
    # Allergy & immunology
    ("AAAAI", "American Academy of Allergy, Asthma & Immunology Annual Meeting", "allergy and immunology", ConferenceTier.MEDIUM),
    ("ACAAI", "American College of Allergy, Asthma & Immunology Annual Scientific Meeting", "allergy and immunology", ConferenceTier.MEDIUM),
    ("EAACI", "European Academy of Allergy and Clinical Immunology Congress", "allergy and immunology", ConferenceTier.MEDIUM),
    # Critical care medicine
    ("SCCM", "Society of Critical Care Medicine Critical Care Congress", "critical care medicine", ConferenceTier.MEDIUM),
    ("ESICM", "European Society of Intensive Care Medicine Annual Congress (LIVES)", "critical care medicine", ConferenceTier.MEDIUM),
    # Geriatrics
    ("AGS", "American Geriatrics Society Annual Scientific Meeting", "geriatrics", ConferenceTier.MEDIUM),
    ("GSA", "Gerontological Society of America Annual Scientific Meeting", "geriatrics", ConferenceTier.MEDIUM),
    ("IAGG", "International Association of Gerontology and Geriatrics World Congress", "geriatrics", ConferenceTier.MEDIUM),
    # Hematology
    ("ASH", "American Society of Hematology Annual Meeting", "hematology", ConferenceTier.MEDIUM),
    ("EHA", "European Hematology Association Congress", "hematology", ConferenceTier.MEDIUM),
    ("ISTH", "International Society on Thrombosis and Haemostasis Congress", "hematology", ConferenceTier.MEDIUM),
    # Infectious disease
    ("IDWeek", "IDWeek (Infectious Diseases Society of America and partners)", "infectious disease", ConferenceTier.MEDIUM),
    ("CROI", "Conference on Retroviruses and Opportunistic Infections", "infectious disease", ConferenceTier.MEDIUM),
    ("ECCMID", "ESCMID Global (European Congress of Clinical Microbiology & Infectious Diseases)", "infectious disease", ConferenceTier.MEDIUM),
    # Medical physics (imaging-adjacent)
    ("AAPM", "American Association of Physicists in Medicine Annual Meeting", "medical physics", ConferenceTier.MEDIUM),
    # Nephrology
    ("ASN", "American Society of Nephrology Kidney Week", "nephrology", ConferenceTier.MEDIUM),
    ("ERA", "European Renal Association Congress", "nephrology", ConferenceTier.MEDIUM),
    ("WCN", "World Congress of Nephrology", "nephrology", ConferenceTier.MEDIUM),
    # Neurosurgery
    ("AANS", "American Association of Neurological Surgeons Annual Scientific Meeting", "neurosurgery", ConferenceTier.MEDIUM),
    ("CNS", "Congress of Neurological Surgeons Annual Meeting", "neurosurgery", ConferenceTier.BIG),
    ("WFNS", "World Federation of Neurosurgical Societies World Congress", "neurosurgery", ConferenceTier.MEDIUM),
    # Otolaryngology (ENT)
    ("AAOHNS", "American Academy of Otolaryngology-Head and Neck Surgery Annual Meeting", "otolaryngology", ConferenceTier.BIG),
    ("COSM", "Combined Otolaryngology Spring Meetings", "otolaryngology", ConferenceTier.BIG),
    ("IFOS", "International Federation of Otorhinolaryngological Societies World Congress", "otolaryngology", ConferenceTier.MEDIUM),
    # Palliative care
    ("AAHPM", "American Academy of Hospice and Palliative Medicine Annual Assembly", "palliative care", ConferenceTier.MEDIUM),
    ("EAPC", "European Association for Palliative Care World Congress", "palliative care", ConferenceTier.MEDIUM),
    # Pathology
    ("USCAP", "United States and Canadian Academy of Pathology Annual Meeting", "pathology", ConferenceTier.BIG),
    ("CAP", "College of American Pathologists Annual Meeting", "pathology", ConferenceTier.BIG),
    ("ECP", "European Congress of Pathology", "pathology", ConferenceTier.MEDIUM),
    # Physical medicine & rehabilitation
    ("AAPMR", "American Academy of Physical Medicine and Rehabilitation Annual Assembly", "physical medicine and rehabilitation", ConferenceTier.MEDIUM),
    ("ISPRM", "International Society of Physical and Rehabilitation Medicine World Congress", "physical medicine and rehabilitation", ConferenceTier.MEDIUM),
    # Plastic surgery
    ("ASPS", "American Society of Plastic Surgeons (Plastic Surgery The Meeting)", "plastic surgery", ConferenceTier.MEDIUM),
    ("AAPS", "American Association of Plastic Surgeons Annual Meeting", "plastic surgery", ConferenceTier.MEDIUM),
    ("IPRAS", "International Confederation for Plastic Reconstructive and Aesthetic Surgery World Congress", "plastic surgery", ConferenceTier.MEDIUM),
    # Public health / preventive medicine
    ("APHA", "American Public Health Association Annual Meeting & Expo", "public health", ConferenceTier.MEDIUM),
    # Radiation oncology
    ("ASTRO", "American Society for Radiation Oncology Annual Meeting", "radiation oncology", ConferenceTier.MEDIUM),
    ("ESTRO", "European Society for Radiotherapy and Oncology Congress", "radiation oncology", ConferenceTier.MEDIUM),
    # Rheumatology (note: the rheumatology "ACR" collides with American College of
    # Radiology, which already owns the "ACR" id above; a disambiguated id is used.)
    ("ACR-RHEUM", "American College of Rheumatology Convergence", "rheumatology", ConferenceTier.MEDIUM),
    ("EULAR", "European Alliance of Associations for Rheumatology Congress", "rheumatology", ConferenceTier.MEDIUM),
    # Sports medicine
    ("AMSSM", "American Medical Society for Sports Medicine Annual Meeting", "sports medicine", ConferenceTier.MEDIUM),
    ("ACSM", "American College of Sports Medicine Annual Meeting", "sports medicine", ConferenceTier.MEDIUM),

    # --- Genomics / bioinformatics -----------------------------------------
    # Human/medical genetics and computational biology flagships.
    ("ASHG", "American Society of Human Genetics Annual Meeting", "genomics", ConferenceTier.BIG),
    ("ESHG", "European Human Genetics Conference", "genomics", ConferenceTier.MEDIUM),
    ("AGBT", "Advances in Genome Biology and Technology General Meeting", "genomics", ConferenceTier.MEDIUM),
    ("ACMG", "American College of Medical Genetics and Genomics Annual Clinical Genetics Meeting", "genomics", ConferenceTier.MEDIUM),
    ("ISMB", "Intelligent Systems for Molecular Biology", "genomics", ConferenceTier.BIG),
    ("RECOMB", "Research in Computational Molecular Biology", "genomics", ConferenceTier.BIG),
    ("ECCB", "European Conference on Computational Biology", "genomics", ConferenceTier.MEDIUM),
    ("PSB", "Pacific Symposium on Biocomputing", "genomics", ConferenceTier.MEDIUM),
    ("APBC", "Asia Pacific Bioinformatics Conference", "genomics", ConferenceTier.MEDIUM),
    ("GLBIO", "Great Lakes Bioinformatics Conference", "genomics", ConferenceTier.MEDIUM),
    ("BOSC", "Bioinformatics Open Source Conference", "genomics", ConferenceTier.MEDIUM),
    ("GCC", "Galaxy Community Conference", "genomics", ConferenceTier.MEDIUM),
    ("JOBIM", "Journees Ouvertes en Biologie, Informatique et Mathematiques", "genomics", ConferenceTier.MEDIUM),
    ("GIW", "Genome Informatics Workshop (GIW/ISCB-Asia)", "genomics", ConferenceTier.MEDIUM),
    ("RECOMB-SEQ", "RECOMB Satellite Workshop on Massively Parallel Sequencing", "genomics", ConferenceTier.MEDIUM),
    ("RECOMB-CG", "RECOMB Satellite Workshop on Comparative Genomics", "genomics", ConferenceTier.MEDIUM),
    ("RECOMB-GENETICS", "RECOMB Satellite Workshop on Computational Genetics", "genomics", ConferenceTier.MEDIUM),
    ("PAG", "Plant and Animal Genome Conference", "genomics", ConferenceTier.MEDIUM),
    ("HUGO", "Human Genome Meeting (Human Genome Organisation)", "genomics", ConferenceTier.MEDIUM),
    ("TAGC", "The Allied Genetics Conference (Genetics Society of America)", "genomics", ConferenceTier.MEDIUM),
    ("SCG", "Single Cell Genomics Conference", "genomics", ConferenceTier.MEDIUM),
    ("BIOITWORLD", "Bio-IT World Conference & Expo", "genomics", ConferenceTier.MEDIUM),
    ("MLCB", "Machine Learning in Computational Biology", "genomics", ConferenceTier.MEDIUM),
    # Cold Spring Harbor Laboratory meetings (meetings.cshl.edu). CSHL meetings
    # have no official acronyms, so a stable "CSHL-*" id is assigned. Meetings
    # whose topic is squarely oncology or neuroscience are filed under those
    # fields; the remainder (genomics, genetics, computational/molecular biology)
    # under "genomics".
    ("CSHL-BOG", "CSHL Biology of Genomes", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-GENINFO", "CSHL Genome Informatics", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-PROBGEN", "CSHL Probabilistic Modeling in Genomics", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-BIODATA", "CSHL Biological Data Science", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-NETBIO", "CSHL Network Biology", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-CRISPR", "CSHL Genome Engineering: CRISPR Frontiers", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-EPIG", "CSHL Epigenetics & Chromatin", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-TE", "CSHL Transposable Elements", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-TELO", "CSHL Telomeres & Telomerase", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-GERM", "CSHL Germ Cells", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-TRANSCTRL", "CSHL Translational Control", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-NAT", "CSHL Nucleic Acid Therapies", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-UBIQ", "CSHL Ubiquitin and Ubiquitin-Like Modifiers", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-SINGLEBIO", "CSHL Single Biomolecules", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-CELLFUSION", "CSHL Cell & Membrane Fusion", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-CELLMODEL", "CSHL Cell Modeling in Space and Time", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-MICROBIOME", "CSHL Microbiome", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-RETRO", "CSHL Retroviruses", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-SYSIMM", "CSHL Systems Immunology", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-METAB", "CSHL Mechanisms of Metabolic Signaling", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-AGING", "CSHL Mechanisms of Aging", "genomics", ConferenceTier.MEDIUM),
    ("CSHL-SOCINSECT", "CSHL Social Insects", "genomics", ConferenceTier.MEDIUM),
    # CSHL meetings routed to their clinical field
    ("CSHL-CANCER", "CSHL Mechanisms & Models of Cancer", "oncology", ConferenceTier.MEDIUM),
    ("CSHL-GLIA", "CSHL Glia in Health & Disease", "neurology", ConferenceTier.MEDIUM),
    ("CSHL-NEUROCONN", "CSHL Molecular Mechanisms of Neuronal Connectivity", "neurology", ConferenceTier.MEDIUM),
    ("CSHL-NEURODEGEN", "CSHL Neurodegenerative Diseases: Biology & Therapeutics", "neurology", ConferenceTier.MEDIUM),
    ("CSHL-BRAINDEV", "CSHL Development & 3D Modeling of the Human Brain", "neurology", ConferenceTier.MEDIUM),
    ("CSHL-BRAINBAR", "CSHL Brain Barriers", "neurology", ConferenceTier.MEDIUM),

    # --- Data science / machine learning -----------------------------------
    ("NeurIPS", "Conference on Neural Information Processing Systems", "machine learning", ConferenceTier.BIG),
    ("ICML", "International Conference on Machine Learning", "machine learning", ConferenceTier.BIG),
    ("ICLR", "International Conference on Learning Representations", "machine learning", ConferenceTier.BIG),
    ("CVPR", "IEEE/CVF Conference on Computer Vision and Pattern Recognition", "machine learning", ConferenceTier.BIG),
    ("ICCV", "IEEE/CVF International Conference on Computer Vision", "machine learning", ConferenceTier.BIG),
]


# Official website per seed conference, keyed by acronym (the seed id). Kept in a
# separate map rather than widening the seed tuple so existing seed consumers are
# untouched. Most point at the organizing society's stable home (deep meeting URLs
# rotate yearly); a few meetings with no permanent landing page are left ``None``.
# ``seed_conferences()`` uses this to populate each row's ``url`` field; rows can
# still be displayed when a URL is unknown (the rest of the fields stay empty
# until discovery fills them in).
_CSHL_MEETINGS_URL = "https://meetings.cshl.edu/meetings"
SEED_CONFERENCE_URLS: dict[str, "str | None"] = {
    # --- Radiology ---------------------------------------------------------
    "RSNA": "https://www.rsna.org",
    "ECR": "https://www.myesr.org",
    "ARRS": "https://www.arrs.org",
    "ACR": "https://www.acr.org",
    "AAR": "https://www.aur.org",
    "CAR": "https://car.ca",
    "RANZCR": "https://www.ranzcr.com",
    "SPR": "https://www.pedrad.org",
    "ASNR": "https://www.asnr.org",
    "ASHNR": "https://ashnr.org",
    "SIR": "https://www.sirweb.org",
    "SAR": "https://abdominalradiology.org",
    "SBI": "https://www.sbi-online.org",
    "ISS": "https://internationalskeletalsociety.com",
    "SNMMI": "https://www.snmmi.org",
    "SIIM": "https://siim.org",
    # --- Medicine ----------------------------------------------------------
    "ASA": "https://www.asahq.org",
    "IARS": "https://www.iars.org",
    "ESAIC": "https://www.esaic.org",
    "WARC": None,  # regional residents' meeting; no permanent site
    "ACC": "https://www.acc.org",
    "AHA": "https://professional.heart.org",
    "ESC": "https://www.escardio.org",
    "HRS": "https://www.hrsonline.org",
    "TCT": "https://www.crf.org",
    "AAD": "https://www.aad.org",
    "SID": "https://www.sidnet.org",
    "EADV": "https://www.eadv.org",
    "ACEP": "https://www.acep.org",
    "SAEM": "https://www.saem.org",
    "ICEM": "https://www.ifem.cc",
    "CORD": "https://www.cordem.org",
    "ENDO": "https://www.endocrine.org",
    "ADA": "https://professional.diabetes.org",
    "EASD": "https://www.easd.org",
    "AAFP": "https://www.aafp.org",
    "STFM": "https://www.stfm.org",
    "WONCA": "https://www.globalfamilydoctor.com",
    "DDW": "https://ddw.org",
    "ACG": "https://gi.org",
    "UEGW": "https://ueg.eu",
    "AASLD": "https://www.aasld.org",
    "EASL": "https://easl.eu",
    "ACP": "https://www.acponline.org",
    "SHM": "https://www.hospitalmedicine.org",
    "EFIM": "https://www.efim.org",
    "AAN": "https://www.aan.com",
    "SfN": "https://www.sfn.org",
    "EAN": "https://www.ean.org",
    "AES": "https://www.aesnet.org",
    "ISC": "https://www.stroke.org",
    "MDS": "https://www.movementdisorders.org",
    "ACOG": "https://www.acog.org",
    "SMFM": "https://www.smfm.org",
    "FIGO": "https://www.figo.org",
    "ASCO": "https://www.asco.org",
    "ESMO": "https://www.esmo.org",
    "AACR": "https://www.aacr.org",
    "SABCS": "https://www.sabcs.org",
    "SITC": "https://www.sitcancer.org",
    "SGO": "https://www.sgo.org",
    "AAO": "https://www.aao.org",
    "ARVO": "https://www.arvo.org",
    "ESCRS": "https://www.escrs.org",
    "AAOS": "https://www.aaos.org",
    "ORS": "https://www.ors.org",
    "EFORT": "https://www.efort.org",
    "AAP": "https://www.aap.org",
    "PAS": "https://www.pas-meeting.org",
    "EAP": "https://www.eapaediatrics.eu",
    "APA": "https://www.psychiatry.org",
    "EPA": "https://www.europsy.net",
    "ACNP": "https://www.acnp.org",
    "ATS": "https://www.thoracic.org",
    "CHEST": "https://www.chestnet.org",
    "ERS": "https://www.ersnet.org",
    "ACS": "https://www.facs.org",
    "STS": "https://www.sts.org",
    "VAM": "https://vascular.org",
    "ASC": "https://www.academicsurgicalcongress.org",
    "SAGES": "https://www.sages.org",
    "AUA": "https://www.auanet.org",
    "EAU": "https://uroweb.org",
    "AAAAI": "https://www.aaaai.org",
    "ACAAI": "https://acaai.org",
    "EAACI": "https://www.eaaci.org",
    "SCCM": "https://www.sccm.org",
    "ESICM": "https://www.esicm.org",
    "AGS": "https://www.americangeriatrics.org",
    "GSA": "https://www.geron.org",
    "IAGG": "https://www.iagg.info",
    "ASH": "https://www.hematology.org",
    "EHA": "https://ehaweb.org",
    "ISTH": "https://www.isth.org",
    "IDWeek": "https://idweek.org",
    "CROI": "https://www.croiconference.org",
    "ECCMID": "https://www.escmid.org",
    "AAPM": "https://www.aapm.org",
    "ASN": "https://www.asn-online.org",
    "ERA": "https://www.era-online.org",
    "WCN": "https://www.theisn.org",
    "AANS": "https://www.aans.org",
    "CNS": "https://www.cns.org",
    "WFNS": "https://www.wfns.org",
    "AAOHNS": "https://www.entnet.org",
    "COSM": "https://www.cosm.md",
    "IFOS": "https://www.ifosworld.org",
    "AAHPM": "https://aahpm.org",
    "EAPC": "https://www.eapcnet.eu",
    "USCAP": "https://www.uscap.org",
    "CAP": "https://www.cap.org",
    "ECP": "https://www.esp-pathology.org",
    "AAPMR": "https://www.aapmr.org",
    "ISPRM": "https://www.isprm.org",
    "ASPS": "https://www.plasticsurgery.org",
    "AAPS": "https://www.aaps1921.org",
    "IPRAS": "https://www.ipras.org",
    "APHA": "https://www.apha.org",
    "ASTRO": "https://www.astro.org",
    "ESTRO": "https://www.estro.org",
    "ACR-RHEUM": "https://www.rheumatology.org",
    "EULAR": "https://www.eular.org",
    "AMSSM": "https://www.amssm.org",
    "ACSM": "https://www.acsm.org",
    # --- Genomics / bioinformatics -----------------------------------------
    "ASHG": "https://www.ashg.org",
    "ESHG": "https://www.eshg.org",
    "AGBT": "https://www.agbt.org",
    "ACMG": "https://www.acmg.net",
    "ISMB": "https://www.iscb.org",
    "RECOMB": "https://recomb.org",
    "ECCB": "https://www.iscb.org",
    "PSB": "https://psb.stanford.edu",
    "APBC": None,  # rotating annual site; no permanent landing page
    "GLBIO": "https://www.iscb.org",
    "BOSC": "https://www.open-bio.org",
    "GCC": "https://galaxyproject.org",
    "JOBIM": "https://www.sfbi.fr",
    "GIW": None,  # rotating annual site; no permanent landing page
    "RECOMB-SEQ": "https://recomb.org",
    "RECOMB-CG": "https://recomb.org",
    "RECOMB-GENETICS": "https://recomb.org",
    "PAG": "https://www.intlpag.org",
    "HUGO": "https://www.hugo-international.org",
    "TAGC": "https://genetics-gsa.org",
    "SCG": None,  # rotating annual site; no permanent landing page
    "BIOITWORLD": "https://www.bio-itworldexpo.com",
    "MLCB": "https://www.mlcb.org",
    # --- Data science / machine learning -----------------------------------
    "NeurIPS": "https://neurips.cc",
    "ICML": "https://icml.cc",
    "ICLR": "https://iclr.cc",
    "CVPR": "https://cvpr.thecvf.com",
    "ICCV": "https://iccv.thecvf.com",
}
# CSHL meetings share the official meetings portal (individual meetings have no
# stable per-meeting permalink), so every CSHL-* seed maps to the same landing.
SEED_CONFERENCE_URLS.update(
    {acronym: _CSHL_MEETINGS_URL for acronym, *_ in SEED_CONFERENCES if acronym.startswith("CSHL-")}
)

# Deep meeting links for the flagship (big-tier) series, layered on top of the
# org homepages above. Each entry holds up to three tiers, most specific first:
#   "event"   -- the current/next edition's own page (most specific; can rot when
#                the edition rolls over, so it is re-verified alongside discovery)
#   "meeting" -- a stable annual-meeting landing path the org reuses every year
#   "org"     -- the organization homepage (mirrors SEED_CONFERENCE_URLS)
# A tier is ``None`` when no such page exists (or none could be verified to
# resolve). ``best_seed_url`` collapses an entry to its most specific non-None
# tier; series absent from this map fall back to their SEED_CONFERENCE_URLS
# homepage. Only big-tier series (see BIG_CONFERENCE_ACRONYMS) are listed, and
# all populated links were verified to return HTTP 200 as of 2026-06-17.
SEED_CONFERENCE_LINKS: dict[str, dict[str, "str | None"]] = {
    # --- Radiology ---------------------------------------------------------
    "RSNA": {"event": None, "meeting": "https://www.rsna.org/annual-meeting", "org": "https://www.rsna.org"},
    "ECR": {"event": None, "meeting": "https://myesr.org/congress/", "org": "https://www.myesr.org"},
    # --- Machine learning --------------------------------------------------
    "NeurIPS": {"event": "https://neurips.cc/Conferences/2026", "meeting": "https://neurips.cc/Conferences/FutureMeetings", "org": "https://neurips.cc"},
    "ICML": {"event": "https://icml.cc/Conferences/2026", "meeting": None, "org": "https://icml.cc"},
    "ICLR": {"event": None, "meeting": "https://iclr.cc/Conferences/FutureMeetings", "org": "https://iclr.cc"},
    "CVPR": {"event": None, "meeting": None, "org": "https://cvpr.thecvf.com"},
    "ICCV": {"event": None, "meeting": None, "org": "https://iccv.thecvf.com"},
    # --- Genomics / bioinformatics -----------------------------------------
    "RECOMB": {"event": None, "meeting": None, "org": "https://recomb.org"},
    "ASHG": {"event": "https://ashgmeeting.ashg.org/", "meeting": "https://www.ashg.org/meetings/", "org": "https://www.ashg.org"},
    "ISMB": {"event": "https://www.iscb.org/ismb2026/home", "meeting": "https://www.iscb.org/about-ismb", "org": "https://www.iscb.org"},
    # --- Medicine ----------------------------------------------------------
    "ASA": {"event": None, "meeting": "https://www.asahq.org/annualmeeting", "org": "https://www.asahq.org"},
    "AAD": {"event": "https://www.aad.org/member/meetings-education/am27", "meeting": "https://meetings.aad.org/", "org": "https://www.aad.org"},
    "CORD": {"event": None, "meeting": "https://www.cordem.org/event/academic-assembly/", "org": "https://www.cordem.org"},
    "AAFP": {"event": None, "meeting": "https://www.aafp.org/events/fmx.html", "org": "https://www.aafp.org"},
    "ACP": {"event": None, "meeting": "https://www.acponline.org/meetings-courses/internal-medicine-meeting", "org": "https://www.acponline.org"},
    "AAN": {"event": None, "meeting": "https://www.aan.com/events/annual-meeting", "org": "https://www.aan.com"},
    "ACOG": {"event": None, "meeting": "https://annualmeeting.acog.org/", "org": "https://www.acog.org"},
    "ASCO": {"event": None, "meeting": "https://www.asco.org/annual-meeting", "org": "https://www.asco.org"},
    "AACR": {"event": "https://www.aacr.org/meeting/aacr-annual-meeting-2027/", "meeting": "https://www.aacr.org/professionals/meetings/", "org": "https://www.aacr.org"},
    "AAO": {"event": "https://www.aao.org/annual-meeting/neworleans", "meeting": "https://www.aao.org/annual-meeting", "org": "https://www.aao.org"},
    "ARVO": {"event": None, "meeting": "https://www.arvo.org/annual-meeting", "org": "https://www.arvo.org"},
    "AAOS": {"event": None, "meeting": "https://www.aaos.org/annual/", "org": "https://www.aaos.org"},
    "AAP": {"event": None, "meeting": "https://aapexperience.org/", "org": "https://www.aap.org"},
    "PAS": {"event": "https://www.pas-meeting.org/2027-meeting/", "meeting": "https://www.pas-meeting.org/about/", "org": "https://www.pas-meeting.org"},
    "APA": {"event": None, "meeting": "https://www.psychiatry.org/annual-meeting", "org": "https://www.psychiatry.org"},
    "ACNP": {"event": None, "meeting": "https://acnp.org/annual-meeting/", "org": "https://acnp.org/"},
    "ACS": {"event": "https://www.facs.org/for-medical-professionals/conferences-and-meetings/clinical-congress-2026/", "meeting": "https://www.facs.org/for-medical-professionals/conferences-and-meetings/", "org": "https://www.facs.org"},
    "STS": {"event": "https://www.sts.org/calendar-of-events/63rd-sts-annual-meeting", "meeting": "https://www.sts.org/education/future-annual-meetings", "org": "https://www.sts.org"},
    "VAM": {"event": None, "meeting": "https://vascular.org/vascular-specialists/education-and-meetings/meetings/future-vam-dates", "org": "https://vascular.org"},
    "ASC": {"event": None, "meeting": None, "org": "https://www.academicsurgicalcongress.org"},
    "SAGES": {"event": "https://www.sages2027.org/", "meeting": "https://www.sages.org/meetings/annual-meeting/", "org": "https://www.sages.org"},
    "AUA": {"event": "https://www.auanet.org/AUA2027", "meeting": None, "org": "https://www.auanet.org"},
    "CNS": {"event": None, "meeting": "https://www.cns.org/annualmeeting", "org": "https://www.cns.org"},
    "AAOHNS": {"event": None, "meeting": "https://www.entnet.org/events/annual-meeting/", "org": "https://www.entnet.org/"},
    "COSM": {"event": None, "meeting": "https://cosm.md/annual-meeting/", "org": "https://www.cosm.md"},
    "USCAP": {"event": "https://2027am.uscap.org/", "meeting": "https://uscap.org/uscap-annual-meeting/", "org": "https://www.uscap.org"},
    "CAP": {"event": "https://www.cap.org/calendar/events/cap26", "meeting": "https://www.cap.org/calendar/events", "org": "https://www.cap.org"},
}

# Tier preference used by ``best_seed_url`` -- most specific first.
_SEED_LINK_TIERS = ("event", "meeting", "org")


def best_seed_url(acronym: str) -> "str | None":
    """Most specific known link for a seed, preferring a deep meeting link.

    Big-tier series may carry a tiered link set in :data:`SEED_CONFERENCE_LINKS`
    (an edition-specific ``event`` page, a stable ``meeting`` landing, and the
    ``org`` homepage); this returns the most specific tier that is populated.
    Every other series falls back to its :data:`SEED_CONFERENCE_URLS` homepage.
    """
    links = SEED_CONFERENCE_LINKS.get(acronym)
    if links is not None:
        for tier in _SEED_LINK_TIERS:
            if links.get(tier):
                return links[tier]
    return SEED_CONFERENCE_URLS.get(acronym)


def seed_categories() -> list[str]:
    """Distinct categories present in :data:`SEED_CONFERENCES`, sorted.

    The standing category set is derived from the seed table so that adding a new
    field's flagship seeds is enough to fold it into the daily refresh -- there is
    no second list to keep in sync.
    """
    seen: list[str] = []
    for _, _, category, _ in SEED_CONFERENCES:
        if category not in seen:
            seen.append(category)
    return sorted(seen)


# Categories the scheduled refresh iterates over (one discovery run each).
STANDING_CATEGORIES = seed_categories()

# --- Refresh cadence -------------------------------------------------------

# Discovery runs per field (one web-search pass per category), so cadence is set
# per field, not per conference. Fields that carry flagship, fast-moving meetings
# are refreshed weekly; every other field refreshes monthly. Editing this set is
# the single knob for a field's cadence. Names must match seed categories.
WEEKLY_CATEGORIES = {
    "radiology",
    "cardiology",
    "oncology",
    "genomics",
    "machine learning",
}


def weekly_categories() -> list[str]:
    """Seed categories refreshed weekly (intersection with WEEKLY_CATEGORIES)."""
    return sorted(c for c in seed_categories() if c in WEEKLY_CATEGORIES)


def monthly_categories() -> list[str]:
    """Seed categories refreshed monthly (everything not refreshed weekly)."""
    return sorted(c for c in seed_categories() if c not in WEEKLY_CATEGORIES)


# --- Per-conference auto-check policy --------------------------------------
#
# A finer schedule layered on top of the per-field cadence above, keyed on each
# series' most recent known edition. The intent is to spend discovery calls only
# when a new edition is plausibly about to be announced.
#
# A series becomes "due for a check" once its latest known edition is between
# CHECK_WINDOW_MIN_MONTHS and CHECK_WINDOW_MAX_MONTHS old: old enough that the
# next edition's dates may be published soon, but recent enough to assume the
# series is still active (not dead or infrequent). While inside that window it is
# re-checked every RECHECK_INTERVAL_DAYS days until either a future ("upcoming")
# edition is found -- at which point it is updated and no longer due -- or the
# edition ages past the maximum, at which point checking stops. These three
# numbers are the only knob for the policy. See ``conference_agent.refresh``.
CHECK_WINDOW_MIN_MONTHS = 6
CHECK_WINDOW_MAX_MONTHS = 12
RECHECK_INTERVAL_DAYS = 14

# Provenance for the seed list above: the reference pages used to compile the
# radiology seeds. Recorded for auditability and as starting points when refresh-
# ing or extending the seeds. These are curated aggregators, not authoritative
# sources -- the discovery agent still verifies dates/details against official
# society sites (see ``discover.py``).
# (label, url, note)
SEED_CONFERENCE_SOURCES = [
    (
        "Medality",
        "https://medality.com/blog/calendar-of-radiology-conferences-events-and-healthcare-awareness-dates/",
        "2026 calendar of radiology conferences, events, and awareness dates.",
    ),
    (
        "Weave",
        "https://www.getweave.com/radiology-conferences/",
        "List of major radiology conferences with societies and locations.",
    ),
    (
        "Med School Insiders",
        "https://medschoolinsiders.com/medical-student/medical-conferences-by-specialty/",
        "Major medical conferences sorted by specialty (medicine seeds).",
    ),
    (
        "Cold Spring Harbor Laboratory Meetings & Courses",
        "https://meetings.cshl.edu/meetingshome.aspx",
        "CSHL meetings list (genomics / computational biology seeds).",
    ),
    (
        "r/medicalschool — conferences by specialty",
        "https://www.reddit.com/r/medicalschool/comments/133c95c/which_conferences_to_go_to_for_each_specialty/",
        "Requested community source for the per-specialty medical flagships. The "
        "thread host blocks automated fetches, so its well-known recommendations "
        "were transcribed from domain knowledge and cross-checked against the "
        "Med School Insiders specialty list above.",
    ),
    (
        "r/bioinformatics — top bioinformatics conferences",
        "https://www.reddit.com/r/bioinformatics/comments/x3g2da/what_are_some_of_the_top_bioinformatics/",
        "Requested community source for the genomics / bioinformatics flagships "
        "(ISMB, RECOMB and its satellites, AGBT, Biology of Genomes, Genome "
        "Informatics, ASHG, GLBIO, BOSC, PAG, etc.). Host blocks automated "
        "fetches; recommendations transcribed and verified against ISCB/official "
        "conference sites.",
    ),
]
