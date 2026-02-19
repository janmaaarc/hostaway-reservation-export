"""
Hostaway API Export Script

Connects to the Hostaway API, pulls all non-cancelled reservations,
fetches associated conversation threads, and outputs structured JSON
with PII (phone/email) redacted.
"""

import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv

BASE_URL = "https://api.hostaway.com/v1"
REQUEST_DELAY = 0.7
MAX_RETRIES = 3
BACKOFF_BASE = 2
PAGE_LIMIT = 50

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
logger = logging.getLogger(__name__)

session = requests.Session()


class HostawayError(Exception):
    """Base exception for Hostaway API errors."""


class AuthenticationError(HostawayError):
    """Raised when authentication fails."""


class NetworkError(HostawayError):
    """Raised when network requests fail."""


class ApiError(HostawayError):
    """Raised when the API returns an error."""


def load_credentials():
    load_dotenv()
    account_id = os.getenv("HOSTAWAY_ACCOUNT_ID")
    api_key = os.getenv("HOSTAWAY_API_KEY")

    if not account_id or not api_key:
        raise ValueError(
            "Missing credentials. Set HOSTAWAY_ACCOUNT_ID and HOSTAWAY_API_KEY in .env"
        )

    return account_id, api_key


def api_request(method, url, headers=None, data=None, params=None):
    """Make an API request with retry logic and rate limit handling."""
    for attempt in range(MAX_RETRIES):
        try:
            response = session.request(
                method, url, headers=headers, data=data, params=params, timeout=30
            )

            if response.status_code == 429:
                retry_after = response.headers.get("Retry-After")
                if retry_after:
                    try:
                        wait_time = int(retry_after)
                    except ValueError:
                        wait_time = BACKOFF_BASE ** (attempt + 1)
                else:
                    wait_time = BACKOFF_BASE ** (attempt + 1)
                logger.warning(
                    "Rate limited (429). Retrying in %ds (attempt %d/%d)",
                    wait_time,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(wait_time)
                continue

            if response.status_code == 401:
                raise AuthenticationError("Authentication failed. Check your credentials.")

            if response.status_code == 403:
                raise AuthenticationError("Access forbidden. Check your API permissions.")

            response.raise_for_status()
            return response.json()

        except requests.exceptions.ConnectionError as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = BACKOFF_BASE ** (attempt + 1)
                logger.warning(
                    "Connection error. Retrying in %ds (attempt %d/%d)",
                    wait_time,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(wait_time)
            else:
                raise NetworkError(f"Connection failed after {MAX_RETRIES} attempts") from e

        except requests.exceptions.Timeout as e:
            if attempt < MAX_RETRIES - 1:
                wait_time = BACKOFF_BASE ** (attempt + 1)
                logger.warning(
                    "Request timed out. Retrying in %ds (attempt %d/%d)",
                    wait_time,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(wait_time)
            else:
                raise NetworkError(f"Request timed out after {MAX_RETRIES} attempts") from e

        except requests.exceptions.HTTPError as e:
            raise ApiError(f"API error {response.status_code}") from e

    raise ApiError(f"Request failed after {MAX_RETRIES} retries")


def authenticate(account_id, api_key):
    """Authenticate with Hostaway and return an access token."""
    logger.info("Authenticating with Hostaway API...")

    response = api_request(
        "POST",
        f"{BASE_URL}/accessTokens",
        data={
            "grant_type": "client_credentials",
            "client_id": account_id,
            "client_secret": api_key,
            "scope": "general",
        },
    )

    token = response.get("access_token")
    if not token:
        raise AuthenticationError("No access token in response")

    logger.info("Authentication successful. Waiting 1s before making API calls...")
    time.sleep(1)

    return token


def build_headers(token):
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }


def fetch_all_pages(url, headers, params=None):
    """Fetch all pages from a paginated endpoint."""
    all_results = []
    offset = 0
    base_params = dict(params) if params else {}

    while True:
        page_params = {**base_params, "limit": PAGE_LIMIT, "offset": offset}

        logger.info("Fetching %s (offset=%d)", url, offset)
        response = api_request("GET", url, headers=headers, params=page_params)

        result = response.get("result")
        if not result:
            if offset == 0:
                logger.warning("Empty result at offset %d", offset)
            break

        all_results.extend(result)

        count = response.get("count", 0)
        if offset + PAGE_LIMIT >= count:
            break

        offset += PAGE_LIMIT
        time.sleep(REQUEST_DELAY)

    return all_results


def fetch_reservations(headers):
    """Fetch all reservations with resources included."""
    logger.info("Fetching reservations...")
    reservations = fetch_all_pages(
        f"{BASE_URL}/reservations",
        headers,
        params={"includeResources": 1},
    )
    logger.info("Fetched %d total reservations", len(reservations))
    return reservations


def filter_non_cancelled(reservations):
    """Filter out cancelled and declined reservations."""
    cancelled_statuses = {"cancelled", "canceled", "declined"}
    filtered = [
        r for r in reservations
        if r.get("status", "").lower() not in cancelled_statuses
    ]
    logger.info(
        "Filtered to %d non-cancelled reservations (out of %d total)",
        len(filtered),
        len(reservations),
    )
    return filtered


def fetch_conversations(headers):
    """Fetch all conversations."""
    logger.info("Fetching conversations...")
    conversations = fetch_all_pages(f"{BASE_URL}/conversations", headers)
    logger.info("Fetched %d conversations", len(conversations))
    return conversations


def fetch_messages_for_conversation(conversation_id, headers):
    """Fetch all messages for a specific conversation."""
    logger.info("Fetching messages for conversation %d", conversation_id)
    messages = fetch_all_pages(
        f"{BASE_URL}/conversations/{conversation_id}/messages",
        headers,
        params={"includeScheduledMessages": 1},
    )
    return messages


def build_conversation_map(conversations):
    """Build a mapping of reservation_id -> conversation."""
    conv_map = {}
    for conv in conversations:
        res_id = conv.get("reservationId")
        if res_id:
            if res_id in conv_map:
                logger.warning(
                    "Multiple conversations for reservation %s. Using latest (id=%s).",
                    res_id,
                    conv.get("id"),
                )
            conv_map[res_id] = conv
    return conv_map


def redact_phone(phone):
    """Redact phone number, keeping last 4 digits."""
    if not phone:
        return phone
    digits = re.sub(r"\D", "", str(phone))
    if len(digits) <= 4:
        return "***"
    return f"***-***-{digits[-4:]}"


def redact_email(email):
    """Redact email, keeping first char and domain."""
    if not email:
        return email
    parts = str(email).split("@")
    if len(parts) != 2:
        return "***"
    local = parts[0]
    prefix = local[0] if local else ""
    return f"{prefix}***@{parts[1]}"


def assemble_output(reservations, conversation_map, headers):
    """Combine reservations with their conversation messages."""
    enriched = []

    for reservation in reservations:
        res_id = reservation.get("id")
        conversation = conversation_map.get(res_id)

        messages = []
        conversation_id = None

        if conversation:
            conversation_id = conversation.get("id")
            try:
                raw_messages = fetch_messages_for_conversation(conversation_id, headers)
                messages = [
                    {
                        "id": m.get("id"),
                        "body": m.get("body"),
                        "sender": m.get("senderName") or m.get("communicationFrom"),
                        "sent_at": m.get("insertedOn") or m.get("createdOn"),
                        "status": m.get("status"),
                    }
                    for m in raw_messages
                ]
                time.sleep(REQUEST_DELAY)
            except HostawayError as e:
                logger.error(
                    "Failed to fetch messages for conversation %d: %s",
                    conversation_id,
                    e,
                )

        enriched.append({
            "id": res_id,
            "guest_name": reservation.get("guestName"),
            "listing_id": reservation.get("listingMapId"),
            "listing_name": reservation.get("listingName"),
            "check_in": reservation.get("arrivalDate"),
            "check_out": reservation.get("departureDate"),
            "status": reservation.get("status"),
            "channel": reservation.get("channelName"),
            "total_price": reservation.get("totalPrice"),
            "currency": reservation.get("currency"),
            "number_of_guests": reservation.get("numberOfGuests"),
            "reservation_details": {
                "phone": redact_phone(reservation.get("phone")),
                "email": redact_email(reservation.get("email")),
                "guest_note": reservation.get("guestNote"),
                "host_note": reservation.get("hostNote"),
            },
            "conversation": {
                "conversation_id": conversation_id,
                "message_count": len(messages),
                "messages": messages,
            },
        })

    return {
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "total_reservations": len(enriched),
        "reservations": enriched,
    }


def write_output(data, output_path):
    """Write structured JSON to file."""
    dir_name = os.path.dirname(output_path)
    if dir_name:
        os.makedirs(dir_name, exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    logger.info("Output written to %s", output_path)


def main():
    try:
        account_id, api_key = load_credentials()
        token = authenticate(account_id, api_key)
        headers = build_headers(token)

        reservations = fetch_reservations(headers)

        if not reservations:
            logger.warning("No reservations found")
            output = {
                "exported_at": datetime.now(timezone.utc).isoformat(),
                "total_reservations": 0,
                "reservations": [],
            }
        else:
            filtered = filter_non_cancelled(reservations)

            if not filtered:
                logger.warning("No non-cancelled reservations found")

            conversations = fetch_conversations(headers)
            conversation_map = build_conversation_map(conversations)
            logger.info(
                "Mapped %d conversations to reservations", len(conversation_map)
            )

            output = assemble_output(filtered, conversation_map, headers)

        script_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        output_path = os.path.join(script_dir, "output", "reservations_with_messages.json")
        write_output(output, output_path)

        logger.info("Export complete. %d reservations exported.", output["total_reservations"])

    except AuthenticationError as e:
        logger.error("Authentication failed: %s", e)
        sys.exit(1)
    except NetworkError as e:
        logger.error("Network error: %s", e)
        sys.exit(1)
    except HostawayError as e:
        logger.error("API error: %s", e)
        sys.exit(1)
    except ValueError as e:
        logger.error("Configuration error: %s", e)
        sys.exit(1)


if __name__ == "__main__":
    main()
