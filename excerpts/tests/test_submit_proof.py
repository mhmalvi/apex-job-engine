"""Tests for the post-click submission verdict ladder."""
import asyncio

import submit_proof as sp

START = "https://jobs.example.com/acme/123/application"
FORM_PAGE = {"href": START, "path": "/acme/123/application", "text": "Apply now",
             "errors": 0, "forms": 0, "fields": 24}


def test_confirmation_path_confirms():
    # Arrange
    reading = {"href": "https://jobs.example.com/acme/123/thank-you",
               "path": "/acme/123/thank-you", "text": "", "errors": 0}

    # Act
    state, evidence = sp.read_verdict(reading, start_url=START)

    # Assert
    assert state == sp.CONFIRMED and evidence.startswith("url:")


def test_confirmation_copy_already_on_page_before_click_does_not_confirm():
    # Arrange
    text = "Thank you for applying to many roles!"
    reading = dict(FORM_PAGE, text=text)

    # Act
    state, _ = sp.read_verdict(reading, start_url=START, before_text=text)

    # Assert
    assert state != sp.CONFIRMED


def test_validation_errors_reject_with_the_fields_named():
    # Arrange
    reading = dict(FORM_PAGE, errors=1, error_texts=["invalid: Phone"])

    # Act
    state, evidence = sp.read_verdict(reading, start_url=START)

    # Assert
    assert state == "rejected" and "Phone" in evidence


def test_landing_on_job_search_is_not_an_application():
    # Arrange
    reading = {"href": "https://careers.example.com/search/?q=python", "path": "/search/",
               "text": "", "errors": 0, "forms": 1, "fields": 3}

    # Act
    state, evidence = sp.read_verdict(reading, start_url="https://careers.example.com/job/9")

    # Assert
    assert state == "rejected" and evidence.startswith(sp.NOT_AN_APPLICATION)


def test_email_in_url_is_a_job_alert_and_address_is_not_echoed():
    # Arrange
    href = "https://example.com/jobs/?email=someone%40example.org"

    # Act
    evidence = sp.landing_refusal(href, start_url="https://example.com/jobs/42")

    # Assert
    assert "job-alert" in evidence and "someone" not in evidence


def test_formless_page_before_and_after_is_never_form_gone():
    # Arrange: a React app with zero <form> and zero visible fields both times
    empty = {"href": START, "path": "/acme/123/application", "text": "", "errors": 0,
             "forms": 0, "fields": 0}

    # Act
    state, _ = sp.read_verdict(empty, start_url=START, before=dict(empty))

    # Assert
    assert state == "nothing"


def test_fields_disappearing_is_evidence_not_proof():
    # Arrange
    after = {"href": "https://login.example.com/", "path": "/", "text": "Sign in",
             "errors": 0, "forms": 0, "fields": 0}

    # Act
    state, _ = sp.read_verdict(after, start_url=START, before=FORM_PAGE)

    # Assert
    assert state == "form-gone"


def test_confirm_polls_until_confirmation_without_real_sleeping():
    # Arrange
    readings = iter([FORM_PAGE, FORM_PAGE,
                     dict(FORM_PAGE, text="Your application was successfully submitted.")])
    now = [0.0]

    async def read():
        return next(readings)

    async def fake_sleep(seconds):
        now[0] += seconds

    # Act
    result = asyncio.run(sp.confirm(read, start_url=START, before=FORM_PAGE,
                                    sleep=fake_sleep, clock=lambda: now[0]))

    # Assert
    assert result["verdict"] == sp.CONFIRMED and result["polls"] == 3


def test_confirm_times_out_unconfirmed_and_reports_strongest_evidence():
    # Arrange
    sequence = [dict(FORM_PAGE, errors=2), FORM_PAGE]
    now = [0.0]

    async def read():
        return sequence[min(int(now[0]), len(sequence) - 1)]

    async def fake_sleep(seconds):
        now[0] += seconds

    # Act
    result = asyncio.run(sp.confirm(read, start_url=START, sleep=fake_sleep,
                                    clock=lambda: now[0], watch_seconds=3, poll_seconds=1))

    # Assert
    assert result["verdict"] == sp.UNCONFIRMED
    assert "validation error" in result["evidence"]
