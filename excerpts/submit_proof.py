"""Decide whether an employer actually received an application.

EXCERPT — original module: ``submit_proof`` (APEX browser apply lane).
Kept: the verdict ladder (``read_verdict``), the landing-refusal rules and the
ten-second watch (``confirm``), verbatim apart from comments. Removed: incident
notes naming specific employers and job IDs, and a trimmed-down copy of the
in-page ``READING_JS`` probe (its contract is described below).

WHY THIS EXISTS

The runner used to record a job as ``submitted`` on the strength of the API
granting the extension's request to press Submit. A grant means one thing
only: the server permitted a click. An audit of those rows found most of them
had landed on a login wall or on the site's own job-search page — nothing had
reached an employer. So a click is not a submission. Something has to look at
the page afterwards and say what it sees, and that is this module.

THE LADDER (strongest first)

  1. the path CHANGED to a confirmation-shaped path -> confirmed
  2. the page carries confirmation copy that was NOT there before the click
     -> confirmed
  3. validation errors are showing, or the landing proves the press was a
     job search / an apply opener / a job-alert box -> rejected
  4. fields that existed BEFORE the click are gone -> evidence, never proof
     (a login wall has no form belonging to us either)
  5. otherwise nothing has happened yet

The page reading is produced by a JS probe evaluated over CDP in the driven
tab. It returns ``{href, path, text, errors, error_texts, forms, fields}``:
``text`` is ``body.innerText`` (not ``textContent``, which would include the
confirmation sentence a single-page app ships inside its JS bundle), and
``fields`` counts VISIBLE form controls through open shadow roots, because a
React application can render its whole form with zero ``<form>`` elements.
Every verdict is taken here in Python, where it is testable.
"""
from __future__ import annotations

import asyncio
import re
import time
import urllib.parse
from typing import Any, Awaitable, Callable, Optional

CONFIRMED = "confirmed"
UNCONFIRMED = "unconfirmed"

#: A path only a page AFTER a submission has. ``formResponse`` is where a
#: submitted Google Form lands (``/viewform`` -> ``/formResponse``).
CONFIRMED_PATH_RE = re.compile(
    r"confirm|thank|success|complete|submitted|formResponse", re.IGNORECASE)

#: Page copy that only appears once an application has landed. Some ATSes
#: keep the same path on success, so for them this sentence is the whole proof.
CONFIRMED_TEXT_RE = re.compile(
    r"application (has been |was )?(successfully )?(received|submitted|sent)"
    r"|thank you for applying"
    r"|we.{0,10}ve received your application"
    r"|your response has been recorded"
    r"|application status\s+application submitted"
    r"|your application was sent to",
    re.IGNORECASE,
)

#: The one ATS host where the form leaving the page IS the delivery (it shows
#: no confirmation and returns to the posting; verified against the employer's
#: side). Everywhere else form removal stays evidence, never proof.
FORM_GONE_DELIVERS_HOST = "careers-page.com"

#: Landings that prove a press was NOT an application's submit. Each shape was
#: read off real ``submit_unconfirmed`` rows: the filled "form" was the site's
#: keyword search, a posting's apply opener, or a job-alert box (GET form that
#: put the applicant's e-mail into the URL).
_SEARCH_PATH_RE = re.compile(r"/search(?:-jobs|-results)?(?:/|$)", re.IGNORECASE)
_APPLY_ENTRY_RE = re.compile(r"/apply/?$", re.IGNORECASE)
_APPLY_ENTRY_QUERY = "jobseqno"
_EMAIL_QUERY_RE = re.compile(r"e-?mail", re.IGNORECASE)

NOT_AN_APPLICATION = "not an application press"

WATCH_SECONDS = 10.0
POLL_SECONDS = 0.75

_NOTHING = "nothing"
_FORM_GONE = "form-gone"
_REJECTED = "rejected"
#: Which non-confirming reading is worth REPORTING when the window closes.
_STRENGTH = {_NOTHING: 0, _FORM_GONE: 1, _REJECTED: 2}

MAX_EVIDENCE_CHARS = 200


def _path_of(url: str) -> str:
    """The path component of a URL ('' when unparseable)."""
    try:
        return urllib.parse.urlsplit(str(url or "")).path or ""
    except ValueError:
        return ""


def _controls(reading: Any) -> tuple[int, int]:
    """``(forms, fields)`` a reading saw; anything else counts as none."""
    if not isinstance(reading, dict):
        return 0, 0
    return int(reading.get("forms") or 0), int(reading.get("fields") or 0)


def before_summary(before: Any) -> Optional[dict]:
    """The part of a before-reading worth keeping in a run record."""
    if not isinstance(before, dict) or before.get("error"):
        return None
    forms, fields = _controls(before)
    return {"path": str(before.get("path") or ""), "forms": forms, "fields": fields}


def landing_refusal(href: str, *, start_url: str) -> str:
    """Whether a press landed somewhere that proves it sent no application.

    Args:
        href: Where the browser is after the press.
        start_url: The URL the runner opened. A landing only counts when its
            path differs from this one.
    Returns:
        Evidence starting with NOT_AN_APPLICATION, or "". The applicant's
        address is never echoed back.
    """
    try:
        parts = urllib.parse.urlsplit(str(href or ""))
    except ValueError:
        return ""
    path = parts.path or ""
    if not parts.netloc or path.rstrip("/") == _path_of(start_url).rstrip("/"):
        return ""
    query = urllib.parse.parse_qs(parts.query)
    if _SEARCH_PATH_RE.search(path):
        return (f"{NOT_AN_APPLICATION}: landed on the site's job search ({path[:60]}) — "
                "a search was run, no application was sent")
    if _APPLY_ENTRY_RE.search(path) and any(k.lower() == _APPLY_ENTRY_QUERY for k in query):
        return (f"{NOT_AN_APPLICATION}: landed on the posting's apply entry ({path[:60]}) — "
                "the press opened the application, it did not send one")
    if any(_EMAIL_QUERY_RE.search(k) and "@" in "".join(v) for k, v in query.items()):
        return (f"{NOT_AN_APPLICATION}: the e-mail address went into the page address "
                f"({parts.netloc}{path[:40]}) — a job-alert or newsletter form, not an application")
    return ""


def read_verdict(reading: Any, *, start_url: str, before_text: str = "",
                 before: Optional[dict] = None) -> tuple[str, str]:
    """What one reading of the page says about the submission.

    Args:
        reading: The probe's reading, or ``{'error': ...}``.
        start_url: The URL the runner OPENED (rung 1's only baseline).
        before_text: Page text before the click; rung 2 then only counts a
            confirmation sentence that was not already there.
        before: A whole reading taken before the click. Rung 4 compares
            against it: "no form left" only means anything if a form WAS there.
    Returns:
        ``(state, evidence)``; state is confirmed / rejected / form-gone / nothing.
    """
    if isinstance(before, dict) and not before_text:
        before_text = str(before.get("text") or "")
    if not isinstance(reading, dict) or reading.get("error"):
        detail = str((reading or {}).get("error") or "unreadable page") \
            if isinstance(reading, dict) else "unreadable page"
        return _NOTHING, f"could not read the page ({detail})"[:MAX_EVIDENCE_CHARS]

    path = str(reading.get("path") or "")
    if path and path != _path_of(start_url) and CONFIRMED_PATH_RE.search(path):
        return CONFIRMED, f"url:{path}"[:MAX_EVIDENCE_CHARS]

    match = CONFIRMED_TEXT_RE.search(str(reading.get("text") or ""))
    if match and not CONFIRMED_TEXT_RE.search(str(before_text or "")):
        return CONFIRMED, f"text:{match.group(0)}"[:MAX_EVIDENCE_CHARS]

    errors = int(reading.get("errors") or 0)
    if errors > 0:
        said = "; ".join(str(t) for t in (reading.get("error_texts") or []) if str(t).strip())
        evidence = f"{errors} validation error(s) showing"
        return _REJECTED, (f"{evidence}: {said}" if said else evidence)[:MAX_EVIDENCE_CHARS]

    refused = landing_refusal(str(reading.get("href") or ""), start_url=start_url)
    if refused:
        return _REJECTED, refused[:MAX_EVIDENCE_CHARS]

    forms, fields = _controls(reading)
    if forms == 0 and fields == 0:
        if not isinstance(before, dict) or before.get("error"):
            return _NOTHING, ("no form on the page, and nothing was read "
                              "before the click to compare with")
        had_forms, had_fields = _controls(before)
        if had_forms == 0 and had_fields == 0:
            return _NOTHING, "no form on the page before or after the click"
        host = str(urllib.parse.urlparse(start_url).hostname or "").lower()
        if host == FORM_GONE_DELIVERS_HOST or host.endswith("." + FORM_GONE_DELIVERS_HOST):
            return CONFIRMED, (f"form-gone:{FORM_GONE_DELIVERS_HOST} took the {had_fields} "
                               "field(s) off the page")[:MAX_EVIDENCE_CHARS]
        return _FORM_GONE, (f"the {had_fields} field(s) and {had_forms} form(s) "
                            "on the page before the click are gone")

    return _NOTHING, "no confirmation on the page"


async def confirm(read: Callable[[], Awaitable[Any]], *, start_url: str,
                  before_text: str = "", before: Optional[dict] = None,
                  watch_seconds: float = WATCH_SECONDS,
                  poll_seconds: float = POLL_SECONDS,
                  sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                  clock: Callable[[], float] = time.monotonic) -> dict:
    """Watch the page for up to ten seconds and say what an employer got.

    Never settles early on a NON-confirming rung: out here ``rejected`` and
    ``form-gone`` both land on the same outcome, so stopping on one could only
    lose a confirmation that was one poll away. ``sleep`` and ``clock`` are
    injected so a test can walk the ladder without spending real seconds.

    Returns:
        ``{'verdict', 'evidence', 'url', 'polls', 'before'}``. Verdict is
        ``confirmed`` only when an employer's own page said so.
    """
    deadline = clock() + float(watch_seconds)
    best_state, best_evidence = _NOTHING, "the page was never read"
    landed = ""
    polls = 0

    while True:
        reading = await read()
        polls += 1
        if isinstance(reading, dict) and reading.get("href"):
            landed = str(reading.get("href") or "") or landed
        state, evidence = read_verdict(reading, start_url=start_url,
                                       before_text=before_text, before=before)
        if state == CONFIRMED:
            return {"verdict": CONFIRMED, "evidence": evidence, "url": landed,
                    "polls": polls, "before": before_summary(before)}
        if _STRENGTH.get(state, 0) >= _STRENGTH.get(best_state, 0):
            best_state, best_evidence = state, evidence
        if clock() >= deadline:
            break
        await sleep(poll_seconds)

    return {"verdict": UNCONFIRMED, "evidence": best_evidence, "url": landed,
            "polls": polls, "before": before_summary(before)}
