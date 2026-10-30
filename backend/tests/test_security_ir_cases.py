"""Native AWS Security Incident Response (SIR) cases in the "AWS Cases"
page: read via the `security-ir` API (not the Support API), in
SECURITY_IR_REGION, read-only."""
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from botocore.exceptions import ClientError
from fastapi import HTTPException

from app import escalation

CASE_ID = "1978590314"


def _dt(day):
    return datetime(2026, 10, day, 14, 43, 18, 123000, tzinfo=timezone.utc)


def _list_item(case_id=CASE_ID, title="[Proactive case] UnauthorizedAccess:IAMUser/InstanceCredentialExfiltration", status="Detection and Analysis", day=4):
    return {
        "caseId": case_id, "title": title, "caseStatus": status, "engagementType": "Investigation",
        "resolverType": "AWS", "pendingAction": "Customer", "createdDate": _dt(day), "lastUpdatedDate": _dt(day + 1),
        "caseArn": f"arn:aws:security-ir:ap-southeast-1:100000000001:case/{case_id}",
    }


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": "arn:aws:iam::123:user/x secret detail"}}, "ListCases")


@pytest.fixture
def sir_client():
    c = MagicMock()
    with patch.object(escalation, "_security_ir_client", return_value=c):
        yield c


# ── escalation.py ────────────────────────────────────────────────────────────

def test_region_defaults_to_the_region_sir_is_active_in():
    assert escalation.SECURITY_IR_REGION == "ap-southeast-1"


def test_list_shapes_rows_like_the_existing_cases_list(sir_client):
    sir_client.list_cases.return_value = {"items": [_list_item()]}
    (row,) = escalation.list_security_ir_cases()
    assert row["case_id"] == CASE_ID and row["display_id"] == CASE_ID
    assert row["account"] == "ams" and row["source"] == "security_ir"
    assert row["case_type"] == "AWS Security Incident Response"
    assert row["subject"].startswith("[Proactive case]")
    assert row["status"] == "Detection and Analysis"
    assert row["time_created"] == "2026-10-04T14:43:18.123Z"
    assert row["pending_action"] == "Customer" and row["resolver_type"] == "AWS"
    assert row["service_code"] is None


def test_list_pages_with_the_api_maximum_page_size(sir_client):
    sir_client.list_cases.side_effect = [
        {"items": [_list_item("1")], "nextToken": "t1"},
        {"items": [_list_item("2")]},
    ]
    rows = escalation.list_security_ir_cases()
    assert [r["case_id"] for r in rows] == ["1", "2"]
    first, second = sir_client.list_cases.call_args_list
    assert first.kwargs == {"maxResults": 25}  # the API rejects anything above 25
    assert second.kwargs == {"maxResults": 25, "nextToken": "t1"}


def test_list_never_loops_forever_on_a_stuck_next_token(sir_client):
    sir_client.list_cases.return_value = {"items": [], "nextToken": "same"}
    escalation.list_security_ir_cases()
    assert sir_client.list_cases.call_count == escalation._SECURITY_IR_MAX_PAGES


@pytest.mark.parametrize("code,fragment", [
    ("AccessDeniedException", "AWSSecurityIncidentResponseReadOnlyAccess"),
    ("SecurityIncidentResponseNotActiveException", "not active"),
    ("ThrottlingException", "Could not reach"),
])
def test_list_errors_become_safe_messages_without_aws_details(sir_client, code, fragment):
    sir_client.list_cases.side_effect = _client_error(code)
    with pytest.raises(escalation.EscalationError) as exc:
        escalation.list_security_ir_cases()
    assert fragment in exc.value.message
    assert "arn:aws:iam" not in exc.value.message and "secret detail" not in exc.value.message


def test_detail_includes_description_fields_and_ordered_comments(sir_client):
    sir_client.get_case.return_value = {
        "title": "T", "description": "Something happened", "caseStatus": "Detection and Analysis",
        "engagementType": "Investigation", "pendingAction": "Customer", "resolverType": "AWS",
        "createdDate": _dt(4), "lastUpdatedDate": _dt(5), "reportedIncidentStartDate": _dt(3),
        "impactedAccounts": ["100000000001"], "impactedServices": ["EC2", "IAM"],
        "impactedAwsRegions": [{"region": "ap-southeast-1"}], "caseAttachments": [{"attachmentId": "a"}],
    }
    sir_client.list_comments.return_value = {"items": [
        {"body": "second", "creator": "AWS Responder", "createdDate": _dt(6)},
        {"body": "first", "creator": "AWS Responder", "createdDate": _dt(5)},
    ]}
    d = escalation.get_security_ir_case_detail(CASE_ID)
    assert d["source"] == "security_ir" and d["account"] == "ams" and d["display_id"] == CASE_ID
    assert d["description"] == "Something happened"
    assert d["impacted_accounts"] == ["100000000001"] and d["impacted_services"] == ["EC2", "IAM"]
    assert d["impacted_regions"] == ["ap-southeast-1"] and d["attachment_count"] == 1
    assert d["pending_action"] == "Customer"
    assert [c["body"] for c in d["communications"]] == ["first", "second"]
    assert d["communications"][0]["submitted_by"] == "AWS Responder"
    assert "ap-southeast-1.console.aws.amazon.com/security-ir" in d["console_url"]
    sir_client.get_case.assert_called_once_with(caseId=CASE_ID)
    assert sir_client.list_comments.call_args.kwargs == {"caseId": CASE_ID, "maxResults": 25}


@pytest.mark.parametrize("creator,expected", [
    ("arn:aws:iam::100000000001:user/q_admin", "q_admin"),
    ("arn:aws:sts::100000000001:assumed-role/Admin/jane", "jane"),
    ("AWS Responder", "AWS Responder"),
    (None, None),
    ("", ""),
])
def test_comment_author_arns_are_shortened_to_the_user_name(creator, expected):
    assert escalation._friendly_creator(creator) == expected


def test_detail_with_sparse_case_does_not_crash(sir_client):
    sir_client.get_case.return_value = {"title": "T", "caseStatus": "Closed", "createdDate": _dt(4)}
    sir_client.list_comments.return_value = {"items": []}
    d = escalation.get_security_ir_case_detail(CASE_ID)
    assert d["impacted_accounts"] == [] and d["impacted_regions"] == [] and d["communications"] == []
    assert d["closed_date"] is None and d["attachment_count"] == 0


def test_detail_unknown_case_gives_not_found_message(sir_client):
    sir_client.get_case.side_effect = _client_error("ResourceNotFoundException")
    with pytest.raises(escalation.EscalationError) as exc:
        escalation.get_security_ir_case_detail("nope")
    assert "not found" in exc.value.message


# ── main.py routes ───────────────────────────────────────────────────────────

@pytest.fixture
def main_prod(monkeypatch):
    from app import main
    monkeypatch.setattr(main, "ENV", "production")
    monkeypatch.setattr(main.job_store, "build_case_to_job_map", lambda: {})
    monkeypatch.setattr(main.health_ack_store, "build_case_to_health_event_map", lambda: {})
    monkeypatch.setattr(main.standalone_case_store, "list_cases", lambda: [])
    return main


def _support_case(cid, code, created):
    return {"caseId": cid, "displayId": cid, "subject": "s", "status": "resolved", "serviceCode": code, "timeCreated": created}


def test_list_route_merges_sir_cases_with_support_cases(main_prod):
    sir_row = {
        "case_id": CASE_ID, "account": "ams", "source": "security_ir", "case_type": "AWS Security Incident Response",
        "subject": "x", "status": "Detection and Analysis", "service_code": None, "display_id": CASE_ID,
        "time_created": "2026-10-04T14:43:18.123Z",
    }
    with patch.object(escalation, "list_aws_support_cases", return_value=[_support_case("a", "x", "2026-01-01T00:00:00.000Z")]), \
         patch.object(escalation, "list_aws_ams_cases", return_value=[_support_case("b", "y", "2026-02-01T00:00:00.000Z")]), \
         patch.object(escalation, "list_security_ir_cases", return_value=[sir_row]):
        out = asyncio.run(main_prod.list_cases(actor="a@b.c"))
    assert out["count"] == 3
    assert out["cases"][0]["case_id"] == CASE_ID  # newest first
    assert out["cases"][0]["source"] == "security_ir"
    assert "partial_errors" not in out


def test_sir_failure_only_adds_a_banner_and_keeps_support_cases(main_prod):
    with patch.object(escalation, "list_aws_support_cases", return_value=[_support_case("a", "x", "2026-01-01T00:00:00.000Z")]), \
         patch.object(escalation, "list_aws_ams_cases", return_value=[]), \
         patch.object(escalation, "list_security_ir_cases", side_effect=escalation.EscalationError("aws_ams", "Security Incident Response: no permission")):
        out = asyncio.run(main_prod.list_cases(actor="a@b.c"))
    assert out["count"] == 1
    assert out["partial_errors"] == [{"account": "ams", "message": "Security Incident Response: no permission"}]


def test_sir_unexpected_exception_is_hidden_behind_generic_message(main_prod):
    with patch.object(escalation, "list_aws_support_cases", return_value=[]), \
         patch.object(escalation, "list_aws_ams_cases", return_value=[_support_case("b", "y", "2026-02-01T00:00:00.000Z")]), \
         patch.object(escalation, "list_security_ir_cases", side_effect=RuntimeError("boom arn:aws:iam::1:user/x")):
        out = asyncio.run(main_prod.list_cases(actor="a@b.c"))
    assert out["partial_errors"] == [{"account": "ams", "message": "Could not list Security Incident Response cases"}]


def test_detail_route_returns_sir_case_with_no_job_or_reply_links(main_prod):
    detail = {"case_id": CASE_ID, "source": "security_ir", "account": "ams", "subject": "T", "communications": []}
    with patch.object(escalation, "get_security_ir_case_detail", return_value=detail) as get:
        out = asyncio.run(main_prod.get_case(CASE_ID, account="ams", source="security_ir", actor="a@b.c"))
    get.assert_called_once_with(CASE_ID)
    assert out["source"] == "security_ir"
    assert out["linked_job"] is None and out["linked_health_event"] is None and out["raised_by_name"] is None


@pytest.mark.parametrize("account,source", [("own", "security_ir"), ("ams", "something-else")])
def test_detail_route_rejects_bad_source_or_account(main_prod, account, source):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(main_prod.get_case(CASE_ID, account=account, source=source, actor="a@b.c"))
    assert exc.value.status_code == 400


def test_detail_route_maps_backend_errors_to_502(main_prod):
    with patch.object(escalation, "get_security_ir_case_detail", side_effect=escalation.EscalationError("aws_ams", "nope")):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main_prod.get_case(CASE_ID, account="ams", source="security_ir", actor="a@b.c"))
    assert exc.value.status_code == 502 and exc.value.detail == "nope"


# ── Replying to a native SIR case (mock only — never posts to a real case) ───

def test_comment_is_prefixed_with_the_real_portal_user_and_uses_an_idempotency_token(sir_client):
    sir_client.create_case_comment.return_value = {"commentId": "c-1"}
    out = escalation.add_security_ir_comment(CASE_ID, "  We rotated the key.  ", "jane@example.com")
    assert out == {"comment_id": "c-1"}
    kw = sir_client.create_case_comment.call_args.kwargs
    assert kw["caseId"] == CASE_ID
    assert kw["body"] == "[Posted via Cloud Infra Portal by jane@example.com]\n\nWe rotated the key."
    assert len(kw["clientToken"]) >= 32  # uuid4 -> boto retries can't double-post


def test_each_comment_gets_a_fresh_client_token(sir_client):
    sir_client.create_case_comment.return_value = {"commentId": "c"}
    escalation.add_security_ir_comment(CASE_ID, "a", "x@y.z")
    escalation.add_security_ir_comment(CASE_ID, "a", "x@y.z")
    t1, t2 = (c.kwargs["clientToken"] for c in sir_client.create_case_comment.call_args_list)
    assert t1 != t2


@pytest.mark.parametrize("text", ["", "   ", None])
def test_empty_comment_is_rejected_before_any_aws_call(sir_client, text):
    with pytest.raises(ValueError):
        escalation.add_security_ir_comment(CASE_ID, text, "x@y.z")
    sir_client.create_case_comment.assert_not_called()


def test_over_long_comment_is_rejected_not_silently_truncated(sir_client):
    with pytest.raises(ValueError):
        escalation.add_security_ir_comment(CASE_ID, "a" * escalation.SECURITY_IR_COMMENT_MAX, "x@y.z")
    sir_client.create_case_comment.assert_not_called()


def test_comment_at_the_limit_including_the_author_line_is_accepted(sir_client):
    sir_client.create_case_comment.return_value = {"commentId": "c"}
    prefix = "[Posted via Cloud Infra Portal by x@y.z]\n\n"
    escalation.add_security_ir_comment(CASE_ID, "a" * (escalation.SECURITY_IR_COMMENT_MAX - len(prefix)), "x@y.z")
    assert len(sir_client.create_case_comment.call_args.kwargs["body"]) == escalation.SECURITY_IR_COMMENT_MAX


@pytest.mark.parametrize("code,fragment", [
    ("AccessDeniedException", "security-ir:CreateCaseComment"),
    ("ConflictException", "may be closed"),
    ("ValidationException", "may be closed"),
    ("ThrottlingException", "Could not reach"),
])
def test_comment_errors_are_safe_and_specific(sir_client, code, fragment):
    sir_client.create_case_comment.side_effect = _client_error(code)
    with pytest.raises(escalation.EscalationError) as exc:
        escalation.add_security_ir_comment(CASE_ID, "hi", "x@y.z")
    assert fragment in exc.value.message
    assert "arn:aws:iam" not in exc.value.message and "secret detail" not in exc.value.message


def test_read_permission_error_message_is_unchanged_for_listing(sir_client):
    sir_client.list_cases.side_effect = _client_error("AccessDeniedException")
    with pytest.raises(escalation.EscalationError) as exc:
        escalation.list_security_ir_cases()
    assert "ReadOnlyAccess" in exc.value.message


def test_reply_route_posts_as_the_acting_user(main_prod):
    with patch.object(escalation, "add_security_ir_comment", return_value={"comment_id": "c-9"}) as add:
        out = asyncio.run(main_prod.reply_to_security_ir_case(
            CASE_ID, main_prod.SecurityIrReplyBody(body="  hello  "), actor="jane@example.com"))
    add.assert_called_once_with(CASE_ID, "hello", "jane@example.com")
    assert out == {"ok": True, "comment_id": "c-9"}


def test_reply_route_rejects_blank_body_with_400(main_prod):
    with patch.object(escalation, "add_security_ir_comment") as add:
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main_prod.reply_to_security_ir_case(CASE_ID, main_prod.SecurityIrReplyBody(body=" "), actor="a@b.c"))
    assert exc.value.status_code == 400
    add.assert_not_called()


def test_reply_route_maps_validation_to_400_and_aws_failure_to_502(main_prod):
    with patch.object(escalation, "add_security_ir_comment", side_effect=ValueError("Comment is too long")):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main_prod.reply_to_security_ir_case(CASE_ID, main_prod.SecurityIrReplyBody(body="x"), actor="a@b.c"))
    assert exc.value.status_code == 400 and "too long" in exc.value.detail
    with patch.object(escalation, "add_security_ir_comment", side_effect=escalation.EscalationError("aws_ams", "nope")):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main_prod.reply_to_security_ir_case(CASE_ID, main_prod.SecurityIrReplyBody(body="x"), actor="a@b.c"))
    assert exc.value.status_code == 502 and exc.value.detail == "nope"


def test_reply_route_hides_unexpected_error_details(main_prod):
    with patch.object(escalation, "add_security_ir_comment", side_effect=RuntimeError("boom arn:aws:iam::1:user/x")):
        with pytest.raises(HTTPException) as exc:
            asyncio.run(main_prod.reply_to_security_ir_case(CASE_ID, main_prod.SecurityIrReplyBody(body="x"), actor="a@b.c"))
    assert exc.value.status_code == 502 and "arn:" not in exc.value.detail


def test_reply_route_requires_the_cases_write_grant():
    """The route's dependency must be require_tab_write("cases") (same as
    creating a case), not plain get_actor — a reply on a live security
    incident goes out under the shared infra_portal identity."""
    import inspect
    from app import main
    route = next(r for r in main.app.routes if getattr(r, "path", "") == "/api/cases/{case_id}/security-ir-reply")
    dep_names = [d.call.__qualname__ for d in route.dependant.dependencies]
    assert any("require_tab_write" in n for n in dep_names), dep_names
    assert all(n != "get_actor" for n in dep_names)
