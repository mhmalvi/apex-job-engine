"""Tests for the deterministic JD requirement extractor (AAA layout)."""
import jd_requirements as jd

POSTING = """We build developer tooling.

Responsibilities
- Own our Kafka consumers and ship features weekly.

Requirements
- 5+ years of professional Python experience
- Hands-on with Docker and PostgreSQL
- Comfortable with LangChain or similar LLM tooling

Benefits
- Remote-first, flexible hours
"""


def test_terms_in_requirements_section_are_must_have():
    # Arrange / Act
    reqs = jd.extract_requirements(POSTING, "Senior Backend Engineer (Remote)")

    # Assert
    must = reqs.must_haves()
    assert {"python", "docker", "postgresql", "langchain", "llm"} <= set(must)
    assert "kafka" not in must          # mentioned only under Responsibilities
    assert "kafka" in reqs.terms()


def test_years_demand_and_title_words_are_extracted():
    # Arrange / Act
    reqs = jd.extract_requirements(POSTING, "Senior Backend Engineer (Remote)")

    # Assert
    assert "5+ years" in reqs.must_haves()
    assert reqs.core_title == "Senior Backend Engineer"
    assert "backend" in reqs.must_haves()   # seniority word "senior" is noise


def test_clean_title_strips_company_and_location():
    # Arrange
    raw = "Platform Engineer – Data Infra (Remote, EU)"

    # Act
    title = jd.clean_title(raw)

    # Assert
    assert title == "Platform Engineer"


def test_company_name_is_never_a_tool():
    # Arrange
    posting = "Requirements\n- Experience with GraphQL at ACME scale\n- Uses DataDog daily"

    # Act
    reqs = jd.extract_requirements(posting, "Engineer", company="ACME Corp")

    # Assert
    assert "acme" not in reqs.terms()
    assert "datadog" in reqs.terms()


def test_coverage_weights_and_reports_must_have_gaps():
    # Arrange
    reqs = jd.extract_requirements(POSTING, "Backend Engineer")
    resume_text = "python engineer; built rest services in docker on postgres. backend."

    # Act
    cov = jd.coverage(reqs, resume_text)

    # Assert
    assert 0 < cov.score < 1
    assert "python" in cov.matched and "postgresql" in cov.matched   # synonym "postgres"
    assert "langchain" in cov.must_missed


def test_same_input_gives_same_output():
    # Arrange / Act
    first = jd.extract_requirements(POSTING, "Backend Engineer")
    second = jd.extract_requirements(POSTING, "Backend Engineer")

    # Assert
    assert first == second
