"""Provenance guard + guarded tailoring loop for per-job résumés.

EXCERPT — original module: ``resume_tailor`` (APEX job pipeline, core).
Kept: the provenance rules (``provenance_violations``, ``enforce_provenance``
and their helpers) with the production logic unchanged, and the shape of the tailoring loop.
Removed: the prompt text, the LLM routing / circuit breaker / usage logging,
the per-job cache, PDF rendering and page fitting. In ``tailor_with_guard``
the model call is injected as ``draft_fn`` so the loop can run without one,
and the draft is merged over the base with ``{**base, **draft}`` instead of
the production ``_assemble``. Leading underscores were dropped from helper names.

The prompt tells the model never to invent a number or a tool. The model does
not always obey its own prompt, so this module checks the draft against the
base résumé deterministically:

  * a NUMBER that appears nowhere in the base (a fabricated or "rounded" metric);
  * a RESULTS WORD (quota / target / attainment / exceeded ...) the base never uses;
  * a TOOL, DOMAIN, CREDENTIAL or ACRONYM the base never mentions.

Offending bullets are swapped back to the base bullet at the same position,
the model is asked again with the violations named, and the best draft wins
on (genuinely tailored, clean, coverage) in that order.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

import jd_requirements as jd

_NUMBER_RE = re.compile(r"\$?\d[\d,]*(?:\.\d+)?\s*(?:%|x|\+|k|m|bn)?", re.I)

#: Words that assert a result the base résumé may not evidence. They are
#: allowed only when the base already uses them.
CLAIM_WORDS = ("quota", "target", "targets", "attainment", "exceeded",
               "overachieved", "over-achieved", "surpassed", "outperformed")

#: Acronyms and product names as they appear in prose: ERP, HIMS, SaaS, B2C.
_ACRONYM_RE = re.compile(r"\b(?:[A-Z][A-Za-z0-9]*[A-Z0-9][A-Za-z0-9]*|[A-Z]{2,})\b")


def numeric_tokens(text: str) -> set[str]:
    """Every number-like token in text, normalised (commas/spaces removed)."""
    return {re.sub(r"[,\s]", "", m).lower() for m in _NUMBER_RE.findall(text or "")}


def prose_of(resume: dict) -> str:
    """The rewritable prose of a résumé: summary plus every bullet."""
    parts = [str(resume.get("summary") or "")]
    for exp in resume.get("experience") or []:
        parts.extend(str(b) for b in (exp.get("bullets") or []))
    return "\n".join(parts)


def allowed_numbers(base: dict) -> set[str]:
    """Numbers the base résumé vouches for, including dates.

    Also the bare digits of every token, so "85%" in the base licenses "85"
    in a rewrite and vice versa.
    """
    text = prose_of(base)
    for exp in base.get("experience") or []:
        text += " " + str(exp.get("dates") or "")
    for edu in base.get("education") or []:
        text += " " + str(edu.get("dates") or "")
    tokens = numeric_tokens(text)
    return tokens | {re.sub(r"[^\d.]", "", t) for t in tokens}


def vocabulary(resume_data: dict) -> set:
    """Every word a rendered résumé would show, lowercased."""
    parts = [str(resume_data.get("summary") or "")]
    for exp in resume_data.get("experience") or []:
        parts.append(str(exp.get("title") or ""))
        parts.extend(str(b) for b in (exp.get("bullets") or []))
    for proj in resume_data.get("projects") or []:
        parts.append(str(proj.get("name") or ""))
        parts.append(str(proj.get("description") or ""))
    skills = resume_data.get("skills") or {}
    if isinstance(skills, dict):
        for group in skills.values():
            parts.extend(str(x) for x in (group or []))
    else:
        parts.extend(str(x) for x in skills)
    # Internal dots only. A trailing sentence period must not become part of
    # the word, or reordering the base's own sentences reads as new vocabulary
    # and every shuffle passes as tailoring.
    return set(re.findall(r"[a-z][a-z+#]*(?:\.[a-z+#]+)*",
                          " ".join(parts).lower()))


def is_tailored(tailored: dict, base: dict) -> bool:
    """Whether this résumé carries anything the base does not.

    Set difference rather than a text diff on purpose: reordering the base's
    own sentences changes the text and customises nothing, and that is the
    cheap way for a tailoring step to look like it worked.
    """
    return bool(vocabulary(tailored) - vocabulary(base))


def inserted_terms(text: str, base_text: str) -> list[str]:
    """Tools, domains, credentials or acronyms in text that the base never mentions.

    A posting that asks for ERP tempts the model to write "selling ERP
    solutions" even when the base never mentions ERP. Any such term the base
    does not contain (or a synonym of it) is an insertion.
    """
    offending: list[str] = []
    licensed: set[str] = set()  # spellings of terms the base genuinely holds
    for term, (category, synonyms) in jd.SALES_LEXICON.items():
        if category not in ("tool", "domain", "credential"):
            continue
        if jd.mentions(term, synonyms, base_text):
            licensed.update((term, *synonyms))
        elif jd.mentions(term, synonyms, text.lower()):
            offending.append(term)
    for tok in set(_ACRONYM_RE.findall(text)):
        low = tok.lower()
        if low in licensed:
            continue
        if not re.search(r"(?<![a-z0-9])" + re.escape(low) + r"(?![a-z0-9])", base_text):
            offending.append(low)
    return sorted(set(offending))


def provenance_violations(tailored: dict, base: dict) -> list[str]:
    """Claims in the tailored prose that the base résumé does not back.

    Returns:
        Human-readable violation strings, empty when clean.
    """
    allowed = allowed_numbers(base)
    base_vocab = vocabulary(base)
    base_text = jd.resume_plain_text(base)
    found: list[str] = []
    for tok in numeric_tokens(prose_of(tailored)):
        bare = re.sub(r"[^\d.]", "", tok)
        if tok not in allowed and bare not in allowed and bare:
            found.append(f"number {tok!r} not in base")
    prose_words = set(re.findall(r"[a-z][a-z\-]+", prose_of(tailored).lower()))
    for word in CLAIM_WORDS:
        if word in prose_words and word not in base_vocab:
            found.append(f"claim word {word!r} not in base")
    for term in inserted_terms(prose_of(tailored), base_text):
        found.append(f"term {term!r} not in base")
    return found


def bullet_is_clean(bullet: str, allowed: set[str], base_vocab: set,
                    base_text: str = "") -> bool:
    """Whether one bullet passes the provenance rules."""
    for tok in numeric_tokens(bullet):
        bare = re.sub(r"[^\d.]", "", tok)
        if bare and tok not in allowed and bare not in allowed:
            return False
    words = set(re.findall(r"[a-z][a-z\-]+", bullet.lower()))
    if any(w in words and w not in base_vocab for w in CLAIM_WORDS):
        return False
    return not (base_text and inserted_terms(bullet, base_text))


def enforce_provenance(tailored: dict, base: dict) -> tuple[dict, list[str]]:
    """Strip unbacked claims rather than send them.

    A bullet carrying an unlicensed number, claim word or term is replaced by
    the base's bullet at that position when one exists, else dropped; a
    summary that violates reverts to the base summary. The result is always
    at least as truthful as the base.

    Returns:
        (cleaned content, list of violations that were removed).
    """
    violations = provenance_violations(tailored, base)
    if not violations:
        return tailored, []
    allowed = allowed_numbers(base)
    base_vocab = vocabulary(base)
    base_text = jd.resume_plain_text(base)
    cleaned = dict(tailored)
    if not bullet_is_clean(str(tailored.get("summary") or ""), allowed, base_vocab, base_text):
        cleaned["summary"] = base.get("summary", "")
    base_exp = base.get("experience") or []
    new_exp = []
    for i, exp in enumerate(tailored.get("experience") or []):
        exp = dict(exp)
        base_bullets = list((base_exp[i].get("bullets") or []) if i < len(base_exp) else [])
        kept = []
        for j, b in enumerate(exp.get("bullets") or []):
            if bullet_is_clean(str(b), allowed, base_vocab, base_text):
                kept.append(b)
            elif j < len(base_bullets) and base_bullets[j] not in kept:
                kept.append(base_bullets[j])
        exp["bullets"] = kept or base_bullets[:2]
        new_exp.append(exp)
    cleaned["experience"] = new_exp
    return cleaned, violations


#: Production defaults (env-overridable there).
COVERAGE_FLOOR = 0.65
MAX_PASSES = 2

#: draft_fn(base, job, reqs, missed_terms, violations) -> draft dict or None
DraftFn = Callable[[dict, dict, jd.Requirements, Optional[list], Optional[list]], Optional[dict]]


def tailor_with_guard(base: dict, job: dict, draft_fn: DraftFn, *,
                      floor: float = COVERAGE_FLOOR,
                      max_passes: int = MAX_PASSES) -> tuple[dict, dict]:
    """Requirements -> draft -> strip unbacked claims -> coverage gate -> re-ask.

    Args:
        base: The base résumé content.
        job: ``{"title", "company", "description"}``.
        draft_fn: The model call. Receives the gaps and violations from the
            previous pass so the retry can name them.
        floor: Coverage a clean draft must reach to stop early.
        max_passes: Upper bound on model calls.
    Returns:
        (chosen content, report). Falls back to the base résumé when no draft
        came back; the caller refuses to render the base as "tailored".
    """
    reqs = jd.extract_requirements(job.get("description", ""), job.get("title", ""),
                                   company=job.get("company", ""))
    best: Optional[dict] = None
    best_cov = None
    best_rank: tuple = (False, False, -1.0)
    all_violations: list[str] = []
    missed: Optional[list[str]] = None
    last_violations: list[str] = []
    passes = 0
    for _ in range(max_passes):
        parsed = draft_fn(base, job, reqs, missed, last_violations or None)
        passes += 1
        if not parsed:
            break
        candidate, violations = enforce_provenance({**base, **parsed}, base)
        all_violations.extend(violations)
        cov = jd.coverage(reqs, jd.resume_plain_text(candidate))
        # Rank: genuinely tailored beats reverted-to-base, clean beats
        # stripped, then coverage. A stripped draft can out-score a clean one
        # on coverage while its prose is the base's.
        rank = (is_tailored(candidate, base), not violations, cov.score)
        if best_cov is None or rank > best_rank:
            best, best_cov, best_rank = candidate, cov, rank
        # A clean draft above the floor is done. A draft that needed stripping
        # is retried even above the floor: stripping reverts prose to the base,
        # and the retry can say the same thing truthfully.
        if cov.score >= floor and not violations:
            break
        missed = cov.must_missed + [t for t in cov.missed if t not in cov.must_missed]
        last_violations = violations
    if best is None or best_cov is None:
        return base, {"passes": passes, "fallback": "base"}
    return best, {"passes": passes, "coverage": best_cov.score,
                  "must_missed": best_cov.must_missed, "violations": all_violations}
