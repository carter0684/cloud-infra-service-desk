"""
Tests for app/screensaver.py — the Helpdesk Team catalogue's
S3-ScreenSaver tool: backend-proxy upload (upload_screensaver_file) that
writes bytes straight to the dedicated screensaver-100000000001 bucket
in account 100000000001 (ap-southeast-1), then republishes the object as
the site-wide latest/current file.

No real AWS calls: the S3 client and Secrets Manager client are both
monkeypatched with lightweight fakes, same style as test_cmdb.py's
FakeEc2Topology/FakeLogsClient fakes.
"""

import datetime as _dt
import json

import pytest

import app.screensaver as screensaver_module


@pytest.fixture(autouse=True)
def _reset_s3_client_singleton(monkeypatch):
    """screensaver.py caches its S3 and CloudFront clients in
    module-level globals (_s3_client / _cloudfront_client) exactly like
    note_attachments.py — reset both before and after every test so one
    test's fake client never leaks into another, and so credential-
    related tests actually exercise _get_screensaver_credentials() again
    instead of reusing a cached client built by an earlier test."""
    monkeypatch.setattr(screensaver_module, "_s3_client", None)
    monkeypatch.setattr(screensaver_module, "_cloudfront_client", None)
    yield
    monkeypatch.setattr(screensaver_module, "_s3_client", None)
    monkeypatch.setattr(screensaver_module, "_cloudfront_client", None)


class FakeS3Client:
    """Fake S3/CloudFront client covering put_object, generate_presigned_url,
    copy_object, and create_invalidation — every AWS call screensaver.py
    makes. boto3.client() is monkeypatched to return this same fake
    regardless of service name (see _patch_s3_client()), so it doubles as
    the fake CloudFront client too."""

    def __init__(self, put_object_error=None, presign_error=None,
                 copy_object_error=None, invalidation_error=None,
                 head_object_error=None, list_objects_pages=None,
                 delete_object_error=None):
        self._put_object_error = put_object_error
        self._presign_error = presign_error
        self._copy_object_error = copy_object_error
        self._invalidation_error = invalidation_error
        self._head_object_error = head_object_error
        # list_objects_pages: optional list of response dicts to return in
        # sequence across successive list_objects_v2() calls, for testing
        # pagination (ContinuationToken handling). Defaults to a single
        # empty page below if not provided.
        self._list_objects_pages = list(list_objects_pages) if list_objects_pages is not None else [
            {"Contents": [], "IsTruncated": False}
        ]
        self._delete_object_error = delete_object_error
        self.put_object_calls = []
        self.presign_calls = []
        self.copy_object_calls = []
        self.invalidation_calls = []
        self.head_object_calls = []
        self.list_objects_calls = []
        self.delete_object_calls = []

    def put_object(self, **kwargs):
        self.put_object_calls.append(kwargs)
        if self._put_object_error:
            raise self._put_object_error
        return {}

    def generate_presigned_url(self, operation_name, Params, ExpiresIn):
        self.presign_calls.append({"operation_name": operation_name, "Params": Params, "ExpiresIn": ExpiresIn})
        if self._presign_error:
            raise self._presign_error
        return f"https://screensaver-100000000001.s3.ap-southeast-1.amazonaws.com/{Params['Key']}?X-Amz-Signature=fake"

    def copy_object(self, **kwargs):
        self.copy_object_calls.append(kwargs)
        if self._copy_object_error:
            raise self._copy_object_error
        return {}

    def create_invalidation(self, **kwargs):
        self.invalidation_calls.append(kwargs)
        if self._invalidation_error:
            raise self._invalidation_error
        return {"Invalidation": {"Id": "FAKEINVAL"}}

    def head_object(self, **kwargs):
        self.head_object_calls.append(kwargs)
        if self._head_object_error:
            raise self._head_object_error
        return {"ETag": '"latest-etag"'}

    def list_objects_v2(self, **kwargs):
        self.list_objects_calls.append(kwargs)
        idx = len(self.list_objects_calls) - 1
        if idx >= len(self._list_objects_pages):
            return {"Contents": [], "IsTruncated": False}
        return self._list_objects_pages[idx]

    def delete_object(self, **kwargs):
        self.delete_object_calls.append(kwargs)
        if self._delete_object_error:
            raise self._delete_object_error
        return {}


def _patch_s3_client(monkeypatch, fake_client):
    monkeypatch.setattr(screensaver_module, "_get_screensaver_credentials",
                         lambda: {"aws_access_key_id": "AKIAFAKE", "aws_secret_access_key": "fakefakefake"})
    monkeypatch.setattr(screensaver_module.boto3, "client", lambda *a, **k: fake_client)


def test_get_screensaver_credentials_uses_env_vars_when_present(monkeypatch):
    monkeypatch.setenv("SCREENSAVER_ACCESS_KEY_ID", "AKIAENV")
    monkeypatch.setenv("SCREENSAVER_SECRET_ACCESS_KEY", "envsecret")
    creds = screensaver_module._get_screensaver_credentials()
    assert creds == {"aws_access_key_id": "AKIAENV", "aws_secret_access_key": "envsecret"}


def test_get_screensaver_credentials_falls_back_to_secrets_manager(monkeypatch):
    monkeypatch.delenv("SCREENSAVER_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("SCREENSAVER_SECRET_ACCESS_KEY", raising=False)
    secret_json = json.dumps({"aws_access_key_id": "AKIASECRET", "aws_secret_access_key": "secretsecret"})
    monkeypatch.setattr(screensaver_module, "_get_secret", lambda name: secret_json)
    creds = screensaver_module._get_screensaver_credentials()
    assert creds == {"aws_access_key_id": "AKIASECRET", "aws_secret_access_key": "secretsecret"}


def test_get_screensaver_credentials_raises_when_secret_missing_keys(monkeypatch):
    monkeypatch.delenv("SCREENSAVER_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("SCREENSAVER_SECRET_ACCESS_KEY", raising=False)
    monkeypatch.setattr(screensaver_module, "_get_secret", lambda name: json.dumps({"aws_access_key_id": "only-one-key"}))
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module._get_screensaver_credentials()


def test_get_screensaver_credentials_raises_when_secrets_manager_fails(monkeypatch):
    monkeypatch.delenv("SCREENSAVER_ACCESS_KEY_ID", raising=False)
    monkeypatch.delenv("SCREENSAVER_SECRET_ACCESS_KEY", raising=False)
    def _raise(name):
        raise RuntimeError("secrets manager down")
    monkeypatch.setattr(screensaver_module, "_get_secret", _raise)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module._get_screensaver_credentials()


def test_safe_filename_strips_path_and_unsafe_characters():
    assert screensaver_module._safe_filename("../../etc/passwd") == "passwd"
    assert screensaver_module._safe_filename("my cool file!@#.png") == "my_cool_file___.png"
    assert screensaver_module._safe_filename("") == "file"
    assert screensaver_module._safe_filename(None) == "file"


def test_upload_screensaver_file_writes_object_and_returns_expected_key_shape(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)

    result = screensaver_module.upload_screensaver_file(
        filename="my screensaver.png", content_type="image/png", content=b"fake-bytes",
        uploaded_by_email="user@example.com",
    )

    assert result["bucket"] == screensaver_module.SCREENSAVER_BUCKET_NAME
    assert result["region"] == screensaver_module.SCREENSAVER_BUCKET_REGION
    assert result["s3_key"].startswith("screensaver/")
    assert result["s3_key"].endswith("-my_screensaver.png")
    assert result["s3_path"] == f"s3://{screensaver_module.SCREENSAVER_BUCKET_NAME}/{result['s3_key']}"
    assert result["content_type"] == "image/png"
    assert result["size_bytes"] == len(b"fake-bytes")
    assert result["uploaded_by_email"] == "user@example.com"
    assert result["public_url"] == (
        f"https://{screensaver_module.SCREENSAVER_CLOUDFRONT_DOMAIN}/{screensaver_module.SCREENSAVER_LATEST_KEY}"
    )

    # put_object must be scoped to exactly the returned key/bucket, with
    # the raw bytes and content-type passed straight through.
    call = fake.put_object_calls[0]
    assert call["Bucket"] == screensaver_module.SCREENSAVER_BUCKET_NAME
    assert call["Key"] == result["s3_key"]
    assert call["Body"] == b"fake-bytes"
    assert call["ContentType"] == "image/png"


def test_upload_screensaver_file_defaults_content_type_when_omitted(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    result = screensaver_module.upload_screensaver_file(
        filename="file.bin", content_type=None, content=b"x", uploaded_by_email="a@b.com"
    )
    assert result["content_type"] == "application/octet-stream"
    assert fake.put_object_calls[0]["ContentType"] == "application/octet-stream"


def test_upload_screensaver_file_rejects_empty_filename(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.upload_screensaver_file(filename="", content_type=None, content=b"x", uploaded_by_email="a@b.com")
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.upload_screensaver_file(filename="   ", content_type=None, content=b"x", uploaded_by_email="a@b.com")


def test_upload_screensaver_file_rejects_empty_content(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.upload_screensaver_file(filename="file.png", content_type=None, content=b"", uploaded_by_email="a@b.com")
    assert fake.put_object_calls == []


def test_upload_screensaver_file_rejects_content_over_max_bytes(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    monkeypatch.setattr(screensaver_module, "MAX_UPLOAD_BYTES", 10)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.upload_screensaver_file(
            filename="file.png", content_type=None, content=b"x" * 11, uploaded_by_email="a@b.com"
        )
    assert fake.put_object_calls == []


def test_upload_screensaver_file_two_calls_never_collide_on_key(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    r1 = screensaver_module.upload_screensaver_file(filename="same.png", content_type=None, content=b"a", uploaded_by_email="a@b.com")
    r2 = screensaver_module.upload_screensaver_file(filename="same.png", content_type=None, content=b"b", uploaded_by_email="a@b.com")
    assert r1["s3_key"] != r2["s3_key"]


def test_upload_screensaver_file_wraps_put_object_failure_in_screensaver_error(monkeypatch):
    fake = FakeS3Client(put_object_error=RuntimeError("s3 down"))
    _patch_s3_client(monkeypatch, fake)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.upload_screensaver_file(filename="file.png", content_type=None, content=b"x", uploaded_by_email="a@b.com")


def test_upload_screensaver_file_publishes_object_as_latest_and_invalidates_cloudfront(monkeypatch):
    """Core design-"B" behavior: a successful upload_screensaver_file()
    call must copy the object onto the fixed latest/current key and
    invalidate that exact CloudFront path, so the stable public URL
    reflects this newest upload."""
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)

    result = screensaver_module.upload_screensaver_file(
        filename="newpic.jpg", content_type="image/jpeg", content=b"jpeg-bytes", uploaded_by_email="a@b.com"
    )

    assert len(fake.copy_object_calls) == 1
    copy_call = fake.copy_object_calls[0]
    assert copy_call["Bucket"] == screensaver_module.SCREENSAVER_BUCKET_NAME
    assert copy_call["Key"] == screensaver_module.SCREENSAVER_LATEST_KEY
    assert copy_call["CopySource"] == {
        "Bucket": screensaver_module.SCREENSAVER_BUCKET_NAME,
        "Key": result["s3_key"],
    }
    assert copy_call["ContentType"] == "image/jpeg"
    assert copy_call["MetadataDirective"] == "REPLACE"

    assert len(fake.invalidation_calls) == 1
    inval_call = fake.invalidation_calls[0]
    assert inval_call["DistributionId"] == screensaver_module.SCREENSAVER_CLOUDFRONT_DISTRIBUTION_ID
    assert inval_call["InvalidationBatch"]["Paths"]["Items"] == [f"/{screensaver_module.SCREENSAVER_LATEST_KEY}"]


def test_upload_screensaver_file_still_succeeds_when_publish_as_latest_copy_fails(monkeypatch):
    """The "publish as latest" step is best-effort — a CopyObject failure
    must not fail the whole upload_screensaver_file() call, since the
    user's file already safely landed under its own dated key (the
    put_object() above already succeeded)."""
    fake = FakeS3Client(copy_object_error=RuntimeError("copy failed"))
    _patch_s3_client(monkeypatch, fake)

    result = screensaver_module.upload_screensaver_file(
        filename="file.png", content_type=None, content=b"x", uploaded_by_email="a@b.com"
    )

    assert result["s3_key"].startswith("screensaver/")
    assert len(fake.put_object_calls) == 1
    assert fake.invalidation_calls == []  # never reached since copy failed first


def test_upload_screensaver_file_still_succeeds_when_invalidation_fails(monkeypatch):
    """A CloudFront invalidation failure is also best-effort/non-fatal —
    the object is already updated in S3, so a stale edge cache just
    resolves on its own within the normal TTL."""
    fake = FakeS3Client(invalidation_error=RuntimeError("cloudfront down"))
    _patch_s3_client(monkeypatch, fake)

    result = screensaver_module.upload_screensaver_file(
        filename="file.png", content_type=None, content=b"x", uploaded_by_email="a@b.com"
    )

    assert result["s3_key"].startswith("screensaver/")
    assert len(fake.copy_object_calls) == 1


def test_generate_download_url_rejects_key_outside_screensaver_prefix(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.generate_download_url("notes/other/file.png")


def test_generate_download_url_returns_presigned_get_url(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    url = screensaver_module.generate_download_url("screensaver/2026/08/25/file.png")
    assert url.startswith("https://")
    call = fake.presign_calls[0]
    assert call["operation_name"] == "get_object"
    assert call["Params"] == {"Bucket": screensaver_module.SCREENSAVER_BUCKET_NAME, "Key": "screensaver/2026/08/25/file.png"}
    assert call["ExpiresIn"] == 300


def test_publish_as_latest_sets_no_cache_control_header(monkeypatch):
    """Belt-and-suspenders no-cache header on the `latest/current` object
    itself, alongside the CloudFront invalidation — see
    _publish_as_latest()'s docstring for why."""
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    screensaver_module.upload_screensaver_file(
        filename="file.png", content_type="image/png", content=b"x", uploaded_by_email="a@b.com"
    )
    copy_call = fake.copy_object_calls[0]
    assert copy_call["CacheControl"] == "no-cache, max-age=0, must-revalidate"


def test_list_uploaded_files_excludes_latest_current_key_and_flags_is_current(monkeypatch):
    fake = FakeS3Client(list_objects_pages=[{
        "Contents": [
            {"Key": "screensaver/latest/current", "Size": 10, "LastModified": _dt.datetime(2026, 8, 27), "ETag": '"latest-etag"'},
            {"Key": "screensaver/2026/08/26/old-file.jpg", "Size": 20, "LastModified": _dt.datetime(2026, 8, 26), "ETag": '"old-etag"'},
            {"Key": "screensaver/2026/08/27/uuid-new-file.jpg", "Size": 30, "LastModified": _dt.datetime(2026, 8, 27, 1), "ETag": '"latest-etag"'},
        ],
        "IsTruncated": False,
    }])
    _patch_s3_client(monkeypatch, fake)

    files = screensaver_module.list_uploaded_files()

    keys = [f["s3_key"] for f in files]
    assert screensaver_module.SCREENSAVER_LATEST_KEY not in keys
    assert "screensaver/2026/08/26/old-file.jpg" in keys
    assert "screensaver/2026/08/27/uuid-new-file.jpg" in keys

    by_key = {f["s3_key"]: f for f in files}
    assert by_key["screensaver/2026/08/27/uuid-new-file.jpg"]["is_current"] is True
    assert by_key["screensaver/2026/08/26/old-file.jpg"]["is_current"] is False
    # newest first
    assert files[0]["s3_key"] == "screensaver/2026/08/27/uuid-new-file.jpg"


def test_list_uploaded_files_paginates_across_multiple_pages(monkeypatch):
    fake = FakeS3Client(list_objects_pages=[
        {
            "Contents": [{"Key": "screensaver/2026/08/25/a.jpg", "Size": 1, "LastModified": _dt.datetime(2026, 8, 25), "ETag": '"e1"'}],
            "IsTruncated": True,
            "NextContinuationToken": "token-1",
        },
        {
            "Contents": [{"Key": "screensaver/2026/08/26/b.jpg", "Size": 2, "LastModified": _dt.datetime(2026, 8, 26), "ETag": '"e2"'}],
            "IsTruncated": False,
        },
    ])
    _patch_s3_client(monkeypatch, fake)

    files = screensaver_module.list_uploaded_files()

    assert len(files) == 2
    assert len(fake.list_objects_calls) == 2
    assert "ContinuationToken" not in fake.list_objects_calls[0]
    assert fake.list_objects_calls[1]["ContinuationToken"] == "token-1"


def test_list_uploaded_files_downgrades_to_not_current_when_head_object_fails(monkeypatch):
    """head_object() looking up latest/current's ETag is best-effort —
    if it fails (e.g. latest/current doesn't exist yet), every entry
    should just come back is_current=False rather than raising."""
    fake = FakeS3Client(
        head_object_error=RuntimeError("not found"),
        list_objects_pages=[{
            "Contents": [{"Key": "screensaver/2026/08/26/a.jpg", "Size": 1, "LastModified": _dt.datetime(2026, 8, 26), "ETag": '"e1"'}],
            "IsTruncated": False,
        }],
    )
    _patch_s3_client(monkeypatch, fake)

    files = screensaver_module.list_uploaded_files()

    assert len(files) == 1
    assert files[0]["is_current"] is False


def test_list_uploaded_files_wraps_list_objects_failure_in_screensaver_error(monkeypatch):
    class FailingListClient(FakeS3Client):
        def list_objects_v2(self, **kwargs):
            raise RuntimeError("s3 list down")
    fake = FailingListClient()
    _patch_s3_client(monkeypatch, fake)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.list_uploaded_files()


def test_delete_uploaded_file_calls_delete_object_with_correct_key(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    screensaver_module.delete_uploaded_file("screensaver/2026/08/26/old-file.jpg")
    assert fake.delete_object_calls == [{
        "Bucket": screensaver_module.SCREENSAVER_BUCKET_NAME,
        "Key": "screensaver/2026/08/26/old-file.jpg",
    }]


def test_delete_uploaded_file_refuses_latest_current_key(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.delete_uploaded_file(screensaver_module.SCREENSAVER_LATEST_KEY)
    assert fake.delete_object_calls == []


def test_delete_uploaded_file_refuses_key_outside_screensaver_prefix(monkeypatch):
    fake = FakeS3Client()
    _patch_s3_client(monkeypatch, fake)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.delete_uploaded_file("notes/other/file.png")
    assert fake.delete_object_calls == []


def test_delete_uploaded_file_wraps_delete_object_failure_in_screensaver_error(monkeypatch):
    fake = FakeS3Client(delete_object_error=RuntimeError("access denied"))
    _patch_s3_client(monkeypatch, fake)
    with pytest.raises(screensaver_module.ScreenSaverError):
        screensaver_module.delete_uploaded_file("screensaver/2026/08/26/old-file.jpg")


def test_bucket_region_is_ap_southeast_1_not_us_east_1():
    """Explicit regression guard for the design decision that this
    bucket's region must be independent of AWS_REGION (which stays
    us-east-1 for Secrets Manager / everything else in this codebase)."""
    assert screensaver_module.SCREENSAVER_BUCKET_REGION == "ap-southeast-1"
    assert screensaver_module.SCREENSAVER_ACCOUNT_ID == "100000000001"
