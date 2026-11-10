"""Tests for the pluggable source interface and the fan-out runner."""
import job_sources as js


class Fixed(js.JobSource):
    name = "fixed"

    def __init__(self, rows, location_aware=True):
        self.rows, self.location_aware, self.calls = rows, location_aware, []

    def fetch(self, term, location, is_remote, hours_old, limit):
        self.calls.append((term, location))
        return list(self.rows)


class Broken(js.JobSource):
    name = "broken"

    def fetch(self, *a, **k):
        raise RuntimeError("quota exhausted")


class Disabled(js.JobSource):
    name = "off"

    def enabled(self):
        return False


QUERIES = [{"term": "python developer", "location": "Remote"},
           {"term": "python developer", "location": "Germany"},
           {"term": "", "location": "x"}]


def test_location_blind_source_gets_each_term_once():
    # Arrange
    src = Fixed([], location_aware=False)

    # Act
    js.run_source(src, QUERIES, hours_old=72, limit=10)

    # Assert
    assert src.calls == [("python developer", "Remote")]


def test_rows_are_deduped_by_url_within_a_source():
    # Arrange
    row = {"url": "https://example.com/j/1", "title": "Engineer"}
    src = Fixed([row, dict(row)])

    # Act
    out = js.run_source(src, QUERIES, hours_old=72, limit=10)

    # Assert
    assert out == [row]


def test_deadline_stops_new_queries_but_keeps_rows():
    # Arrange
    src = Fixed([{"url": "https://example.com/j/1", "title": "E"}])
    ticks = iter([0.0, 999.0, 999.0])

    # Act
    out = js.run_source(src, QUERIES, 72, 10, deadline=10.0, clock=lambda: next(ticks))

    # Assert
    assert len(src.calls) == 1 and len(out) == 1


def test_a_failing_source_does_not_break_discovery():
    # Arrange
    good = Fixed([{"url": "https://example.com/j/2", "title": "E"}])
    sources = js.load_enabled_sources([lambda: good, Broken, Disabled])

    # Act
    rows = js.fetch_from_sources(sources, QUERIES[:1], hours_old=72, limit=10)

    # Assert
    assert [s.name for s in sources] == ["fixed", "broken"]
    assert rows == [{"url": "https://example.com/j/2", "title": "E"}]


def test_example_provider_normalises_and_drops_unusable_rows():
    # Arrange
    payload = {"jobs": [
        {"title": "Backend Engineer", "company_name": "Example", "url": "https://example.com/1",
         "description": "<p>Build <b>APIs</b></p>", "publication_date": "2026-01-01"},
        {"title": "", "url": "https://example.com/2"},
    ]}
    src = js.Remotive(fetch_json=lambda url, params: payload)

    # Act
    rows = src.fetch("backend", "", True, 72, 10)

    # Assert
    assert len(rows) == 1
    assert rows[0]["description"] == "Build APIs" and rows[0]["is_remote"] is True


def test_term_matches_is_loose_token_match():
    # Arrange / Act / Assert
    assert js.term_matches("AI engineer", "Senior ML Engineer")
    assert not js.term_matches("AI engineer", "Accountant")
