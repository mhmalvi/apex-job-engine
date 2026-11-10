"""Pluggable discovery sources: the interface, one example source, the parallel runner.

EXCERPT — original modules: ``sources/base.py``, ``sources/__init__.py`` and
``sources/remotive.py`` (APEX job pipeline, core).
Kept: ``JobSource``, ``term_matches``, ``clean_text``, the per-source query
de-duplication, the per-source time budget and the parallel fan-out, as in
production. Removed: the real ``make_job`` normaliser (it reuses discovery's
URL validation, location normalisation, apply-mode detection, skill
extraction and deadline mining — replaced here by a minimal stand-in), the
shared HTTP helper (``fetch_json`` is injected into the example source) and
the concrete registry of providers.

Every source returns rows in the pipeline's normalised job schema, merged
into the main scraper's results *before* dedup — so cross-source dedup,
enrichment, scoring and insert apply equally to all of them.

Design notes:
- Sources are best-effort: any HTTP/parse/quota failure returns [] and is
  logged, never raised, so one bad source can't break a discovery run.
- API keys come from the environment only; a key-gated source self-disables
  (``enabled() -> False``) until its key is present.
"""
from __future__ import annotations

import logging
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Callable, Optional

logger = logging.getLogger(__name__)

#: How long one source may keep STARTING new queries; past it the source
#: returns the rows it has.
EXTRA_SOURCE_BUDGET_SECONDS = 150
#: How long discovery waits for all extra sources together: the budget plus
#: one in-flight request (20 s HTTP timeout) plus slack. Before the per-source
#: budget existed this was a flat cap, and a source still running then lost
#: EVERY row it had already fetched.
EXTRA_SOURCES_TIMEOUT_SECONDS = 175


def clean_text(value: Optional[str], max_len: int = 8000) -> str:
    """Strip HTML tags + collapse whitespace from a (possibly HTML) string."""
    if not value:
        return ""
    text = re.sub(r"<script[^>]*>.*?</script>", " ", str(value), flags=re.DOTALL | re.I)
    text = re.sub(r"<style[^>]*>.*?</style>", " ", text, flags=re.DOTALL | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"&nbsp;", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len]


def make_job(*, title: str, url: str, company: str = "", location: str = "",
             description: str = "", is_remote: bool = False, source: str = "",
             posted_date: str = "") -> Optional[dict]:
    """Minimal stand-in for the production normaliser.

    Returns None when the row is unusable (no http(s) URL or no title) so
    callers can simply filter falsy results.
    """
    url = (url or "").strip()
    title = (title or "").strip()
    if not title or not re.match(r"https?://", url):
        return None
    return {"url": url, "title": title, "company": (company or "").strip(),
            "location": (location or "").strip(), "description": clean_text(description),
            "is_remote": bool(is_remote), "source": source, "posted_date": posted_date}


def term_matches(term: str, *fields: str) -> bool:
    """True if any significant token of ``term`` appears in any field.

    Client-side filter for sources whose API has no server-side search
    (whole-board feeds). Loose by design: the scorer is the real relevance gate.
    """
    tokens = [t for t in re.split(r"\s+", (term or "").lower()) if len(t) > 2]
    if not tokens:
        return True
    haystack = " ".join(f for f in fields if f).lower()
    return any(tok in haystack for tok in tokens)


class JobSource:
    """Base class for a pluggable job source.

    Subclasses set ``name`` and implement ``fetch``. ``enabled`` gates the
    source on env flags / key presence so a disabled source costs nothing.
    ``location_aware = False`` marks a source that ignores location, so it is
    sent each search term once instead of once per market.
    """

    name = "base"
    location_aware = True

    def enabled(self) -> bool:
        """Return True if this source should run (override per source)."""
        return True

    def fetch(self, term: str, location: str, is_remote: bool,
              hours_old: int, limit: int) -> list[dict]:
        """Return normalised job dicts for one (term, location) query.

        Must be best-effort: catch its own errors and return [] on failure.
        """
        raise NotImplementedError


class Remotive(JobSource):
    """Example provider: a free, no-key remote-jobs API with server-side search."""

    name = "remotive"
    ENDPOINT = "https://remotive.com/api/remote-jobs"

    def __init__(self, fetch_json: Callable[..., Optional[dict]]):
        self._fetch_json = fetch_json

    def enabled(self) -> bool:
        return os.getenv("APEX_SOURCE_REMOTIVE", "1") != "0"

    def fetch(self, term, location, is_remote, hours_old, limit) -> list[dict]:
        data = self._fetch_json(self.ENDPOINT, params={"search": term, "limit": limit})
        jobs: list[dict] = []
        for e in (data or {}).get("jobs", [])[:limit]:
            job = make_job(
                title=e.get("title", ""), company=e.get("company_name", ""),
                url=e.get("url", ""),
                location=e.get("candidate_required_location") or "Remote",
                description=e.get("description", ""), is_remote=True,
                posted_date=e.get("publication_date", ""), source=self.name,
            )
            if job:
                jobs.append(job)
        return jobs


def load_enabled_sources(classes: list[Callable[[], JobSource]]) -> list[JobSource]:
    """Instantiate every source and return only those currently enabled."""
    sources: list[JobSource] = []
    for cls in classes:
        try:
            src = cls()
            if src.enabled():
                sources.append(src)
        except Exception:  # noqa: BLE001 — a broken source must not block discovery
            logger.warning("source %r failed to init", cls, exc_info=True)
    return sources


def queries_for(source: JobSource, queries: list[dict]) -> list[dict]:
    """The queries worth sending to this source (each term once if location-blind)."""
    if getattr(source, "location_aware", True):
        return queries
    seen, out = set(), []
    for q in queries:
        term = (q.get("term") or "").strip().lower()
        if term and term not in seen:
            seen.add(term)
            out.append(q)
    return out


def run_source(source: JobSource, queries: list[dict], hours_old: int, limit: int,
               deadline: Optional[float] = None,
               clock: Callable[[], float] = time.monotonic) -> list[dict]:
    """Run one source across its queries (sequentially) and dedup its own URLs.

    Args:
        deadline: ``clock()`` value after which no new query starts; the rows
            fetched so far are returned. None = no deadline.
    """
    seen, out = set(), []
    todo = queries_for(source, queries)
    done = 0
    for q in todo:
        if deadline is not None and clock() >= deadline:
            logger.warning("source %s hit its budget after %d/%d queries — keeping %d rows",
                           source.name, done, len(todo), len(out))
            break
        term = (q.get("term") or "").strip()
        if not term:
            continue
        done += 1
        try:
            rows = source.fetch(
                term=term, location=q.get("location", "") or "",
                is_remote=bool(q.get("is_remote")), hours_old=hours_old, limit=limit,
            ) or []
        except Exception:  # noqa: BLE001 — best-effort per source
            logger.warning("source %s fetch failed for %r", source.name, term, exc_info=True)
            rows = []
        for r in rows:
            url = r.get("url")
            if url and url not in seen:
                seen.add(url)
                out.append(r)
    logger.info("source %s -> %d jobs", source.name, len(out))
    return out


def fetch_from_sources(sources: list[JobSource], queries: list[dict],
                       hours_old: int, limit: int) -> list[dict]:
    """Fetch normalised jobs from every enabled source, sources in parallel.

    Returns:
        A flat list of job dicts (best-effort; never raises).
    """
    if not sources:
        return []
    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=len(sources)) as ex:
        deadline = time.monotonic() + EXTRA_SOURCE_BUDGET_SECONDS
        futs = {ex.submit(run_source, s, queries, hours_old, limit, deadline): s
                for s in sources}
        try:
            for fut in as_completed(futs, timeout=EXTRA_SOURCES_TIMEOUT_SECONDS):
                try:
                    results.extend(fut.result(timeout=120) or [])
                except Exception:  # noqa: BLE001
                    logger.warning("source %s crashed", futs[fut].name, exc_info=True)
        except TimeoutError:
            logger.warning("extra sources timed out — returning partial results")
    return results
