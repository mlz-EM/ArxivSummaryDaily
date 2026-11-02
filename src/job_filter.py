"""Conservative exclusions for the materials/physical-science faculty search.

Unknown fields pass. A missing materials keyword is never an exclusion.
Only explicit unrelated fields in the job title are screened locally; strong
materials evidence keeps interdisciplinary searches available for LLM review.
"""

import re


_POTENTIAL_FIELD = re.compile(
    r"\b(engineering|engineer|materials?|physics|biophysics|chemistry|chemical|"
    r"mechanics|manufacturing|metallurgy|semiconductors?|quantum|nanoscience|"
    r"nanotechnology|photonics|optics|polymers?|energy|geophysics|geoscience|"
    r"earth science|computer science|computing|computational|data science|artificial intelligence|"
    r"mathematics|applied math|statistics)\b", re.I,
)
_MATERIALS_EVIDENCE = re.compile(
    r"\b(materials? science|materials? engineering|materials? characterization|"
    r"electron microscopy of materials|condensed matter|solid[ -]state|"
    r"semiconductor\w*|nanomaterials?|biomaterials?|metallurg\w*|"
    r"electrochemi\w*|soft matter|programmable materials|quantum materials|"
    r"structure[ -]property|mechanical properties|polymer\w*|photonic\w*)\b", re.I,
)
_UNRELATED_FIELDS = {
    "clinical_or_health": r"\b(nursing|dentistry|dental|pediatrics?|surgery|radiology|"
        r"anesthesiolog\w*|obstetrics|gynecology|cardiolog\w*|psychiatry|"
        r"dermatolog\w*|ophthalmolog\w*|otolaryngolog\w*|urolog\w*|"
        r"clinical medicine|internal medicine|family medicine|hematolog\w*|rheumatolog\w*|"
        r"gastroenterolog\w*|endocrinolog\w*|pulmonary medicine|infectious diseases?|public health|"
        r"social work|speech[ -]language|occupational therapy|physical therapy)\b",
    "life_sciences": r"\b(biology|biological sciences?|biochemistry|microbiolog\w*|"
        r"neuroscien\w*|physiolog\w*|immunolog\w*|genetics?|genomics?|"
        r"oncolog\w*|cancer|pharmacolog\w*)\b",
    "humanities_or_social_science": r"\b(anthropolog\w*|sociolog\w*|psychology|"
        r"political science|criminal justice|criminolog\w*|literature|"
        r"english|anglophone|poetry|spanish|french|history|philosophy|theology|religious studies|"
        r"music|theat[er]{2}|dance|journalism|linguistics?|fine arts|art history|"
        r"elementary education|special education|educational leadership|counselor education)\b",
    "business_or_law": r"\b(accounting|finance|marketing|economics|business administration|law)\b",
}
_UNRELATED_FIELDS = {reason: re.compile(pattern, re.I) for reason, pattern in _UNRELATED_FIELDS.items()}


def faculty_role_exclusion(title):
    """Reject explicit appointment types, preserving endowed research chairs."""
    if re.search(
        r"\b(adjunct|visiting|temporary|lecturer|instructor|post[ -]?doc\w*|fellowship)\b|"
        r"\bnon[ -]+tenure[ -]+track\b|\b(teaching|clinical) professor\b|"
        r"\bprofessor (of|in) (the )?(professional )?practice\b|"
        r"\bprofessional[ -]track\b", str(title or ""), re.I,
    ):
        return "non_tenure_track"
    if re.search(r"\b(dean|department chair|department head|chair of (the )?department)\b", str(title or ""), re.I):
        return "administrative"
    return ""


def profile_exclusion_reason(job):
    title = str(job.get("title") or "")
    role_reason = faculty_role_exclusion(title)
    if role_reason:
        return role_reason
    if _POTENTIAL_FIELD.search(title) or _MATERIALS_EVIDENCE.search(str(job.get("description") or "")):
        return ""
    for reason, pattern in _UNRELATED_FIELDS.items():
        if pattern.search(title):
            return reason
    return ""
