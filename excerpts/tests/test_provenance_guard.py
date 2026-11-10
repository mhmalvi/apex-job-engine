"""Tests for the résumé provenance guard and the guarded tailoring loop."""
import provenance_guard as pg

BASE = {
    "summary": "Backend engineer building Python services and data pipelines.",
    "experience": [{
        "title": "Software Engineer", "company": "Example Co", "dates": "2019 - 2024",
        "bullets": [
            "Built Python ETL jobs that cut report latency by 40%.",
            "Ran Docker deployments for 12 internal services.",
        ],
    }],
    "skills": {"expert": ["Python", "Docker"], "proficient": ["PostgreSQL"]},
}

JOB = {"title": "Backend Engineer", "company": "Example Inc",
       "description": "Requirements\n- Python\n- Docker\n- PostgreSQL\n- Kubernetes"}


def _draft(summary, bullets):
    return {"summary": summary,
            "experience": [dict(BASE["experience"][0], bullets=bullets)]}


def test_invented_number_is_a_violation():
    # Arrange
    draft = _draft(BASE["summary"], ["Built Python ETL jobs that cut latency by 75%."])

    # Act
    violations = pg.provenance_violations({**BASE, **draft}, BASE)

    # Assert
    assert any("75%" in v for v in violations)


def test_licensed_number_in_another_form_passes():
    # Arrange: base says "40%", rewrite says "40 percent" -> bare 40 is licensed
    draft = _draft(BASE["summary"], ["Cut report latency 40 percent with Python ETL jobs."])

    # Act
    violations = pg.provenance_violations({**BASE, **draft}, BASE)

    # Assert
    assert violations == []


def test_inserted_tool_and_claim_word_are_caught():
    # Arrange
    draft = _draft("Engineer who exceeded quota selling ERP suites.", BASE["experience"][0]["bullets"])

    # Act
    violations = pg.provenance_violations({**BASE, **draft}, BASE)

    # Assert
    assert any("'quota'" in v for v in violations)
    assert any("'erp'" in v for v in violations)


def test_enforce_swaps_offending_bullet_back_to_base():
    # Arrange
    bad = "Ran Kubernetes for 300 services."
    draft = {**BASE, **_draft(BASE["summary"], [BASE["experience"][0]["bullets"][0], bad])}

    # Act
    cleaned, violations = pg.enforce_provenance(draft, BASE)

    # Assert
    assert violations
    assert cleaned["experience"][0]["bullets"][1] == BASE["experience"][0]["bullets"][1]


def test_reordering_base_sentences_is_not_tailoring():
    # Arrange
    shuffled = {**BASE, **_draft(BASE["summary"], list(reversed(BASE["experience"][0]["bullets"])))}

    # Act / Assert
    assert pg.is_tailored(shuffled, BASE) is False


def test_loop_reasks_with_violations_and_keeps_clean_draft():
    # Arrange: first pass fabricates a number, second pass is clean
    calls = []

    def draft_fn(base, job, reqs, missed, violations):
        calls.append(violations)
        if len(calls) == 1:
            return _draft("Backend engineer shipping PostgreSQL-backed Python services.",
                          ["Built Python ETL jobs that cut latency by 90%."])
        return _draft("Backend engineer shipping PostgreSQL-backed Python services in Docker.",
                      BASE["experience"][0]["bullets"])

    # Act
    chosen, report = pg.tailor_with_guard(BASE, JOB, draft_fn)

    # Assert
    assert report["passes"] == 2
    assert calls[0] is None and any("90%" in v for v in calls[1])
    assert "90%" not in pg.prose_of(chosen)


def test_no_draft_falls_back_to_base():
    # Arrange / Act
    chosen, report = pg.tailor_with_guard(BASE, JOB, lambda *a: None)

    # Assert
    assert chosen is BASE and report["fallback"] == "base"
