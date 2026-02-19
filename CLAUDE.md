# Hostaway Reservation Export

## Project Overview

Python script that connects to the Hostaway API, pulls all non-cancelled reservations with their conversation message threads, and outputs structured JSON with PII redacted.

Built as a test task for a US-based property management company (17+ vacation rental units).

## Tech Stack

- **Language:** Python 3
- **Dependencies:** `requests`, `python-dotenv`, `pytest`
- **API:** Hostaway REST API v1

## Project Structure

```
hostway-test/
├── CLAUDE.md
├── .env                  # API credentials (never commit)
├── .env.example          # Template with placeholder values
├── .gitignore
├── requirements.txt
├── src/
│   └── hostaway_export.py   # Main script
├── output/
│   └── reservations_with_messages.json  # Generated output
└── tests/
    └── test_hostaway.py     # Unit tests (49 tests)
```

## Running the Script

```bash
# Install dependencies
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Run the export
python src/hostaway_export.py

# Run tests
python -m pytest tests/ -v
```

## Hostaway API Details

- **Base URL:** `https://api.hostaway.com/v1`
- **Auth:** OAuth2 client credentials -> `POST /v1/accessTokens` with `client_id` (account ID) + `client_secret` (API key)
- **Token:** Valid 24 months. Must wait 1s after receiving before making calls.
- **Rate Limits:** 15 req/10s per IP, 20 req/10s per account
- **Pagination:** `limit`/`offset` params, response includes `count` and `totalPages`

### Key Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `POST /v1/accessTokens` | Get bearer token |
| `GET /v1/reservations` | List all reservations (use `includeResources=1`) |
| `GET /v1/conversations` | List all conversations |
| `GET /v1/conversations/{id}/messages` | Get message thread for a conversation |

### API Quirks

- Reservations use `arrivalDate`/`departureDate` (not checkIn/checkOut)
- No server-side date filtering for reservations — must filter client-side
- Conversations link to reservations via `reservationId` field
- Message sender info varies: check `senderName` first, fall back to `communicationFrom`
- `insertedOn` is the reliable timestamp for messages

## Error Handling

- **Auth failures (401/403):** Clear error message about invalid credentials
- **Rate limits (429):** Exponential backoff starting at 2s, max 3 retries
- **Empty responses:** Logged as warning, processing continues
- **Network errors:** Retry with backoff, fail after 3 attempts
- **Base delay:** 0.7s between all API calls to stay under rate limits

## Environment Variables

Required in `.env`:
```
HOSTAWAY_ACCOUNT_ID=your_account_id
HOSTAWAY_API_KEY=your_api_key
```

## Output Format

```json
{
  "exported_at": "2026-02-19T...",
  "total_reservations": 105,
  "reservations": [
    {
      "id": 12345,
      "guest_name": "...",
      "listing_id": "...",
      "listing_name": "...",
      "check_in": "2026-02-20",
      "check_out": "2026-02-25",
      "status": "...",
      "channel": "airbnbOfficial",
      "total_price": 500.00,
      "currency": "USD",
      "number_of_guests": 2,
      "reservation_details": { "phone", "email", "guest_note", "host_note" },
      "conversation": {
        "conversation_id": 678,
        "message_count": 3,
        "messages": [{ "id", "body", "sender", "sent_at", "status" }]
      }
    }
  ]
}
```

## Key Conventions

- Secrets in `.env` only, never hardcoded
- Structured logging via `logging` module (no print statements)
- Immutable data patterns throughout
- All API calls go through `api_request()` with built-in retry logic
