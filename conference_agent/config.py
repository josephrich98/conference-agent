"""Constants, controlled vocabularies, and seed lists.

Centralizes values that are referenced across modules so they are changed in one
place: the Anthropic model id used by the discovery agent, default database
location, and a seed list of conferences to bootstrap discovery.
"""

from __future__ import annotations

import os

from conference_agent.models import normalize_subcategories

# --- Discovery agent -------------------------------------------------------

# Anthropic model id used by the discovery agent. Centralized here so it is
# updated in one place. Consult the `claude-api` skill for the current id.
ANTHROPIC_MODEL = os.environ.get("CONFERENCE_AGENT_MODEL", "claude-opus-4-8")

ANTHROPIC_API_KEY_ENV = "ANTHROPIC_API_KEY"

# --- Natural-language search (local LLM) -----------------------------------

# Optional natural-language → boolean-query translation for the web table runs
# against a free, local Ollama server (https://ollama.com), so it needs no API
# key and no network egress. The base URL points at Ollama's HTTP API; the model
# is a small, fast instruction model (e.g. ``llama3.2:3b`` or ``qwen2.5:1.5b``).
# Both are overridable via the environment. The feature degrades gracefully: when
# the server is unreachable the web UI still works with the manual boolean box.
OLLAMA_BASE_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")
NL_QUERY_MODEL = os.environ.get("CONFERENCE_NL_QUERY_MODEL", "qwen2.5:1.5b")
# Seconds to wait on the local model before giving up (small models are fast, but
# a cold load can take a few seconds).
NL_QUERY_TIMEOUT = float(os.environ.get("CONFERENCE_NL_QUERY_TIMEOUT", "30"))

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

# --- Seed list -------------------------------------------------------------

# A seed of flagship conferences to bootstrap and sanity-check the discovery
# agent. The subcategory element is one or more lowercase field tags: either a
# single string (e.g. "radiology") or a tuple when a conference spans fields
# (e.g. SPR is ("radiology", "pediatrics"); MICCAI is ("radiology", "machine
# learning")). ``normalize_subcategories`` flattens either form, so a conference
# tagged with a field is covered by that field's ``discover`` run --
# ``_seed_checklist`` filters seeds by whether any tag matches the requested
# subcategory. The broad ``category`` (one of ``models.CATEGORIES``) is derived
# from these tags, never seeded directly. Subspecialties within a single field are
# still noted inline rather than given their own tag. Each field carries only its
# flagship meetings (coverage is "flagship only"); the discovery agent finds the
# rest and verifies details against official sources.
#
# Adding a new field is just adding its flagship seeds here: ``seed_subcategories()``
# below derives the standing subcategory list from this table, so a new field is
# automatically picked up by the daily refresh. A brand-new subcategory must also
# be mapped in ``models.SUBCATEGORY_TO_CATEGORY`` so its category derives.
#
# A seed carries only identity and classification -- no size. Size is derived from
# a sourced attendance figure (see ``models.size_for_attendance``), which is a
# discovered fact like dates, links, and cost; discovery fills it in, and a row's
# size stays blank until then.
# (acronym, full name, subcategory)
SEED_CONFERENCES = [
    # --- Radiology ---------------------------------------------------------
    # General / cross-subspecialty
    ("RSNA", "Radiological Society of North America Annual Meeting", "radiology"),
    ("ECR", "European Congress of Radiology", "radiology"),
    ("ARRS", "American Roentgen Ray Society Annual Meeting", "radiology"),
    ("ACR", "American College of Radiology Annual Meeting", "radiology"),
    ("AAR", "Association of Academic Radiology Annual Meeting", "radiology"),
    ("CAR", "Canadian Association of Radiologists Annual Scientific Meeting", "radiology"),
    ("RANZCR", "Royal Australian and New Zealand College of Radiologists Annual Scientific Meeting", "radiology"),
    # Subspecialty societies
    ("SPR", "Society for Pediatric Radiology Annual Meeting", ("radiology", "pediatrics")),
    ("ASNR", "American Society of Neuroradiology Annual Meeting", "radiology"),
    ("ASHNR", "American Society of Head and Neck Radiology Annual Meeting", "radiology"),
    ("SIR", "Society of Interventional Radiology Annual Scientific Meeting", "radiology"),
    ("SAR", "Society of Abdominal Radiology Annual Meeting", "radiology"),
    ("SBI", "Society of Breast Imaging Annual Symposium", "radiology"),
    ("ISS", "International Skeletal Society Annual Meeting", "radiology"),
    ("SNMMI", "Society of Nuclear Medicine and Molecular Imaging Annual Meeting", "radiology"),
    ("SIIM", "Society for Imaging Informatics in Medicine Annual Meeting", ("radiology", "machine learning")),
    # Medical image computing (technical / proceedings-based, not a clinical
    # society); tagged both radiology and machine learning.
    ("MICCAI", "Medical Image Computing and Computer Assisted Intervention", ("radiology", "machine learning")),

    # --- Medicine (other specialties) --------------------------------------
    # Flagship conference(s) per specialty. Sourced from Med School Insiders'
    # "Medical Conferences by Specialty" (see SEED_CONFERENCE_SOURCES); the
    # discovery agent finds the rest of each field.
    # Anesthesiology
    ("ASA", "American Society of Anesthesiologists Annual Meeting", "anesthesiology"),
    ("IARS", "International Anesthesia Research Society Annual Meeting", "anesthesiology"),
    ("ESAIC", "Euroanaesthesia (European Society of Anaesthesiology and Intensive Care)", "anesthesiology"),
    ("WARC", "Western Anesthesia Residents' Conference", "anesthesiology"),
    # Cardiology
    ("ACC", "American College of Cardiology Annual Scientific Session", "cardiology"),
    ("AHA", "American Heart Association Scientific Sessions", "cardiology"),
    ("ESC", "European Society of Cardiology Congress", "cardiology"),
    ("HRS", "Heart Rhythm Society Annual Scientific Sessions", "cardiology"),
    ("TCT", "Transcatheter Cardiovascular Therapeutics", "cardiology"),
    # Dermatology
    ("AAD", "American Academy of Dermatology Annual Meeting", "dermatology"),
    ("SID", "Society for Investigative Dermatology Annual Meeting", "dermatology"),
    ("EADV", "European Academy of Dermatology and Venereology Congress", "dermatology"),
    # Emergency medicine
    ("ACEP", "American College of Emergency Physicians Scientific Assembly", "emergency medicine"),
    ("SAEM", "Society for Academic Emergency Medicine Annual Meeting", "emergency medicine"),
    ("ICEM", "International Conference on Emergency Medicine", "emergency medicine"),
    ("CORD", "Council of Residency Directors in Emergency Medicine Academic Assembly", "emergency medicine"),
    # Endocrinology
    ("ENDO", "Endocrine Society Annual Meeting", "endocrinology"),
    ("ADA", "American Diabetes Association Scientific Sessions", "endocrinology"),
    ("EASD", "European Association for the Study of Diabetes Annual Meeting", "endocrinology"),
    # Family medicine
    ("AAFP", "American Academy of Family Physicians (FMX)", "family medicine"),
    ("STFM", "Society of Teachers of Family Medicine Annual Spring Conference", "family medicine"),
    ("WONCA", "World Organization of Family Doctors World Conference", "family medicine"),
    # Gastroenterology
    ("DDW", "Digestive Disease Week", "gastroenterology"),
    ("ACG", "American College of Gastroenterology Annual Scientific Meeting", "gastroenterology"),
    ("UEGW", "United European Gastroenterology Week", "gastroenterology"),
    ("AASLD", "American Association for the Study of Liver Diseases (The Liver Meeting)", "gastroenterology"),
    ("EASL", "European Association for the Study of the Liver (International Liver Congress)", "gastroenterology"),
    # Internal medicine
    ("ACP", "American College of Physicians Internal Medicine Meeting", "internal medicine"),
    ("SHM", "Society of Hospital Medicine Annual Conference", "internal medicine"),
    ("EFIM", "European Federation of Internal Medicine Congress", "internal medicine"),
    # Neurology
    ("AAN", "American Academy of Neurology Annual Meeting", "neurology"),
    ("SfN", "Society for Neuroscience Annual Meeting", "neurology"),
    ("EAN", "European Academy of Neurology Congress", "neurology"),
    ("AES", "American Epilepsy Society Annual Meeting", "neurology"),
    ("ISC", "International Stroke Conference", "neurology"),
    ("MDS", "International Parkinson and Movement Disorder Society Congress", "neurology"),
    # Obstetrics & gynecology
    ("ACOG", "American College of Obstetricians and Gynecologists Annual Meeting", "obstetrics and gynecology"),
    ("SMFM", "Society for Maternal-Fetal Medicine Annual Pregnancy Meeting", "obstetrics and gynecology"),
    ("FIGO", "International Federation of Gynecology and Obstetrics World Congress", "obstetrics and gynecology"),
    # Oncology
    ("ASCO", "American Society of Clinical Oncology Annual Meeting", "oncology"),
    ("ESMO", "European Society for Medical Oncology Congress", "oncology"),
    ("AACR", "American Association for Cancer Research Annual Meeting", "oncology"),
    ("SABCS", "San Antonio Breast Cancer Symposium", "oncology"),
    ("SITC", "Society for Immunotherapy of Cancer Annual Meeting", "oncology"),
    ("SGO", "Society of Gynecologic Oncology Annual Meeting on Women's Cancer", "oncology"),
    # Ophthalmology
    ("AAO", "American Academy of Ophthalmology Annual Meeting", "ophthalmology"),
    ("ARVO", "Association for Research in Vision and Ophthalmology Annual Meeting", "ophthalmology"),
    ("ESCRS", "European Society of Cataract and Refractive Surgeons Congress", "ophthalmology"),
    # Orthopedics
    ("AAOS", "American Academy of Orthopaedic Surgeons Annual Meeting", "orthopedics"),
    ("ORS", "Orthopaedic Research Society Annual Meeting", "orthopedics"),
    ("EFORT", "European Federation of National Associations of Orthopaedics and Traumatology Congress", "orthopedics"),
    # Pediatrics
    ("AAP", "American Academy of Pediatrics National Conference & Exhibition", "pediatrics"),
    ("PAS", "Pediatric Academic Societies Annual Meeting", "pediatrics"),
    ("EAP", "European Academy of Pediatrics Congress", "pediatrics"),
    # Psychiatry
    ("APA", "American Psychiatric Association Annual Meeting", "psychiatry"),
    ("EPA", "European Congress of Psychiatry", "psychiatry"),
    ("ACNP", "American College of Neuropsychopharmacology Annual Meeting", "psychiatry"),
    # Pulmonology / critical care
    ("ATS", "American Thoracic Society International Conference", "pulmonology"),
    ("CHEST", "American College of Chest Physicians Annual Meeting (CHEST)", "pulmonology"),
    ("ERS", "European Respiratory Society International Congress", "pulmonology"),
    # Surgery
    ("ACS", "American College of Surgeons Clinical Congress", "surgery"),
    ("STS", "Society of Thoracic Surgeons Annual Meeting", "surgery"),
    ("VAM", "Vascular Annual Meeting (Society for Vascular Surgery)", "surgery"),
    ("ASC", "Academic Surgical Congress", "surgery"),
    ("SAGES", "Society of American Gastrointestinal and Endoscopic Surgeons Annual Meeting", "surgery"),
    # Urology
    ("AUA", "American Urological Association Annual Meeting", "urology"),
    ("EAU", "European Association of Urology Annual Congress", "urology"),
    # Allergy & immunology
    ("AAAAI", "American Academy of Allergy, Asthma & Immunology Annual Meeting", "allergy and immunology"),
    ("ACAAI", "American College of Allergy, Asthma & Immunology Annual Scientific Meeting", "allergy and immunology"),
    ("EAACI", "European Academy of Allergy and Clinical Immunology Congress", "allergy and immunology"),
    # Critical care medicine
    ("SCCM", "Society of Critical Care Medicine Critical Care Congress", "critical care medicine"),
    ("ESICM", "European Society of Intensive Care Medicine Annual Congress (LIVES)", "critical care medicine"),
    # Geriatrics
    ("AGS", "American Geriatrics Society Annual Scientific Meeting", "geriatrics"),
    ("GSA", "Gerontological Society of America Annual Scientific Meeting", "geriatrics"),
    ("IAGG", "International Association of Gerontology and Geriatrics World Congress", "geriatrics"),
    # Hematology
    ("ASH", "American Society of Hematology Annual Meeting", "hematology"),
    ("EHA", "European Hematology Association Congress", "hematology"),
    ("ISTH", "International Society on Thrombosis and Haemostasis Congress", "hematology"),
    # Infectious disease
    ("IDWeek", "IDWeek (Infectious Diseases Society of America and partners)", "infectious disease"),
    ("CROI", "Conference on Retroviruses and Opportunistic Infections", "infectious disease"),
    ("ECCMID", "ESCMID Global (European Congress of Clinical Microbiology & Infectious Diseases)", "infectious disease"),
    # Medical physics (imaging-adjacent)
    ("AAPM", "American Association of Physicists in Medicine Annual Meeting", "medical physics"),
    # Nephrology
    ("ASN", "American Society of Nephrology Kidney Week", "nephrology"),
    ("ERA", "European Renal Association Congress", "nephrology"),
    ("WCN", "World Congress of Nephrology", "nephrology"),
    # Neurosurgery
    ("AANS", "American Association of Neurological Surgeons Annual Scientific Meeting", "neurosurgery"),
    ("CNS", "Congress of Neurological Surgeons Annual Meeting", "neurosurgery"),
    ("WFNS", "World Federation of Neurosurgical Societies World Congress", "neurosurgery"),
    # Otolaryngology (ENT)
    ("AAOHNS", "American Academy of Otolaryngology-Head and Neck Surgery Annual Meeting", "otolaryngology"),
    ("COSM", "Combined Otolaryngology Spring Meetings", "otolaryngology"),
    ("IFOS", "International Federation of Otorhinolaryngological Societies World Congress", "otolaryngology"),
    # Palliative care
    ("AAHPM", "American Academy of Hospice and Palliative Medicine Annual Assembly", "palliative care"),
    ("EAPC", "European Association for Palliative Care World Congress", "palliative care"),
    # Pathology
    ("USCAP", "United States and Canadian Academy of Pathology Annual Meeting", "pathology"),
    ("CAP", "College of American Pathologists Annual Meeting", "pathology"),
    ("ECP", "European Congress of Pathology", "pathology"),
    # Physical medicine & rehabilitation
    ("AAPMR", "American Academy of Physical Medicine and Rehabilitation Annual Assembly", "physical medicine and rehabilitation"),
    ("ISPRM", "International Society of Physical and Rehabilitation Medicine World Congress", "physical medicine and rehabilitation"),
    # Plastic surgery
    ("ASPS", "American Society of Plastic Surgeons (Plastic Surgery The Meeting)", "plastic surgery"),
    ("AAPS", "American Association of Plastic Surgeons Annual Meeting", "plastic surgery"),
    ("IPRAS", "International Confederation for Plastic Reconstructive and Aesthetic Surgery World Congress", "plastic surgery"),
    # Public health / preventive medicine
    ("APHA", "American Public Health Association Annual Meeting & Expo", "public health"),
    # Radiation oncology
    ("ASTRO", "American Society for Radiation Oncology Annual Meeting", "radiation oncology"),
    ("ESTRO", "European Society for Radiotherapy and Oncology Congress", "radiation oncology"),
    # Rheumatology (note: the rheumatology "ACR" collides with American College of
    # Radiology, which already owns the "ACR" id above; a disambiguated id is used.)
    ("ACR-RHEUM", "American College of Rheumatology Convergence", "rheumatology"),
    ("EULAR", "European Alliance of Associations for Rheumatology Congress", "rheumatology"),
    # Sports medicine
    ("AMSSM", "American Medical Society for Sports Medicine Annual Meeting", "sports medicine"),
    ("ACSM", "American College of Sports Medicine Annual Meeting", "sports medicine"),

    # --- Genomics / bioinformatics -----------------------------------------
    # Human/medical genetics and computational biology flagships.
    ("ASHG", "American Society of Human Genetics Annual Meeting", "genomics"),
    ("ESHG", "European Human Genetics Conference", "genomics"),
    ("AGBT", "Advances in Genome Biology and Technology General Meeting", "genomics"),
    ("ACMG", "American College of Medical Genetics and Genomics Annual Clinical Genetics Meeting", "genomics"),
    ("ISMB", "Intelligent Systems for Molecular Biology", "genomics"),
    ("RECOMB", "Research in Computational Molecular Biology", "genomics"),
    ("ECCB", "European Conference on Computational Biology", "genomics"),
    ("PSB", "Pacific Symposium on Biocomputing", "genomics"),
    ("APBC", "Asia Pacific Bioinformatics Conference", "genomics"),
    ("GLBIO", "Great Lakes Bioinformatics Conference", "genomics"),
    ("BOSC", "Bioinformatics Open Source Conference", "genomics"),
    ("GCC", "Galaxy Community Conference", "genomics"),
    ("JOBIM", "Journees Ouvertes en Biologie, Informatique et Mathematiques", "genomics"),
    ("GIW", "Genome Informatics Workshop (GIW/ISCB-Asia)", "genomics"),
    ("RECOMB-SEQ", "RECOMB Satellite Workshop on Massively Parallel Sequencing", "genomics"),
    ("RECOMB-CG", "RECOMB Satellite Workshop on Comparative Genomics", "genomics"),
    ("RECOMB-GENETICS", "RECOMB Satellite Workshop on Computational Genetics", "genomics"),
    ("PAG", "Plant and Animal Genome Conference", "genomics"),
    ("HUGO", "Human Genome Meeting (Human Genome Organisation)", "genomics"),
    ("TAGC", "The Allied Genetics Conference (Genetics Society of America)", "genomics"),
    ("SCG", "Single Cell Genomics Conference", "genomics"),
    ("BIOITWORLD", "Bio-IT World Conference & Expo", "genomics"),
    ("MLCB", "Machine Learning in Computational Biology", ("genomics", "machine learning")),
    # Cold Spring Harbor Laboratory meetings (meetings.cshl.edu). CSHL meetings
    # have no official acronyms, so a stable "CSHL-*" id is assigned. Meetings
    # whose topic is squarely oncology or neuroscience are filed under those
    # fields; the remainder (genomics, genetics, computational/molecular biology)
    # under "genomics".
    ("CSHL-BOG", "CSHL Biology of Genomes", "genomics"),
    ("CSHL-GENINFO", "CSHL Genome Informatics", "genomics"),
    ("CSHL-PROBGEN", "CSHL Probabilistic Modeling in Genomics", "genomics"),
    ("CSHL-BIODATA", "CSHL Biological Data Science", ("genomics", "machine learning")),
    ("CSHL-NETBIO", "CSHL Network Biology", "genomics"),
    ("CSHL-CRISPR", "CSHL Genome Engineering: CRISPR Frontiers", "genomics"),
    ("CSHL-EPIG", "CSHL Epigenetics & Chromatin", "genomics"),
    ("CSHL-TE", "CSHL Transposable Elements", "genomics"),
    ("CSHL-TELO", "CSHL Telomeres & Telomerase", "genomics"),
    ("CSHL-GERM", "CSHL Germ Cells", "genomics"),
    ("CSHL-TRANSCTRL", "CSHL Translational Control", "genomics"),
    ("CSHL-NAT", "CSHL Nucleic Acid Therapies", "genomics"),
    ("CSHL-UBIQ", "CSHL Ubiquitin and Ubiquitin-Like Modifiers", "genomics"),
    ("CSHL-SINGLEBIO", "CSHL Single Biomolecules", "genomics"),
    ("CSHL-CELLFUSION", "CSHL Cell & Membrane Fusion", "genomics"),
    ("CSHL-CELLMODEL", "CSHL Cell Modeling in Space and Time", "genomics"),
    ("CSHL-MICROBIOME", "CSHL Microbiome", "genomics"),
    ("CSHL-RETRO", "CSHL Retroviruses", "genomics"),
    ("CSHL-SYSIMM", "CSHL Systems Immunology", "genomics"),
    ("CSHL-METAB", "CSHL Mechanisms of Metabolic Signaling", "genomics"),
    ("CSHL-AGING", "CSHL Mechanisms of Aging", "genomics"),
    ("CSHL-SOCINSECT", "CSHL Social Insects", "genomics"),
    # CSHL meetings routed to their clinical field. Every CSHL meeting also
    # carries a "genomics" tag (its home domain), so the genomics view lists the
    # full CSHL series while these still surface under their clinical field too.
    ("CSHL-CANCER", "CSHL Mechanisms & Models of Cancer", ("oncology", "genomics")),
    ("CSHL-GLIA", "CSHL Glia in Health & Disease", ("neurology", "genomics")),
    ("CSHL-NEUROCONN", "CSHL Molecular Mechanisms of Neuronal Connectivity", ("neurology", "genomics")),
    ("CSHL-NEURODEGEN", "CSHL Neurodegenerative Diseases: Biology & Therapeutics", ("neurology", "genomics")),
    ("CSHL-BRAINDEV", "CSHL Development & 3D Modeling of the Human Brain", ("neurology", "genomics")),
    ("CSHL-BRAINBAR", "CSHL Brain Barriers", ("neurology", "genomics")),

    # --- Data science / machine learning -----------------------------------
    ("NeurIPS", "Conference on Neural Information Processing Systems", "machine learning"),
    ("ICML", "International Conference on Machine Learning", "machine learning"),
    ("ICLR", "International Conference on Learning Representations", "machine learning"),
    ("CVPR", "IEEE/CVF Conference on Computer Vision and Pattern Recognition", "machine learning"),
    ("ICCV", "IEEE/CVF International Conference on Computer Vision", "machine learning"),
    ("ECCV", "European Conference on Computer Vision", "machine learning"),
    ("AAAI", "AAAI Conference on Artificial Intelligence", "machine learning"),

    # --- Chemistry ---------------------------------------------------------
    ("ACS-CHEM", "American Chemical Society National Meeting & Exposition", "chemistry"),
    ("GCE", "ACS Annual Green Chemistry & Engineering Conference", "chemistry"),
    ("DDC", "Drug Discovery Chemistry", ("chemistry", "drug discovery")),
    ("PITTCON", "Pittcon Conference & Expo", ("chemistry", "analytical chemistry")),

    # --- Physics -----------------------------------------------------------
    ("APS", "APS Global Physics Summit", "physics"),
    ("DPG", "DPG Spring Meetings (Deutsche Physikalische Gesellschaft)", "physics"),
    ("ICHEP", "International Conference on High Energy Physics", "physics"),
    ("MORIOND", "Rencontres de Moriond", "physics"),
    ("TSRA", "Texas Symposium on Relativistic Astrophysics", "astrophysics"),
    ("CLEO", "Conference on Lasers and Electro-Optics", "optics"),

    # --- Biology (biophysics / biochemistry / cell biology) ----------------
    ("BPS", "Biophysical Society Annual Meeting", "biophysics"),
    ("ASBMB", "American Society for Biochemistry and Molecular Biology Annual Meeting (Discover BMB)", "biochemistry"),
    ("FEBS", "FEBS Congress (Federation of European Biochemical Societies)", "biochemistry"),
    ("ASCB", "ASCB|EMBO Meeting (Cell Bio)", "cell biology"),

    # --- Statistics --------------------------------------------------------
    ("JSM", "Joint Statistical Meetings", "statistics"),
    ("SDSS", "Symposium on Data Science and Statistics", ("statistics", "data science")),
    ("ENAR", "ENAR Spring Meeting (International Biometric Society)", ("statistics", "biostatistics")),
    ("ICORS", "International Conference on Robust Statistics", "statistics"),

    # --- Computer science --------------------------------------------------
    ("SIGGRAPH", "ACM SIGGRAPH Conference", "computer graphics"),
    ("WSC", "Winter Simulation Conference", "simulation"),
    ("ICSE", "International Conference on Software Engineering", "software engineering"),
    ("POPL", "ACM SIGPLAN Symposium on Principles of Programming Languages", "programming languages"),
    ("STOC", "ACM Symposium on Theory of Computing", "theoretical computer science"),
    ("FOCS", "IEEE Symposium on Foundations of Computer Science", "theoretical computer science"),
    ("SODA", "ACM-SIAM Symposium on Discrete Algorithms", "theoretical computer science"),

    # --- Mathematics -------------------------------------------------------
    ("ICM", "International Congress of Mathematicians", "mathematics"),
    ("JMM", "Joint Mathematics Meetings", "mathematics"),
    ("MATHFEST", "MAA MathFest", "mathematics"),
    ("SIAM", "SIAM Annual Meeting", "applied mathematics"),
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
    "MICCAI": "https://miccai.org",
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
    "ECCV": "https://eccv.ecva.net",
    "AAAI": "https://aaai.org",
    # --- Chemistry ---------------------------------------------------------
    "ACS-CHEM": "https://www.acs.org/meetings/acs-meetings.html",
    "GCE": "https://www.gcande.org",
    "DDC": "https://www.drugdiscoverychemistry.com",
    "PITTCON": "https://pittcon.org",
    # --- Physics -----------------------------------------------------------
    "APS": "https://summit.aps.org",
    "DPG": "https://www.dpg-physik.de/aktivitaeten-und-programme/tagungen/fruehjahrstagungen",
    "ICHEP": "https://ichep2026.org",
    "MORIOND": "https://moriond.in2p3.fr",
    "TSRA": "https://texassymposium.events.asu.edu",
    "CLEO": "https://www.cleoconference.org",
    # --- Biology -----------------------------------------------------------
    "BPS": "https://www.biophysics.org",
    "ASBMB": "https://www.asbmb.org",
    "FEBS": "https://www.febs.org",
    "ASCB": "https://www.ascb.org",
    # --- Statistics --------------------------------------------------------
    "JSM": "https://www.amstat.org",
    "SDSS": "https://ww2.amstat.org/meetings/sdss/",
    "ENAR": "https://www.enar.org",
    "ICORS": "https://icors2026.ankara.edu.tr",
    # --- Computer science --------------------------------------------------
    "SIGGRAPH": "https://www.siggraph.org",
    "WSC": "https://meetings.informs.org/wordpress/wsc2026/",
    "ICSE": "https://conf.researchr.org/home/icse-2027",
    "POPL": "https://popl27.sigplan.org",
    "STOC": "https://acm-stoc.org",
    "FOCS": "https://focs.computer.org",
    "SODA": "https://www.siam.org/conferences-events/siam-conferences/soda27/",
    # --- Mathematics -------------------------------------------------------
    "ICM": "https://www.icm2026.org",
    "JMM": "https://jointmathematicsmeetings.org",
    "MATHFEST": "https://maa.org/event/mathfest/",
    "SIAM": "https://www.siam.org",
}
# CSHL meetings share the official meetings portal (individual meetings have no
# stable per-meeting permalink), so every CSHL-* seed maps to the same landing.
SEED_CONFERENCE_URLS.update(
    {acronym: _CSHL_MEETINGS_URL for acronym, *_ in SEED_CONFERENCES if acronym.startswith("CSHL-")}
)

# Deep meeting links for the flagship series, layered on top of the
# org homepages above. Each entry holds up to three tiers, most specific first:
#   "event"   -- the current/next edition's own page (most specific; can rot when
#                the edition rolls over, so it is re-verified alongside discovery)
#   "meeting" -- a stable annual-meeting landing path the org reuses every year
#   "org"     -- the organization homepage (mirrors SEED_CONFERENCE_URLS)
# A tier is ``None`` when no such page exists (or none could be verified to
# resolve). ``best_seed_url`` collapses an entry to its most specific non-None
# tier; series absent from this map fall back to their SEED_CONFERENCE_URLS
# homepage. Only flagship series are listed, and
# all populated links were verified to return HTTP 200 as of 2026-06-17.
SEED_CONFERENCE_LINKS: dict[str, dict[str, "str | None"]] = {
    # --- Radiology ---------------------------------------------------------
    "RSNA": {"event": None, "meeting": "https://www.rsna.org/annual-meeting", "org": "https://www.rsna.org"},
    "ECR": {"event": None, "meeting": "https://myesr.org/congress/", "org": "https://www.myesr.org"},
    "MICCAI": {"event": "https://conferences.miccai.org/2026/", "meeting": None, "org": "https://miccai.org"},
    # --- Machine learning --------------------------------------------------
    "NeurIPS": {"event": "https://neurips.cc/Conferences/2026", "meeting": "https://neurips.cc/Conferences/FutureMeetings", "org": "https://neurips.cc"},
    "ICML": {"event": "https://icml.cc/Conferences/2026", "meeting": None, "org": "https://icml.cc"},
    "ICLR": {"event": None, "meeting": "https://iclr.cc/Conferences/FutureMeetings", "org": "https://iclr.cc"},
    "CVPR": {"event": None, "meeting": None, "org": "https://cvpr.thecvf.com"},
    "ICCV": {"event": None, "meeting": None, "org": "https://iccv.thecvf.com"},
    "ECCV": {"event": None, "meeting": None, "org": "https://eccv.ecva.net"},
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

# Tier preference used by the link resolvers below -- most specific first.
_SEED_LINK_TIERS = ("event", "meeting", "org")

# Hardcoded submission/presentation formats for specific conferences.
# Maps acronym (case-insensitive) to a list of formats.
# Used as a floor: discovery cannot overwrite these hard-set values.
HARDCODED_FORMATS: dict[str, list[str]] = {
    # CSHL meetings follow the abstract, poster, oral format model uniformly.
    acronym.upper(): ["abstract", "poster", "oral"]
    for acronym, *_ in SEED_CONFERENCES
    if acronym.startswith("CSHL-")
}

# Upper-cased index of SEED_CONFERENCE_LINKS for case-insensitive lookup against
# discovered acronyms, whose casing may differ from the canonical seed keys
# (e.g. a discovery run may report "NEURIPS" rather than "NeurIPS").
_SEED_CONFERENCE_LINKS_BY_UPPER = {k.upper(): v for k, v in SEED_CONFERENCE_LINKS.items()}


def curated_seed_url(acronym: str) -> "str | None":
    """Hand-verified deep link for a flagship series, or ``None`` if not curated.

    Returns the most specific populated tier from :data:`SEED_CONFERENCE_LINKS`
    (``event`` > ``meeting`` > ``org``). Unlike :func:`best_seed_url`, it never
    falls back to a plain :data:`SEED_CONFERENCE_URLS` homepage -- it yields a
    link only for series with a curated entry. Discovery uses it as a *floor* so
    a refresh cannot overwrite a verified flagship link with a weaker
    model-found one. Matching is case-insensitive.
    """
    if not acronym:
        return None
    links = _SEED_CONFERENCE_LINKS_BY_UPPER.get(acronym.upper())
    if links is None:
        return None
    for tier in _SEED_LINK_TIERS:
        if links.get(tier):
            return links[tier]
    return None


def best_seed_url(acronym: str) -> "str | None":
    """Most specific known link for a seed, preferring a deep meeting link.

    Flagship series may carry a tiered link set in :data:`SEED_CONFERENCE_LINKS`
    (an edition-specific ``event`` page, a stable ``meeting`` landing, and the
    ``org`` homepage); this returns the most specific tier that is populated.
    Every other series falls back to its :data:`SEED_CONFERENCE_URLS` homepage.
    """
    return curated_seed_url(acronym) or SEED_CONFERENCE_URLS.get(acronym)


# Curated subcategory tags per seed acronym (case-insensitive), the authoritative
# classification for every seeded series. Discovery is responsible for dates,
# links, and cost -- not for classifying a conference into fields -- so the model
# sometimes returns a descriptive blurb ("general radiology / medical imaging")
# where a clean tag belongs. ``seed_subcategories_for`` exposes the curated tags
# so the write paths can apply them as a *floor* (see ``database.upsert_conferences``
# / ``merge_records``): a refresh can never overwrite a seed's tags with model
# free-text. Editing a seed's subcategory element is the single lever for its
# classification. Built from the seed table, so it stays in sync automatically.
_SEED_SUBCATEGORIES_BY_UPPER: dict[str, list[str]] = {
    acronym.upper(): normalize_subcategories(subcategory)
    for acronym, _, subcategory in SEED_CONFERENCES
}


def seed_subcategories_for(acronym: str) -> "list[str] | None":
    """Curated subcategory tags for a seed acronym, or ``None`` if not a seed.

    Matching is case-insensitive. Returns a fresh list so callers cannot mutate
    the shared table.
    """
    if not acronym:
        return None
    subs = _SEED_SUBCATEGORIES_BY_UPPER.get(acronym.upper())
    return list(subs) if subs else None


def seed_subcategories() -> list[str]:
    """Distinct subcategories present in :data:`SEED_CONFERENCES`, sorted.

    The standing subcategory set is derived from the seed table so that adding a
    new field's flagship seeds is enough to fold it into the daily refresh -- there
    is no second list to keep in sync.
    """
    seen: list[str] = []
    for _, _, subcategory in SEED_CONFERENCES:
        for sub in normalize_subcategories(subcategory):
            if sub not in seen:
                seen.append(sub)
    return sorted(seen)


# Subcategories the scheduled refresh iterates over (one discovery run each).
STANDING_SUBCATEGORIES = seed_subcategories()

# --- Refresh cadence -------------------------------------------------------

# Discovery runs per field (one web-search pass per subcategory), so cadence is
# set per field, not per conference. Fields that carry flagship, fast-moving
# meetings are refreshed weekly; every other field refreshes monthly. Editing this
# set is the single knob for a field's cadence. Names must match seed subcategories.
WEEKLY_SUBCATEGORIES = {
    "radiology",
    "cardiology",
    "oncology",
    "genomics",
    "machine learning",
}


def weekly_subcategories() -> list[str]:
    """Seed subcategories refreshed weekly (intersection with WEEKLY_SUBCATEGORIES)."""
    return sorted(s for s in seed_subcategories() if s in WEEKLY_SUBCATEGORIES)


def monthly_subcategories() -> list[str]:
    """Seed subcategories refreshed monthly (everything not refreshed weekly)."""
    return sorted(s for s in seed_subcategories() if s not in WEEKLY_SUBCATEGORIES)


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
