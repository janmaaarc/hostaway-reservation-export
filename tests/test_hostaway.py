"""Unit tests for the Hostaway export script."""

import json
import os
import sys
import tempfile
from unittest.mock import MagicMock, patch

import pytest
import requests as requests_lib

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from hostaway_export import (
    AuthenticationError,
    ApiError,
    NetworkError,
    api_request,
    assemble_output,
    authenticate,
    build_conversation_map,
    build_headers,
    fetch_all_pages,
    fetch_conversations,
    fetch_reservations,
    filter_non_cancelled,
    load_credentials,
    main,
    redact_email,
    redact_phone,
    write_output,
)


class TestLoadCredentials:
    @patch.dict(os.environ, {"HOSTAWAY_ACCOUNT_ID": "12345", "HOSTAWAY_API_KEY": "testkey"})
    def test_returns_credentials_when_set(self):
        account_id, api_key = load_credentials()
        assert account_id == "12345"
        assert api_key == "testkey"

    @patch("hostaway_export.load_dotenv")
    @patch.dict(os.environ, {}, clear=True)
    def test_raises_when_missing(self, mock_dotenv):
        with pytest.raises(ValueError, match="Missing credentials"):
            load_credentials()

    @patch("hostaway_export.load_dotenv")
    @patch.dict(os.environ, {"HOSTAWAY_ACCOUNT_ID": "12345"}, clear=True)
    def test_raises_when_api_key_missing(self, mock_dotenv):
        with pytest.raises(ValueError, match="Missing credentials"):
            load_credentials()


class TestBuildHeaders:
    def test_returns_correct_headers(self):
        headers = build_headers("my_token")
        assert headers["Authorization"] == "Bearer my_token"
        assert headers["Content-Type"] == "application/json"


class TestFilterNonCancelled:
    def test_excludes_cancelled_reservations(self):
        reservations = [
            {"id": 1, "status": "cancelled"},
            {"id": 2, "status": "confirmed"},
            {"id": 3, "status": "declined"},
        ]
        result = filter_non_cancelled(reservations)
        ids = [r["id"] for r in result]
        assert ids == [2]

    def test_keeps_all_active_statuses(self):
        reservations = [
            {"id": 1, "status": "confirmed"},
            {"id": 2, "status": "new"},
            {"id": 3, "status": "modified"},
            {"id": 4, "status": "ownerStay"},
        ]
        result = filter_non_cancelled(reservations)
        assert len(result) == 4

    def test_handles_empty_list(self):
        result = filter_non_cancelled([])
        assert result == []

    def test_includes_past_reservations(self):
        reservations = [
            {"id": 1, "departureDate": "2020-01-01", "status": "confirmed"},
            {"id": 2, "departureDate": "2030-12-31", "status": "confirmed"},
        ]
        result = filter_non_cancelled(reservations)
        assert len(result) == 2

    def test_case_insensitive_status(self):
        reservations = [
            {"id": 1, "status": "Cancelled"},
            {"id": 2, "status": "DECLINED"},
            {"id": 3, "status": "confirmed"},
        ]
        result = filter_non_cancelled(reservations)
        ids = [r["id"] for r in result]
        assert ids == [3]


class TestRedactPhone:
    def test_redacts_full_phone(self):
        assert redact_phone("+15551234567") == "***-***-4567"

    def test_redacts_short_phone(self):
        assert redact_phone("1234") == "***"

    def test_handles_none(self):
        assert redact_phone(None) is None

    def test_handles_empty_string(self):
        assert redact_phone("") == ""

    def test_handles_formatted_phone(self):
        assert redact_phone("(555) 123-4567") == "***-***-4567"


class TestRedactEmail:
    def test_redacts_email(self):
        assert redact_email("john@test.com") == "j***@test.com"

    def test_handles_none(self):
        assert redact_email(None) is None

    def test_handles_empty_string(self):
        assert redact_email("") == ""

    def test_handles_invalid_email(self):
        assert redact_email("notanemail") == "***"

    def test_preserves_domain(self):
        assert redact_email("guest@airbnb.com") == "g***@airbnb.com"


class TestBuildConversationMap:
    def test_maps_by_reservation_id(self):
        conversations = [
            {"id": 100, "reservationId": 1},
            {"id": 200, "reservationId": 2},
            {"id": 300, "reservationId": 3},
        ]
        result = build_conversation_map(conversations)
        assert result[1]["id"] == 100
        assert result[2]["id"] == 200
        assert result[3]["id"] == 300

    def test_skips_conversations_without_reservation_id(self):
        conversations = [
            {"id": 100, "reservationId": None},
            {"id": 200, "reservationId": 2},
        ]
        result = build_conversation_map(conversations)
        assert len(result) == 1
        assert 2 in result

    def test_empty_conversations(self):
        result = build_conversation_map([])
        assert result == {}

    def test_warns_on_duplicate_reservation_id(self):
        conversations = [
            {"id": 100, "reservationId": 1},
            {"id": 200, "reservationId": 1},
        ]
        result = build_conversation_map(conversations)
        assert result[1]["id"] == 200
        assert len(result) == 1


class TestApiRequest:
    @patch("hostaway_export.session")
    def test_successful_request(self, mock_session):
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {"status": "success", "result": []}
        mock_response.raise_for_status = MagicMock()
        mock_session.request.return_value = mock_response

        result = api_request("GET", "https://api.hostaway.com/v1/test")
        assert result == {"status": "success", "result": []}

    @patch("hostaway_export.session")
    def test_auth_failure_raises(self, mock_session):
        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_session.request.return_value = mock_response

        with pytest.raises(AuthenticationError, match="Authentication failed"):
            api_request("GET", "https://api.hostaway.com/v1/test")

    @patch("hostaway_export.session")
    def test_forbidden_raises(self, mock_session):
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_session.request.return_value = mock_response

        with pytest.raises(AuthenticationError, match="Access forbidden"):
            api_request("GET", "https://api.hostaway.com/v1/test")

    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.session")
    def test_rate_limit_retries(self, mock_session, mock_sleep):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {}

        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"status": "success"}
        success.raise_for_status = MagicMock()

        mock_session.request.side_effect = [rate_limited, success]

        result = api_request("GET", "https://api.hostaway.com/v1/test")
        assert result == {"status": "success"}
        assert mock_sleep.called

    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.session")
    def test_rate_limit_respects_retry_after(self, mock_session, mock_sleep):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {"Retry-After": "5"}

        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"status": "success"}
        success.raise_for_status = MagicMock()

        mock_session.request.side_effect = [rate_limited, success]

        api_request("GET", "https://api.hostaway.com/v1/test")
        mock_sleep.assert_called_with(5)

    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.session")
    def test_rate_limit_exhausts_retries(self, mock_session, mock_sleep):
        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.headers = {}
        mock_session.request.return_value = rate_limited

        with pytest.raises(ApiError, match="failed after"):
            api_request("GET", "https://api.hostaway.com/v1/test")

    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.session")
    def test_connection_error_retries(self, mock_session, mock_sleep):
        mock_session.request.side_effect = [
            requests_lib.exceptions.ConnectionError("Connection refused"),
            requests_lib.exceptions.ConnectionError("Connection refused"),
            requests_lib.exceptions.ConnectionError("Connection refused"),
        ]

        with pytest.raises(NetworkError, match="Connection failed"):
            api_request("GET", "https://api.hostaway.com/v1/test")

    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.session")
    def test_timeout_retries_then_fails(self, mock_session, mock_sleep):
        mock_session.request.side_effect = [
            requests_lib.exceptions.Timeout("Request timed out"),
            requests_lib.exceptions.Timeout("Request timed out"),
            requests_lib.exceptions.Timeout("Request timed out"),
        ]

        with pytest.raises(NetworkError, match="timed out"):
            api_request("GET", "https://api.hostaway.com/v1/test")
        assert mock_sleep.call_count == 2

    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.session")
    def test_timeout_recovers_on_retry(self, mock_session, mock_sleep):
        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {"status": "success"}
        success.raise_for_status = MagicMock()

        mock_session.request.side_effect = [
            requests_lib.exceptions.Timeout("Request timed out"),
            success,
        ]

        result = api_request("GET", "https://api.hostaway.com/v1/test")
        assert result == {"status": "success"}
        assert mock_sleep.call_count == 1


class TestAuthenticate:
    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.api_request")
    def test_returns_token(self, mock_api, mock_sleep):
        mock_api.return_value = {"access_token": "test_token_123"}

        token = authenticate("12345", "secret_key")
        assert token == "test_token_123"
        mock_sleep.assert_called_with(1)

    @patch("hostaway_export.api_request")
    def test_raises_on_missing_token(self, mock_api):
        mock_api.return_value = {"error": "invalid"}

        with pytest.raises(AuthenticationError, match="No access token"):
            authenticate("12345", "bad_key")


class TestFetchAllPages:
    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.api_request")
    def test_single_page(self, mock_api, mock_sleep):
        mock_api.return_value = {
            "result": [{"id": 1}, {"id": 2}],
            "count": 2,
        }
        result = fetch_all_pages("https://api.hostaway.com/v1/test", {})
        assert len(result) == 2
        assert mock_api.call_count == 1

    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.api_request")
    def test_multiple_pages(self, mock_api, mock_sleep):
        mock_api.side_effect = [
            {"result": [{"id": i} for i in range(50)], "count": 75},
            {"result": [{"id": i} for i in range(50, 75)], "count": 75},
        ]
        result = fetch_all_pages("https://api.hostaway.com/v1/test", {})
        assert len(result) == 75

    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.api_request")
    def test_empty_result(self, mock_api, mock_sleep):
        mock_api.return_value = {"result": None, "count": 0}
        result = fetch_all_pages("https://api.hostaway.com/v1/test", {})
        assert result == []


class TestAssembleOutput:
    @patch("hostaway_export.time.sleep")
    @patch("hostaway_export.fetch_messages_for_conversation")
    def test_combines_reservation_with_messages(self, mock_fetch, mock_sleep):
        mock_fetch.return_value = [
            {"id": 1, "body": "Hello", "senderName": "Guest", "insertedOn": "2026-01-01", "status": "sent"}
        ]
        reservations = [{
            "id": 100,
            "guestName": "John",
            "listingMapId": 1,
            "listingName": "Test Listing",
            "arrivalDate": "2026-02-20",
            "departureDate": "2026-02-25",
            "status": "confirmed",
            "channelName": "airbnb",
            "totalPrice": 500,
            "currency": "USD",
            "numberOfGuests": 2,
            "phone": "555-1234",
            "email": "john@test.com",
            "guestNote": None,
            "hostNote": None,
        }]
        conv_map = {100: {"id": 999, "reservationId": 100}}

        result = assemble_output(reservations, conv_map, {})
        assert result["total_reservations"] == 1
        res = result["reservations"][0]
        assert res["guest_name"] == "John"
        assert res["reservation_details"]["phone"] == "***-***-1234"
        assert res["reservation_details"]["email"] == "j***@test.com"
        assert res["conversation"]["conversation_id"] == 999
        assert res["conversation"]["message_count"] == 1
        assert res["conversation"]["messages"][0]["body"] == "Hello"

    @patch("hostaway_export.time.sleep")
    def test_reservation_without_conversation(self, mock_sleep):
        reservations = [{
            "id": 200,
            "guestName": "Jane",
            "listingMapId": 2,
            "listingName": "Other Listing",
            "arrivalDate": "2026-03-01",
            "departureDate": "2026-03-05",
            "status": "confirmed",
            "channelName": "booking",
            "totalPrice": 300,
            "currency": "USD",
            "numberOfGuests": 1,
            "phone": None,
            "email": None,
            "guestNote": None,
            "hostNote": None,
        }]

        result = assemble_output(reservations, {}, {})
        res = result["reservations"][0]
        assert res["conversation"]["conversation_id"] is None
        assert res["conversation"]["messages"] == []


class TestFetchReservations:
    @patch("hostaway_export.fetch_all_pages")
    def test_fetches_with_include_resources(self, mock_fetch):
        mock_fetch.return_value = [{"id": 1}, {"id": 2}]
        result = fetch_reservations({"Authorization": "Bearer token"})
        assert len(result) == 2
        call_args = mock_fetch.call_args
        assert call_args[1]["params"]["includeResources"] == 1

    @patch("hostaway_export.fetch_all_pages")
    def test_returns_empty_when_no_reservations(self, mock_fetch):
        mock_fetch.return_value = []
        result = fetch_reservations({"Authorization": "Bearer token"})
        assert result == []


class TestFetchConversations:
    @patch("hostaway_export.fetch_all_pages")
    def test_fetches_all_conversations(self, mock_fetch):
        mock_fetch.return_value = [{"id": 100}, {"id": 200}]
        result = fetch_conversations({"Authorization": "Bearer token"})
        assert len(result) == 2

    @patch("hostaway_export.fetch_all_pages")
    def test_returns_empty_when_no_conversations(self, mock_fetch):
        mock_fetch.return_value = []
        result = fetch_conversations({"Authorization": "Bearer token"})
        assert result == []


class TestWriteOutput:
    def test_writes_json_to_file(self):
        data = {"test": True, "count": 5}
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "output", "test.json")
            write_output(data, output_path)

            with open(output_path, "r") as f:
                result = json.load(f)
            assert result == data

    def test_writes_to_flat_path(self):
        data = {"test": True}
        with tempfile.TemporaryDirectory() as tmpdir:
            output_path = os.path.join(tmpdir, "test.json")
            write_output(data, output_path)

            with open(output_path, "r") as f:
                result = json.load(f)
            assert result == data


class TestMain:
    @patch("hostaway_export.write_output")
    @patch("hostaway_export.assemble_output")
    @patch("hostaway_export.build_conversation_map")
    @patch("hostaway_export.fetch_conversations")
    @patch("hostaway_export.filter_non_cancelled")
    @patch("hostaway_export.fetch_reservations")
    @patch("hostaway_export.build_headers")
    @patch("hostaway_export.authenticate")
    @patch("hostaway_export.load_credentials")
    def test_full_pipeline(
        self, mock_creds, mock_auth, mock_headers, mock_fetch_res,
        mock_filter, mock_fetch_conv, mock_conv_map, mock_assemble, mock_write,
    ):
        mock_creds.return_value = ("12345", "testkey")
        mock_auth.return_value = "fake_token"
        mock_headers.return_value = {"Authorization": "Bearer fake_token"}
        mock_fetch_res.return_value = [{"id": 1}]
        mock_filter.return_value = [{"id": 1}]
        mock_fetch_conv.return_value = [{"id": 100, "reservationId": 1}]
        mock_conv_map.return_value = {1: {"id": 100}}
        mock_assemble.return_value = {
            "exported_at": "2026-02-19T00:00:00",
            "total_reservations": 1,
            "reservations": [{"id": 1}],
        }

        main()

        mock_creds.assert_called_once()
        mock_auth.assert_called_once_with("12345", "testkey")
        mock_fetch_res.assert_called_once()
        mock_filter.assert_called_once()
        mock_fetch_conv.assert_called_once()
        mock_assemble.assert_called_once()
        mock_write.assert_called_once()

    @patch("hostaway_export.write_output")
    @patch("hostaway_export.fetch_reservations")
    @patch("hostaway_export.build_headers")
    @patch("hostaway_export.authenticate")
    @patch("hostaway_export.load_credentials")
    def test_handles_no_reservations(
        self, mock_creds, mock_auth, mock_headers, mock_fetch_res, mock_write,
    ):
        mock_creds.return_value = ("12345", "testkey")
        mock_auth.return_value = "fake_token"
        mock_headers.return_value = {"Authorization": "Bearer fake_token"}
        mock_fetch_res.return_value = []

        main()

        mock_write.assert_called_once()
        output_data = mock_write.call_args[0][0]
        assert output_data["total_reservations"] == 0
        assert output_data["reservations"] == []

    @patch("hostaway_export.load_credentials")
    def test_exits_on_auth_error(self, mock_creds):
        mock_creds.side_effect = AuthenticationError("Bad credentials")

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1

    @patch("hostaway_export.load_credentials")
    def test_exits_on_missing_env(self, mock_creds):
        mock_creds.side_effect = ValueError("Missing credentials")

        with pytest.raises(SystemExit) as exc_info:
            main()
        assert exc_info.value.code == 1
