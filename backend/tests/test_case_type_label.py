"""describe_case_type() labels for the "All AWS Cases" list, including the
AWS Security Incident Response (SIR) service code that drives the SIR tab."""
import pytest

from app.escalation import (
    AMS_SERVICE_CODE_INCIDENT,
    AMS_SERVICE_CODE_SR,
    SIR_SERVICE_CODE,
    describe_case_type,
)


def test_sir_service_code_matches_what_aws_actually_returns():
    # Observed on the AMS account via support:DescribeCases.
    assert SIR_SERVICE_CODE == "service-security-incident-response"


@pytest.mark.parametrize("account", ["ams", "own"])
def test_sir_cases_get_their_own_label_on_either_account(account):
    assert describe_case_type(account, SIR_SERVICE_CODE) == "AWS Security Incident Response"


def test_existing_ams_labels_are_unchanged():
    assert describe_case_type("ams", AMS_SERVICE_CODE_SR) == "AMS Service Request"
    assert describe_case_type("ams", AMS_SERVICE_CODE_INCIDENT) == "AMS Incident"
    assert describe_case_type("ams", "amazon-cloudwatch") == "AMS Case"
    assert describe_case_type("ams", None) == "AMS Case"


def test_existing_own_account_label_is_unchanged():
    assert describe_case_type("own", "amazon-cloudwatch") == "AWS Support"
    assert describe_case_type("own", None) == "AWS Support"
