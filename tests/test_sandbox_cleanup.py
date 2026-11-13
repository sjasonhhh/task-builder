from __future__ import annotations

import asyncio
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from task_builder import sandbox_runtime as runtime
from task_builder.sandbox_runtime import SandboxClient, SandboxError


def fake_client(monkeypatch, details, pages=(), delete_error=None):
    client = SandboxClient({"BASE_URL": "http://platform.test", "TASK_BUILDER_SANDBOX_RESOURCE": "pool",
                            "AD_USERNAME": "user", "AD_PASSWORD": "test-password"})
    details = iter(details)
    calls = []

    def request(method, path, **kwargs):
        url = urlsplit(path)
        query = parse_qs(url.query)
        assert query["resource"] == ["pool"]
        assert kwargs["deadline"] > 0
        if url.path == "/api/v1/personal-sandboxes":
            assert method == "GET"
            page = int(query["page"][0])
            calls.append(("list", page))
            value = pages[page - 1]
        else:
            assert url.path == "/api/v1/sandboxes/target"
            calls.append((method, "target"))
            if method == "DELETE":
                assert kwargs["retry"] is False
                value = delete_error or {}
            else:
                assert method == "GET"
                value = next(details)
        if isinstance(value, Exception):
            raise value
        return json.dumps(value).encode()

    monkeypatch.setattr(client, "request", request)
    return client, calls


def page(*ids, total=None):
    result = {"items": [{"id": sid} for sid in ids]}
    if total is not None:
        result["pagination"] = {"totalItems": total}
    return result


@pytest.mark.parametrize("phase", ["before-delete", "after-delete", "delete-error"])
def test_403_only_counts_as_deleted_when_list_confirms_absence(monkeypatch, phase):
    details = [SandboxError("forbidden", 403)]
    if phase != "before-delete":
        details.insert(0, {"state": "Running"})
    client, calls = fake_client(monkeypatch, details, [page(total=0)],
                               SandboxError("delete failed", 500) if phase == "delete-error" else None)
    client.delete("target")
    assert calls[-1] == ("list", 1)
    assert (("DELETE", "target") in calls) == (phase != "before-delete")


@pytest.mark.parametrize("phase", ["before-delete", "after-delete", "delete-error"])
def test_403_for_existing_instance_remains_a_failure(monkeypatch, phase):
    details = [SandboxError("forbidden", 403)]
    if phase != "before-delete":
        details.insert(0, {"state": "Running"})
    client, calls = fake_client(monkeypatch, details, [page("target", total=1)],
                               SandboxError("delete failed", 403) if phase == "delete-error" else None)
    with pytest.raises(SandboxError) as caught:
        client.delete("target")
    assert caught.value.status == 403
    assert calls[-1] == ("list", 1)


@pytest.mark.parametrize("target_on_second_page", [False, True])
def test_confirmation_checks_all_pages_even_if_server_returns_short_pages(monkeypatch, target_on_second_page):
    pages = [page("other-1", total=2), page("target" if target_on_second_page else "other-2", total=2)]
    client, calls = fake_client(monkeypatch, [SandboxError("forbidden", 403)], pages)
    if target_on_second_page:
        with pytest.raises(SandboxError) as caught:
            client.delete("target")
        assert caught.value.status == 403
    else:
        client.delete("target")
    assert calls == [("GET", "target"), ("list", 1), ("list", 2)]


def test_missing_total_requires_reading_until_empty_page(monkeypatch):
    client, calls = fake_client(monkeypatch, [SandboxError("forbidden", 403)],
                               [page("other-1"), page("other-2"), page()])
    client.delete("target")
    assert calls[-1] == ("list", 3)


@pytest.mark.parametrize("response", [SandboxError("list forbidden", 403),
                                      SandboxError("list unavailable", 503),
                                      SandboxError("list timed out")])
def test_failed_list_query_does_not_report_success(monkeypatch, response):
    client, _ = fake_client(monkeypatch, [SandboxError("forbidden", 403)], [response])
    with pytest.raises(SandboxError) as caught:
        client.delete("target")
    assert caught.value is response


@pytest.mark.parametrize("pages", [
    [{}],
    [{"items": None}],
    [{"items": [{}]}],
    [{"items": [{"id": None}]}],
    [{"items": [], "pagination": []}],
    [{"items": [], "pagination": {"totalItems": "0"}}],
    [{"items": [], "pagination": {"totalItems": -1}}],
    [{"items": [], "pagination": {"totalItems": True}}],
    [page("other", total=2), page(total=2)],
    [page("other", total=2), page("other", total=2)],
    [page("other", "other", total=2)],
    [page("other", total=0)],
])
def test_invalid_or_incomplete_list_cannot_confirm_absence(monkeypatch, pages):
    client, _ = fake_client(monkeypatch, [SandboxError("forbidden", 403)], pages)
    with pytest.raises(SandboxError, match="无法确认删除"):
        client.delete("target")


@pytest.mark.parametrize("after_delete", [False, True])
def test_404_still_confirms_absence_without_listing(monkeypatch, after_delete):
    details = [SandboxError("not found", 404)]
    if after_delete:
        details.insert(0, {"state": "Running"})
    client, calls = fake_client(monkeypatch, details)
    client.delete("target")
    assert not any(call[0] == "list" for call in calls)


def test_401_does_not_fall_back_to_listing(monkeypatch):
    client, calls = fake_client(monkeypatch, [SandboxError("unauthorized", 401)])
    with pytest.raises(SandboxError) as caught:
        client.delete("target")
    assert caught.value.status == 401
    assert calls == [("GET", "target")]


def test_logged_cleanup_records_success_after_confirmed_403_absence(monkeypatch, tmp_path):
    client, _ = fake_client(monkeypatch, [SandboxError("forbidden", 403)], [page(total=0)])
    monkeypatch.setattr(runtime, "SandboxClient", lambda: client)
    log = tmp_path / "tb-cleanup.jsonl"
    log.write_text(json.dumps({"backend": "sandbox", "event": "created", "name": "tb-cleanup",
                               "sandbox_id": "target", "base_url": client.base_url, "resource": "pool"}) + "\n")
    assert asyncio.run(runtime.cleanup_logged_sandboxes(tmp_path)) == []
    assert json.loads(log.read_text().splitlines()[-1])["event"] == "shutdown-complete"
