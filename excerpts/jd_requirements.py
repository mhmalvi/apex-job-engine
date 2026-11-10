"""Deterministic job-description requirement extraction and ATS coverage.

EXCERPT — original module: ``jd_keywords`` (APEX job pipeline, core).
Removed for the showcase: most of the sales-persona lexicon (a trimmed sample
is kept), the persona-resolution module (replaced by a plain ``persona``
argument), skill re-ordering and headline helpers; two private helpers
(``_term_pattern``, ``_mentions``) are exported under public names. The matching, section
detection, weighting and coverage logic is the production code.

No LLM is involved anywhere in this module, by decision: the requirement list
is what the résumé is judged against, so it has to be reproducible,
inspectable and free of hallucinated demands. Two sources feed it:

1. A persona lexicon — the competencies, tools, KPIs, domains and credentials
   a recruiter in that field screens for, with the synonyms an ATS would
   accept.
2. Structure in the posting itself — the title, the requirements /
   qualifications section (terms found there are MUST-HAVE), proper-noun
   tools written in CamelCase or ALLCAPS, and years-of-experience demands.

``coverage()`` then measures how much of that list a rendered résumé actually
contains, weighted so a title term counts more than a passing mention. The
score is stored with the tailored résumé and gates the send.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Iterable, Optional

ENGINEER = "engineer"
SALES = "sales"

# ---------------------------------------------------------------------------
# Lexicons: term -> (category, synonyms)
# ---------------------------------------------------------------------------

#: Trimmed sample of the sales lexicon (the production one has ~70 entries).
SALES_LEXICON: dict[str, tuple[str, tuple[str, ...]]] = {
    "business development": ("skill", ("bd", "biz dev", "business developer")),
    "b2b": ("skill", ("business-to-business", "business to business")),
    "b2c": ("skill", ("business-to-consumer",)),
    "saas": ("domain", ("software as a service", "software-as-a-service")),
    "lead generation": ("skill", ("lead gen", "generating leads", "demand generation")),
    "pipeline": ("skill", ("sales pipeline", "pipeline management", "funnel")),
    "quota": ("kpi", ("sales target", "sales targets", "revenue target", "targets")),
    "negotiation": ("skill", ("negotiate", "negotiating", "negotiations")),
    "crm": ("tool", ("crm software", "crm system", "crm systems")),
    "salesforce": ("tool", ("sfdc",)),
    "hubspot": ("tool", ()),
    "excel": ("tool", ("ms excel", "spreadsheets", "google sheets")),
    "erp": ("domain", ("enterprise software", "management systems")),
    "healthcare": ("domain", ("health", "hospital", "medical", "pharma")),
    "fintech": ("domain", ("financial services", "banking", "payments")),
    "bachelor's degree": ("credential", ("bachelor", "bachelors", "bsc", "b.sc", "bba")),
}

ENGINEER_LEXICON: dict[str, tuple[str, tuple[str, ...]]] = {
    "python": ("tool", ()), "javascript": ("tool", ("js",)), "typescript": ("tool", ("ts",)),
    "java": ("tool", ()), "c++": ("tool", ()), "golang": ("tool", ()), "rust": ("tool", ()),
    "ruby": ("tool", ()), "react": ("tool", ("react.js", "reactjs")), "angular": ("tool", ()),
    "vue": ("tool", ("vue.js",)), "node.js": ("tool", ("node", "nodejs")), "django": ("tool", ()),
    "flask": ("tool", ()), "fastapi": ("tool", ()), "aws": ("tool", ("amazon web services",)),
    "azure": ("tool", ()), "gcp": ("tool", ("google cloud",)), "docker": ("tool", ()),
    "kubernetes": ("tool", ("k8s",)), "terraform": ("tool", ()),
    "machine learning": ("skill", ("ml",)), "deep learning": ("skill", ()),
    "nlp": ("skill", ("natural language processing",)), "llm": ("skill", ("large language model", "llms")),
    "ai": ("skill", ("artificial intelligence",)), "pytorch": ("tool", ()), "tensorflow": ("tool", ()),
    "langchain": ("tool", ()), "rag": ("skill", ("retrieval-augmented",)), "sql": ("tool", ()),
    "postgresql": ("tool", ("postgres",)), "mongodb": ("tool", ()), "redis": ("tool", ()),
    "kafka": ("tool", ()), "microservices": ("skill", ()), "rest api": ("skill", ("rest", "restful")),
    "graphql": ("tool", ()), "ci/cd": ("skill", ("cicd", "continuous integration")),
    "agile": ("skill", ("scrum",)), "leadership": ("skill", ()), "mentoring": ("skill", ()),
    "data pipeline": ("skill", ("etl", "data engineering")),
}

LEXICONS = {SALES: SALES_LEXICON, ENGINEER: ENGINEER_LEXICON}

#: Headers that open a section whose bullet points are requirements. Terms
#: found under one of these are marked must-have.
_REQ_HEADER_RE = re.compile(
    r"^\W*(?:(?:key |minimum |basic |essential |required |preferred |desired )?"
    r"(?:requirements?|qualifications?|skills?(?: and| &)? (?:experience|qualifications?|abilities))|"
    r"what (?:you|we)(?:'ll| will| are)?\s*(?:need|bring|looking for|expect)|"
    r"must[- ]haves?|who you are|about you|ideal candidate|candidate profile|"
    r"educational requirements?|experience requirements?|additional requirements?)\b",
    re.I)

#: Headers that END a requirements section (anything that is not one).
_OTHER_HEADER_RE = re.compile(
    r"^\W*(?:responsibilities|duties|what you(?:'ll| will) do|about (?:us|the company|the role)|"
    r"benefits|compensation|salary|perks|why join|how to apply|job context|job description|"
    r"employment status|workplace|job location|company information|read before apply)\b", re.I)

#: Tokens that look like proper-noun tools but are not.
_PROPER_NOUN_STOP = frozenset("""
AND OR THE FOR NOT WITH JOB APPLY ROLE TEAM LTD LLC INC PLC CEO CTO COO CFO HR
KPI KPIS USA UK UAE BD BDT USD EUR AUD PM AM UTC GMT IT
""".split())

_YEARS_RE = re.compile(
    r"(?:minimum\s+(?:of\s+)?|at least\s+)?(\d{1,2})\s*(?:\+|-\s*\d{1,2})?\s*(?:years?|yrs?)"
    r"(?:\s+of)?\s*(?:\w+\s+){0,4}?(?:experience|exp\b)", re.I)

#: Title noise: seniority markers and filler that do not describe the function.
_TITLE_NOISE = frozenset("""
senior sr junior jr lead head of the a an and & manager executive officer
assistant associate deputy chief principal staff mid level i ii iii iv
""".split())


@dataclass
class Requirement:
    """One thing the posting asks for.

    Attributes:
        term: Canonical lowercase term.
        category: title | skill | tool | domain | kpi | credential | years.
        weight: Importance for scoring (title 3, must-have 2, mention 1,
            plus a small frequency bonus).
        must_have: True when found in a requirements section or the title.
        synonyms: Alternate spellings that count as a match.
    """
    term: str
    category: str
    weight: float
    must_have: bool = False
    synonyms: tuple[str, ...] = ()


@dataclass
class Requirements:
    """Everything extracted from one posting."""
    title_terms: list[str] = field(default_factory=list)
    items: list[Requirement] = field(default_factory=list)
    core_title: str = ""

    def terms(self) -> list[str]:
        """The requirement terms, highest weight first."""
        return [r.term for r in self.items]

    def must_haves(self) -> list[str]:
        """Only the terms the posting treats as mandatory."""
        return [r.term for r in self.items if r.must_have]


@dataclass
class Coverage:
    """How much of a posting's requirement list a résumé contains."""
    score: float
    matched: list[str]
    missed: list[str]
    must_missed: list[str]

    def summary(self) -> str:
        """One line for logs and the review digest."""
        miss = ", ".join(self.must_missed[:6]) or "none"
        return (f"ATS coverage {self.score:.0%} ({len(self.matched)}/"
                f"{len(self.matched) + len(self.missed)}); must-have gaps: {miss}")


# ---------------------------------------------------------------------------
# Matching primitives
# ---------------------------------------------------------------------------

def term_pattern(term: str) -> re.Pattern:
    """Word-boundary regex for a term, tolerant of plural 's' and hyphen/space swaps.

    Args:
        term: Lowercase term, may contain spaces, hyphens, dots, '+' or '/'.
    Returns:
        A compiled case-insensitive pattern.
    """
    parts = [re.escape(p) for p in re.split(r"[\s\-]+", term.strip()) if p]
    body = r"[\s\-]*".join(parts)
    return re.compile(r"(?<![a-z0-9])" + body + r"(?:s|es)?(?![a-z0-9])", re.I)


def mentions(term: str, synonyms: Iterable[str], text: str) -> int:
    """Count how often a term or any synonym appears in text."""
    return sum(len(term_pattern(t).findall(text)) for t in (term, *synonyms))


# ---------------------------------------------------------------------------
# Posting structure
# ---------------------------------------------------------------------------

def requirement_section(job_desc: str) -> str:
    """The text under requirement-style headers, joined.

    Args:
        job_desc: Raw posting text.
    Returns:
        The lines that sit under a requirements header until the next
        non-requirement header; empty when no such header exists.
    """
    kept: list[str] = []
    inside = False
    for line in job_desc.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if len(stripped) <= 80 and _REQ_HEADER_RE.match(stripped):
            inside = True
            kept.append(stripped)
            continue
        if len(stripped) <= 80 and _OTHER_HEADER_RE.match(stripped):
            inside = False
            continue
        if inside:
            kept.append(stripped)
    return "\n".join(kept)


def clean_title(job_title: str) -> str:
    """Strip company, location, bracketed notes and trailing qualifiers from a title.

    "Backend Engineer – Platform (Remote)" becomes "Backend Engineer".
    """
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", job_title or "")
    segments = re.split(r"\s+[–—|:]\s+|\s+-\s+|,|\s+@\s+|\s+at\s+", t, maxsplit=1)
    lead = re.sub(r"\s+", " ", segments[0]).strip(" -–—|")
    # "Manager – Sales Operations": the lead segment is only a rank word and
    # the function sits after the dash. Reattach it as "<function> <rank>".
    lead_words = [w for w in lead.lower().split() if w not in _TITLE_NOISE]
    if not lead_words and len(segments) > 1:
        tail = re.sub(r"\s+", " ", segments[1]).strip(" -–—|")
        if tail:
            lead = f"{tail} {lead}"
    return lead


def _title_terms(job_title: str) -> list[str]:
    """Function words of a title, minus seniority and filler."""
    words = re.findall(r"[a-z][a-z\-]+", clean_title(job_title).lower())
    return [w for w in words if w not in _TITLE_NOISE]


def _proper_noun_tools(job_desc: str, company: str = "") -> set[str]:
    """Tool names written as CamelCase or ALLCAPS, e.g. HubSpot, MEDDIC, SaaS.

    Sentence-initial capitalised words are excluded by requiring an internal
    capital or an all-caps run of 3-8 letters. Hashtags and the employer's own
    name are not tools and are skipped.
    """
    found = set()
    company_tokens = {t.lower() for t in re.findall(r"[A-Za-z0-9]+", company or "")}
    text = re.sub(r"#\w+", " ", job_desc or "")
    for tok in re.findall(r"\b[A-Za-z][A-Za-z0-9.+]{2,}\b", text):
        if tok.upper() in _PROPER_NOUN_STOP or tok.lower() in company_tokens:
            continue
        internal_cap = re.search(r"[a-z][A-Z]", tok) is not None
        all_caps = re.fullmatch(r"[A-Z]{3,8}", tok) is not None
        if internal_cap or all_caps:
            found.add(tok.lower())
    return found


def _years_required(text: str) -> Optional[str]:
    """The years-of-experience demand as "N+ years", if the posting states one."""
    m = _YEARS_RE.search(text or "")
    return f"{m.group(1)}+ years" if m else None


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------

def extract_requirements(job_desc: str, job_title: str,
                         persona: str = ENGINEER, company: str = "") -> Requirements:
    """Build the weighted requirement list for a posting, deterministically.

    Args:
        job_desc: Raw posting text (any length; not truncated).
        job_title: Scraped title.
        persona: Lexicon to read the posting with.
        company: Employer name; its tokens are never treated as tools.
    Returns:
        Requirements, items sorted by weight descending.
    """
    lexicon = LEXICONS.get(persona, ENGINEER_LEXICON)
    text = (job_desc or "").lower()
    req_text = requirement_section(job_desc or "").lower()
    title_terms = _title_terms(job_title)
    title_lower = clean_title(job_title).lower()
    items: dict[str, Requirement] = {}

    def add(term, category, weight, must, syn=()):
        prev = items.get(term)
        if prev is None or prev.weight < weight:
            items[term] = Requirement(term, category, round(weight, 2),
                                      must or (prev.must_have if prev else False), tuple(syn))
        elif must:
            prev.must_have = True

    for term, (category, synonyms) in lexicon.items():
        in_title = mentions(term, synonyms, title_lower) > 0
        count = mentions(term, synonyms, text)
        if not (in_title or count):
            continue
        in_req = mentions(term, synonyms, req_text) > 0
        weight = 3.0 if in_title else (2.0 if in_req else 1.0)
        weight += min(count, 4) * 0.25
        add(term, "title" if in_title else category, weight, in_title or in_req, synonyms)

    for tool in _proper_noun_tools(job_desc or "", company):
        if tool in items or any(tool in syn for _, (_, syn) in lexicon.items()):
            continue
        # Weighted up inside a requirements section, but never must-have: an
        # ALLCAPS token there is as likely an org name as a tool.
        in_req = mentions(tool, (), req_text) > 0
        add(tool, "tool", 1.5 if in_req else 1.0, False)

    years = _years_required(req_text or text)
    if years:
        add(years, "years", 1.5, True)

    # The title's own function words are requirements too, so that a
    # "Platform Engineer" résumé is expected to say "platform".
    for word in title_terms:
        if len(word) >= 4 and word not in items and not any(
                word in r.term for r in items.values()):
            add(word, "title", 2.5, True)

    ordered = sorted(items.values(), key=lambda r: (-r.weight, r.term))
    return Requirements(title_terms=title_terms, items=ordered,
                        core_title=clean_title(job_title))


# ---------------------------------------------------------------------------
# Coverage
# ---------------------------------------------------------------------------

def resume_plain_text(resume: dict) -> str:
    """Everything a rendered résumé shows, as one lowercase string."""
    parts = [str((resume.get("contact") or {}).get("title") or ""),
             str(resume.get("summary") or "")]
    for exp in resume.get("experience") or []:
        parts.append(str(exp.get("title") or ""))
        parts.extend(str(b) for b in (exp.get("bullets") or []))
    for proj in resume.get("projects") or []:
        parts.append(f"{proj.get('name') or ''} {proj.get('description') or ''}")
    skills = resume.get("skills") or {}
    groups = skills.values() if isinstance(skills, dict) else [skills]
    for group in groups:
        parts.extend(str(x) for x in (group or []))
    for edu in resume.get("education") or []:
        parts.append(f"{edu.get('degree') or ''} {edu.get('institution') or ''}")
    return "\n".join(parts).lower()


def coverage(reqs: Requirements, resume_text: str) -> Coverage:
    """Score a résumé against a posting's requirement list.

    Args:
        reqs: Output of extract_requirements.
        resume_text: Output of resume_plain_text (or any lowercase text).
    Returns:
        Coverage with the weighted score and the matched/missed terms.
    """
    matched, missed, must_missed = [], [], []
    got = total = 0.0
    for r in reqs.items:
        total += r.weight
        if r.category == "years" or mentions(r.term, r.synonyms, resume_text):
            matched.append(r.term)
            got += r.weight
        else:
            missed.append(r.term)
            if r.must_have:
                must_missed.append(r.term)
    score = (got / total) if total else 1.0
    return Coverage(round(score, 3), matched, missed, must_missed)
