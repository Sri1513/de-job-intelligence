"""Tests for email-pipeline hardening: title sanitization and US-location gating."""
from src.ingestion.email_scraper import clean_job_title, parse_linkedin_plain_text_email
from src.workers.email_pipeline import is_us_location


class TestCleanJobTitle:
    def test_keeps_clean_title(self):
        assert clean_job_title("Senior Data Engineer") == "Senior Data Engineer"

    def test_strips_tracking_url(self):
        raw = (
            "Jobs similar to PROJECT - Data Engineer II at Deloitte "
            "https://www.linkedin.com/comm/jobs/view/44730992?refId=abc&trackingId=xyz"
        )
        assert clean_job_title(raw) == "Jobs similar to PROJECT - Data Engineer II at Deloitte"

    def test_truncates_very_long_title(self):
        raw = "Data Engineer " + ("x" * 300)
        cleaned = clean_job_title(raw)
        assert cleaned is not None
        assert len(cleaned) <= 151  # 150 chars + ellipsis marker

    def test_rejects_url_only_garbage(self):
        assert clean_job_title("https://www.linkedin.com/comm/jobs/view/123") is None

    def test_rejects_too_short(self):
        assert clean_job_title("Hi") is None
        assert clean_job_title("") is None
        assert clean_job_title(None) is None

    def test_collapses_whitespace(self):
        assert clean_job_title("Data   Engineer\n\tII") == "Data Engineer II"


class TestParseSkipsJunkChunks:
    def test_reminder_format_chunk_does_not_produce_url_title(self):
        body = (
            "Jobs similar to PROJECT - Data Engineer II at Deloitte "
            "https://www.linkedin.com/comm/jobs/view/44730992?refId=abc\n"
            "Deloitte\n"
            "---------------------------------------------------------\n"
            "Senior Data Engineer\n"
            "Acme Corp\n"
            "New York, NY\n"
            "View job: https://www.linkedin.com/jobs/view/1\n"
        )
        jobs = parse_linkedin_plain_text_email(body)
        titles = [j["title"] for j in jobs]
        assert not any("http" in t for t in titles)
        assert "Senior Data Engineer" in titles


class TestIsUsLocation:
    def test_state_abbreviation(self):
        assert is_us_location({"location": "Austin, TX"}) is True
        assert is_us_location({"location": "New York, NY"}) is True

    def test_state_name(self):
        assert is_us_location({"location": "Seattle, Washington"}) is True

    def test_usa_markers(self):
        assert is_us_location({"location": "United States"}) is True
        assert is_us_location({"location": "Remote, USA"}) is True

    def test_remote_labels(self):
        assert is_us_location({"location": "Remote"}) is True

    def test_remote_no_location_accepted(self):
        assert is_us_location({"location": "", "is_remote": True}) is True

    def test_non_us_rejected(self):
        assert is_us_location({"location": "London, United Kingdom"}) is False
        assert is_us_location({"location": "Toronto, Canada"}) is False
        assert is_us_location({"location": "Bengaluru, India"}) is False
        assert is_us_location({"location": "Dublin, Ireland"}) is False

    def test_empty_non_remote_rejected(self):
        assert is_us_location({"location": ""}) is False
        assert is_us_location({}) is False
