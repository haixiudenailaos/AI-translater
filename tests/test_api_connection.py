from __future__ import annotations

import json

import httpx

from src.api.base_api import BaseAPI


class _BodyThatTimesOut(httpx.SyncByteStream):
    def __iter__(self):
        raise httpx.ReadTimeout("response body was too slow")


def _make_api(handler, **overrides):
    api = BaseAPI(
        {
            "base_url": "https://example.invalid/v1",
            "api_key": "test-key",
            "model_name": "test-model",
            **overrides,
        }
    )
    api._current_client.close()
    api._current_client = httpx.Client(transport=httpx.MockTransport(handler))
    return api


def test_connection_accepts_200_without_reading_slow_response_body():
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, stream=_BodyThatTimesOut())

    api = _make_api(handler)
    try:
        assert api.test_connection() is True
        assert len(requests) == 1

        payload = json.loads(requests[0].content)
        assert payload["model"] == "test-model"
        assert payload["stream"] is False
    finally:
        api.close()


def test_connection_uses_phase_specific_timeout_instead_of_three_seconds():
    captured_timeout = {}

    def handler(request):
        captured_timeout.update(request.extensions["timeout"])
        return httpx.Response(200, stream=_BodyThatTimesOut())

    api = _make_api(
        handler,
        http_connect_timeout=7,
        http_read_timeout=180,
        connection_test_timeout=30,
    )
    try:
        assert api.test_connection() is True
        assert captured_timeout == {
            "connect": 7.0,
            "read": 30.0,
            "write": 30.0,
            "pool": 10.0,
        }
    finally:
        api.close()


def test_connection_reads_error_body_to_reject_unknown_model():
    def handler(_request):
        return httpx.Response(
            400,
            json={"error": {"message": "Model not found"}},
        )

    api = _make_api(handler)
    try:
        assert api.test_connection() is False
    finally:
        api.close()
