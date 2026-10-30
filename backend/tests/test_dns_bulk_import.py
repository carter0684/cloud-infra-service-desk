"""
Tests for the bulk-import parsing/preview/apply logic in
app/dns_records.py (the "Bulk import" section) — CSV/TSV, BIND
zone-file, and .xlsx (Excel) auto-detection/parsing,
CREATE/MODIFY/UNCHANGED/ERROR classification, and the apply path's
per-row writes.

NO REAL AWS CALLS: reuses test_dns_records.py's FakeRoute53 double via
the same monkeypatch of dns_records._r53_client(). This matters even
more here than for the single-record tests, since a bulk apply issues
one ChangeResourceRecordSets call per row — a real call would mutate
several live records per test run, not one.
"""

from __future__ import annotations

import base64
from io import BytesIO

import openpyxl
import pytest

import app.dns_records as dns
from .test_dns_records import (
    FakeRoute53,
    ZONE,
    A_RECORD,
    NS_RECORD,
    ALIAS_RECORD,
    WEIGHTED_RECORD,
    ALL_RECORDS,
)


def _patch(monkeypatch, client):
    monkeypatch.setattr(dns, "_r53_client", lambda: client)
    return client


def _xlsx_b64(rows: list[list]) -> str:
    """Build a minimal single-sheet .xlsx workbook in memory from `rows`
    (first row = header) and return its bytes, base64-encoded — exactly
    the shape the frontend sends as DnsBulkPreviewBody.file_content_b64
    after reading a real .xlsx pick with FileReader.readAsArrayBuffer()."""
    wb = openpyxl.Workbook()
    ws = wb.active
    for row in rows:
        ws.append(row)
    buf = BytesIO()
    wb.save(buf)
    return base64.b64encode(buf.getvalue()).decode("ascii")


@pytest.fixture
def fake(monkeypatch):
    client = FakeRoute53(zones=[ZONE], records={"Z0861165ABC": list(ALL_RECORDS)})
    monkeypatch.setattr(dns, "_r53_client", lambda: client)
    return client


# ── Format auto-detection ───────────────────────────────────────────────────

def test_detect_format_csv_header_with_comma():
    assert dns._detect_bulk_format("name,type,ttl,value\nwww.example.com,A,300,203.0.113.1") == "csv"


def test_detect_format_csv_header_with_tab():
    assert dns._detect_bulk_format("name\ttype\tttl\tvalue\nwww.example.com\tA\t300\t203.0.113.1") == "csv"


def test_detect_format_zone_file_when_no_recognised_header():
    zone_text = "$ORIGIN example.com.\nwww 300 IN A 203.0.113.1\n"
    assert dns._detect_bulk_format(zone_text) == "zone"


def test_detect_format_skips_blank_lines_and_comments_before_deciding():
    text = "\n; a leading comment\n\nname,type,ttl,value\nwww.example.com,A,300,1.2.3.4"
    assert dns._detect_bulk_format(text) == "csv"


def test_detect_format_zone_file_does_not_false_positive_on_type_alone():
    # A real record line contains a type token ("A") but no cell that is
    # literally one of the value-header words, so it must not be
    # misdetected as CSV.
    assert dns._detect_bulk_format("www.example.com. 300 IN A 203.0.113.1") == "zone"


# ── CSV parsing ─────────────────────────────────────────────────────────────

def test_csv_parse_basic_rows():
    text = "name,type,ttl,value\nwww.example.com,A,300,203.0.113.1\nmail.example.com,MX,600,10 mail.example.com"
    rows = dns._parse_csv_rows(text)
    assert len(rows) == 2
    assert rows[0]["name"] == "www.example.com"
    assert rows[0]["type"] == "A"
    assert rows[0]["ttl"] == "300"
    assert rows[0]["value"] == "203.0.113.1"


def test_csv_parse_accepts_header_synonyms():
    text = "host,record_type,ttl,data\nwww.example.com,A,300,203.0.113.1"
    rows = dns._parse_csv_rows(text)
    assert rows[0]["name"] == "www.example.com"
    assert rows[0]["type"] == "A"


def test_csv_parse_missing_required_column_raises():
    with pytest.raises(dns.DnsRecordError, match="missing a required column"):
        dns._parse_csv_rows("name,ttl\nwww.example.com,300")


def test_csv_parse_semicolon_separated_multi_value_cell():
    text = "name,type,ttl,value\nwww.example.com,A,300,203.0.113.1;203.0.113.2"
    rows = dns._parse_csv_rows(text)
    assert rows[0]["value"] == "203.0.113.1;203.0.113.2"


def test_csv_parse_skips_blank_rows():
    text = "name,type,ttl,value\nwww.example.com,A,300,203.0.113.1\n\n,,,\n"
    rows = dns._parse_csv_rows(text)
    assert len(rows) == 1


# ── Zone-file parsing ────────────────────────────────────────────────────────

def test_zone_file_parse_basic_line():
    rows = dns._parse_zone_file_rows("www.example.com. 300 IN A 203.0.113.1")
    assert rows[0]["name"] == "www.example.com."
    assert rows[0]["type"] == "A"
    assert rows[0]["ttl"] == "300"
    assert rows[0]["value"] == "203.0.113.1"


def test_zone_file_parse_ttl_without_class_token():
    rows = dns._parse_zone_file_rows("www.example.com. 300 A 203.0.113.1")
    assert rows[0]["ttl"] == "300"
    assert rows[0]["type"] == "A"


def test_zone_file_parse_no_ttl_no_class():
    rows = dns._parse_zone_file_rows("www.example.com. A 203.0.113.1")
    assert rows[0]["ttl"] is None
    assert rows[0]["type"] == "A"
    assert rows[0]["value"] == "203.0.113.1"


def test_zone_file_parse_origin_directive_expands_bare_names():
    text = "$ORIGIN example.com.\nwww 300 IN A 203.0.113.1"
    rows = dns._parse_zone_file_rows(text)
    assert rows[0]["name"] == "www.example.com."


def test_zone_file_parse_at_sign_means_the_origin_itself():
    text = "$ORIGIN example.com.\n@ 300 IN A 203.0.113.1"
    rows = dns._parse_zone_file_rows(text)
    assert rows[0]["name"] == "example.com."


def test_zone_file_parse_default_ttl_directive():
    text = "$TTL 600\nwww.example.com. IN A 203.0.113.1"
    rows = dns._parse_zone_file_rows(text)
    assert rows[0]["ttl"] == "600"


def test_zone_file_parse_blank_name_continues_previous_line():
    text = "www.example.com. 300 IN A 203.0.113.1\n         300 IN A 203.0.113.2"
    rows = dns._parse_zone_file_rows(text)
    assert rows[0]["name"] == rows[1]["name"] == "www.example.com."
    assert rows[1]["value"] == "203.0.113.2"


def test_zone_file_parse_strips_comments_outside_quotes():
    text = 'www.example.com. 300 IN TXT "hello" ; this is a comment'
    rows = dns._parse_zone_file_rows(text)
    assert rows[0]["value"] == '"hello"'


def test_zone_file_parse_keeps_semicolon_inside_quotes():
    text = 'www.example.com. 300 IN TXT "a;b"'
    rows = dns._parse_zone_file_rows(text)
    assert rows[0]["value"] == '"a;b"'


def test_zone_file_parse_joins_parenthesised_continuation():
    text = "www.example.com. 300 IN TXT ( \"part-one\"\n\"part-two\" )"
    rows = dns._parse_zone_file_rows(text)
    assert len(rows) == 1
    assert '"part-one"' in rows[0]["value"] and '"part-two"' in rows[0]["value"]


def test_zone_file_parse_skips_unsupported_directives():
    text = "$INCLUDE other.zone\nwww.example.com. 300 IN A 203.0.113.1"
    rows = dns._parse_zone_file_rows(text)
    assert len(rows) == 1
    assert rows[0]["type"] == "A"


def test_zone_file_parse_reports_missing_type_as_parse_error():
    rows = dns._parse_zone_file_rows("www.example.com. 300 IN")
    assert rows[0]["parse_error"] is not None


# ── Merge (multi-value grouping) ─────────────────────────────────────────────

def test_merge_groups_rows_by_name_and_type():
    rows = [
        {"line_no": 1, "raw": "r1", "name": "www.example.com.", "type": "A", "ttl": "300", "value": "203.0.113.1", "parse_error": None},
        {"line_no": 2, "raw": "r2", "name": "www.example.com.", "type": "A", "ttl": None, "value": "203.0.113.2", "parse_error": None},
        {"line_no": 3, "raw": "r3", "name": "mail.example.com.", "type": "MX", "ttl": "600", "value": "10 mail.example.com.", "parse_error": None},
    ]
    groups = dns._merge_bulk_rows(rows)
    assert len(groups) == 2
    www = next(g for g in groups if g["type"] == "A")
    assert sorted(www["values"]) == ["203.0.113.1", "203.0.113.2"]
    assert www["ttl"] == "300"          # filled in from whichever line had it
    assert www["line_nos"] == [1, 2]


def test_merge_splits_semicolon_separated_single_row_value():
    rows = [{"line_no": 1, "raw": "r1", "name": "www.example.com.", "type": "A", "ttl": "300", "value": "203.0.113.1;203.0.113.2", "parse_error": None}]
    groups = dns._merge_bulk_rows(rows)
    assert sorted(groups[0]["values"]) == ["203.0.113.1", "203.0.113.2"]


def test_merge_keeps_error_rows_separate_even_with_same_name_type():
    rows = [
        {"line_no": 1, "raw": "bad line", "name": "", "type": "", "ttl": None, "value": "", "parse_error": "boom"},
        {"line_no": 2, "raw": "bad line 2", "name": "", "type": "", "ttl": None, "value": "", "parse_error": "boom again"},
    ]
    groups = dns._merge_bulk_rows(rows)
    assert len(groups) == 2  # NOT merged into one group despite identical (blank) name/type


# ── preview_bulk_import: classification ─────────────────────────────────────

def test_preview_classifies_new_name_as_create(fake):
    text = "name,type,ttl,value\nnewhost.example-site-a.com,A,300,203.0.113.55"
    result = dns.preview_bulk_import(zone_id="Z0861165ABC", file_content=text)
    assert result["counts"]["create"] == 1
    row = result["rows"][0]
    assert row["action"] == "CREATE"
    assert row["values"] == ["203.0.113.55"]
    assert row["previous_values"] is None
    assert fake.change_calls == []  # preview never writes


def test_preview_classifies_same_value_as_unchanged(fake):
    # A_RECORD is www.example-site-a.com TTL 300 values [.10, .11]
    text = "name,type,ttl,value\nwww.example-site-a.com,A,300,203.0.113.10;203.0.113.11"
    result = dns.preview_bulk_import(zone_id="Z0861165ABC", file_content=text)
    assert result["counts"]["unchanged"] == 1
    row = result["rows"][0]
    assert row["action"] == "UNCHANGED"
    assert sorted(row["previous_values"]) == ["203.0.113.10", "203.0.113.11"]


def test_preview_classifies_different_value_as_modify_with_diff(fake):
    text = "name,type,ttl,value\nwww.example-site-a.com,A,300,203.0.113.99"
    result = dns.preview_bulk_import(zone_id="Z0861165ABC", file_content=text)
    assert result["counts"]["modify"] == 1
    row = result["rows"][0]
    assert row["action"] == "MODIFY"
    assert row["values"] == ["203.0.113.99"]
    assert sorted(row["previous_values"]) == ["203.0.113.10", "203.0.113.11"]


def test_preview_classifies_different_ttl_only_as_modify(fake):
    # Same values as A_RECORD, only the TTL differs — must still be
    # MODIFY, not UNCHANGED, since Route 53 UPSERT would change the TTL.
    text = "name,type,ttl,value\nwww.example-site-a.com,A,900,203.0.113.10;203.0.113.11"
    result = dns.preview_bulk_import(zone_id="Z0861165ABC", file_content=text)
    assert result["counts"]["modify"] == 1
    assert result["rows"][0]["previous_ttl"] == 300


def test_preview_ns_soa_rows_are_errors(fake):
    text = "name,type,ttl,value\nexample-site-a.com,NS,172800,ns-9.awsdns-00.com."
    result = dns.preview_bulk_import(zone_id="Z0861165ABC", file_content=text)
    assert result["counts"]["error"] == 1
    row = result["rows"][0]
    assert row["action"] == "ERROR"
    assert "cannot be added, modified or deleted" in row["error"]


def test_preview_bad_line_is_an_error_row_and_does_not_abort_the_rest(fake):
    text = (
        "name,type,ttl,value\n"
        "newhost.example-site-a.com,A,300,203.0.113.55\n"
        "badhost.example-site-a.com,ZZZ,300,whatever\n"
    )
    result = dns.preview_bulk_import(zone_id="Z0861165ABC", file_content=text)
    assert result["counts"]["create"] == 1
    assert result["counts"]["error"] == 1


def test_preview_zone_file_input_end_to_end(fake):
    text = "$ORIGIN example-site-a.com.\nnewhost 300 IN A 203.0.113.77\n"
    result = dns.preview_bulk_import(zone_id="Z0861165ABC", file_content=text)
    assert result["counts"]["create"] == 1
    assert result["rows"][0]["name"] == "newhost.example-site-a.com"


def test_preview_rejects_empty_file(fake):
    with pytest.raises(dns.DnsRecordError, match="empty"):
        dns.preview_bulk_import(zone_id="Z0861165ABC", file_content="   \n  \n")


def test_preview_rejects_more_than_max_rows(fake):
    lines = ["name,type,ttl,value"]
    for i in range(dns._MAX_BULK_ROWS + 1):
        lines.append(f"h{i}.example-site-a.com,A,300,203.0.113.{i % 255}")
    with pytest.raises(dns.DnsRecordError, match="more than the"):
        dns.preview_bulk_import(zone_id="Z0861165ABC", file_content="\n".join(lines))


# ── apply_bulk_import: writes ───────────────────────────────────────────────

def test_apply_requires_sr_number(fake):
    text = "name,type,ttl,value\nnewhost.example-site-a.com,A,300,203.0.113.55"
    with pytest.raises(dns.DnsRecordError, match="Service Request"):
        dns.apply_bulk_import(zone_id="Z0861165ABC", file_content=text, sr_number="", actor_email="a@b.com")
    assert fake.change_calls == []


def test_apply_creates_new_and_upserts_changed_and_skips_unchanged(fake):
    text = (
        "name,type,ttl,value\n"
        "newhost.example-site-a.com,A,300,203.0.113.55\n"                       # CREATE
        "www.example-site-a.com,A,300,203.0.113.10;203.0.113.11\n"              # UNCHANGED
        "cdn2.example-site-a.com,A,60,203.0.113.66\n"                            # CREATE (distinct name)
    )
    result = dns.apply_bulk_import(zone_id="Z0861165ABC", file_content=text, sr_number="SR-500", actor_email="a@b.com")

    assert result["counts"]["created"] == 2
    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["error"] == 0
    assert result["batch_id"]  # non-empty
    # Exactly two ChangeResourceRecordSets calls — the UNCHANGED row must
    # never reach Route 53.
    assert len(fake.change_calls) == 2
    actions = {c["ChangeBatch"]["Changes"][0]["Action"] for c in fake.change_calls}
    assert actions == {"CREATE"}


def test_apply_modifies_an_existing_record_with_a_different_value(fake):
    text = "name,type,ttl,value\nwww.example-site-a.com,A,300,203.0.113.200"
    result = dns.apply_bulk_import(zone_id="Z0861165ABC", file_content=text, sr_number="SR-501", actor_email="a@b.com")
    assert result["counts"]["modified"] == 1
    change = fake.change_calls[0]["ChangeBatch"]["Changes"][0]
    assert change["Action"] == "UPSERT"
    assert change["ResourceRecordSet"]["ResourceRecords"] == [{"Value": "203.0.113.200"}]


def test_apply_never_issues_a_delete_call(fake):
    """Bulk import is create/modify only — even a row that would look
    like it wants to remove something must never reach a DELETE action."""
    text = "name,type,ttl,value\nnewhost.example-site-a.com,A,300,203.0.113.1"
    dns.apply_bulk_import(zone_id="Z0861165ABC", file_content=text, sr_number="SR-502", actor_email="a@b.com")
    actions = {c["ChangeBatch"]["Changes"][0]["Action"] for c in fake.change_calls}
    assert "DELETE" not in actions


def test_apply_blocks_ns_soa_rows_and_reports_them_as_errors(fake):
    text = "name,type,ttl,value\nexample-site-a.com,NS,172800,ns-9.awsdns-00.com."
    result = dns.apply_bulk_import(zone_id="Z0861165ABC", file_content=text, sr_number="SR-503", actor_email="a@b.com")
    assert result["counts"]["error"] == 1
    assert fake.change_calls == []
    assert result["rows"][0]["result"] == "error"


def test_apply_stamps_the_same_batch_id_on_every_applied_row(fake):
    text = (
        "name,type,ttl,value\n"
        "newhost1.example-site-a.com,A,300,203.0.113.1\n"
        "newhost2.example-site-a.com,A,300,203.0.113.2\n"
    )
    result = dns.apply_bulk_import(zone_id="Z0861165ABC", file_content=text, sr_number="SR-504", actor_email="a@b.com")
    batch_ids = {r["batch_id"] for r in result["rows"]}
    assert batch_ids == {result["batch_id"]}


def test_apply_reports_route53_rejection_per_row_without_aborting_the_batch(monkeypatch):
    client = FakeRoute53(zones=[ZONE], records={"Z0861165ABC": list(ALL_RECORDS)})
    # Force every change call to fail, to prove a per-row failure still
    # returns a result (rather than raising and losing the other rows).
    def _always_fail(**kwargs):
        raise client.exceptions.InvalidChangeBatch("simulated rejection")

    client.change_resource_record_sets = _always_fail
    _patch(monkeypatch, client)

    text = "name,type,ttl,value\nnewhost.example-site-a.com,A,300,203.0.113.1"
    result = dns.apply_bulk_import(zone_id="Z0861165ABC", file_content=text, sr_number="SR-505", actor_email="a@b.com")
    assert result["counts"]["error"] == 1
    row = result["rows"][0]
    assert row["applied"] is False
    assert row["result"] == "error"
    assert "simulated rejection" in row["error"]


def test_apply_revalidates_against_live_state_not_a_stale_preview(fake):
    """If the zone changes between preview and apply, apply must reflect
    the CURRENT Route 53 state, not whatever preview_bulk_import()
    returned earlier — apply always re-parses and re-classifies itself."""
    text = "name,type,ttl,value\nwww.example-site-a.com,A,300,203.0.113.10;203.0.113.11"

    preview = dns.preview_bulk_import(zone_id="Z0861165ABC", file_content=text)
    assert preview["rows"][0]["action"] == "UNCHANGED"

    # Simulate someone else changing the live record between preview and
    # apply — now the same file content should MODIFY, not no-op.
    fake._records["Z0861165ABC"] = [
        {**A_RECORD, "ResourceRecords": [{"Value": "203.0.113.250"}]},
        NS_RECORD, ALIAS_RECORD, WEIGHTED_RECORD,
    ]

    result = dns.apply_bulk_import(zone_id="Z0861165ABC", file_content=text, sr_number="SR-506", actor_email="a@b.com")
    assert result["counts"]["modified"] == 1
    assert result["counts"]["unchanged"] == 0


# ── .xlsx (Excel) parsing/preview/apply ──────────────────────────────────────

def test_parse_xlsx_rows_basic():
    b64 = _xlsx_b64([
        ["name", "type", "ttl", "value"],
        ["newhost.example-site-a.com", "A", 300, "203.0.113.55"],
    ])
    rows = dns._parse_xlsx_rows(base64.b64decode(b64))
    assert len(rows) == 1
    assert rows[0]["name"] == "newhost.example-site-a.com"
    assert rows[0]["type"] == "A"
    assert rows[0]["ttl"] == "300"  # Excel stores 300 as a float — must not be "300.0"
    assert rows[0]["value"] == "203.0.113.55"
    assert rows[0]["parse_error"] is None


def test_parse_xlsx_rows_only_reads_the_first_worksheet():
    b64 = _xlsx_b64([
        ["name", "type", "ttl", "value"],
        ["newhost.example-site-a.com", "A", 300, "203.0.113.55"],
    ])
    wb = openpyxl.load_workbook(BytesIO(base64.b64decode(b64)))
    wb.create_sheet("Old Notes").append(["ignored.example-site-a.com", "A", 300, "203.0.113.99"])
    buf = BytesIO()
    wb.save(buf)
    b64_multi_sheet = base64.b64encode(buf.getvalue()).decode("ascii")

    rows = dns._parse_xlsx_rows(base64.b64decode(b64_multi_sheet))
    assert len(rows) == 1
    assert rows[0]["name"] == "newhost.example-site-a.com"


def test_parse_xlsx_rows_skips_blank_rows():
    b64 = _xlsx_b64([
        ["name", "type", "ttl", "value"],
        ["newhost.example-site-a.com", "A", 300, "203.0.113.55"],
        [None, None, None, None],
        ["cdn2.example-site-a.com", "A", 60, "203.0.113.66"],
    ])
    rows = dns._parse_xlsx_rows(base64.b64decode(b64))
    assert len(rows) == 2


def test_parse_xlsx_rows_missing_required_column_raises():
    b64 = _xlsx_b64([["name", "ttl"], ["newhost.example-site-a.com", 300]])
    with pytest.raises(dns.DnsRecordError, match="missing a required column"):
        dns._parse_xlsx_rows(base64.b64decode(b64))


def test_parse_xlsx_rows_corrupt_file_raises_clean_error():
    with pytest.raises(dns.DnsRecordError, match="Could not read this as an Excel"):
        dns._parse_xlsx_rows(b"this is not a real xlsx file")


def test_preview_accepts_xlsx_via_file_content_b64(fake):
    b64 = _xlsx_b64([
        ["name", "type", "ttl", "value"],
        ["newhost.example-site-a.com", "A", 300, "203.0.113.55"],
        ["www.example-site-a.com", "A", 300, "203.0.113.10;203.0.113.11"],
    ])
    result = dns.preview_bulk_import(zone_id="Z0861165ABC", file_content="", file_content_b64=b64)
    assert result["counts"]["create"] == 1
    assert result["counts"]["unchanged"] == 1
    assert result["counts"]["error"] == 0


def test_preview_rejects_empty_xlsx_workbook(fake):
    b64 = _xlsx_b64([])
    with pytest.raises(dns.DnsRecordError, match="empty"):
        dns.preview_bulk_import(zone_id="Z0861165ABC", file_content="", file_content_b64=b64)


def test_apply_creates_and_modifies_from_xlsx(fake):
    b64 = _xlsx_b64([
        ["name", "type", "ttl", "value"],
        ["newhost.example-site-a.com", "A", 300, "203.0.113.55"],
        ["www.example-site-a.com", "A", 300, "203.0.113.200"],
    ])
    result = dns.apply_bulk_import(
        zone_id="Z0861165ABC", file_content="", file_content_b64=b64,
        sr_number="SR-600", actor_email="a@b.com",
    )
    assert result["counts"]["created"] == 1
    assert result["counts"]["modified"] == 1
    assert result["counts"]["error"] == 0
    actions = {c["ChangeBatch"]["Changes"][0]["Action"] for c in fake.change_calls}
    assert actions == {"CREATE", "UPSERT"}
