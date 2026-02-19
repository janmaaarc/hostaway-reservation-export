# Hostaway Reservation Export

Python script that connects to the Hostaway API, pulls all non-cancelled reservations with their conversation message threads, and outputs structured JSON with PII (phone/email) redacted.

## Quick Start

```bash
# Clone and set up
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Configure credentials
cp .env.example .env
# Edit .env with your Hostaway Account ID and API Key

# Run the export
python src/hostaway_export.py

# Run tests
python -m pytest tests/ -v
```

## Project Structure

```
hostway-test/
├── README.md
├── WRITEUP.md               # AI tools write-up and storage proposal
├── CLAUDE.md                 # Claude Code project instructions
├── .env                      # API credentials (git-ignored)
├── .env.example              # Credential template
├── .gitignore
├── requirements.txt
├── src/
│   └── hostaway_export.py    # Main export script
├── output/
│   └── reservations_with_messages.json  # Generated output
└── tests/
    └── test_hostaway.py      # Unit tests (49 tests)
```

## Configuration

Create a `.env` file (or copy `.env.example`) with your Hostaway credentials:

```
HOSTAWAY_ACCOUNT_ID=your_account_id
HOSTAWAY_API_KEY=your_api_key
```

## What It Does

1. **Authenticates** via OAuth2 client credentials (`POST /v1/accessTokens`)
2. **Fetches all reservations** with pagination (50 per page, `includeResources=1`)
3. **Filters** out cancelled and declined reservations (keeps all other statuses including past)
4. **Fetches all conversations** and maps them to reservations via `reservationId`
5. **Pulls message threads** for each matched conversation
6. **Redacts PII** — phone numbers show `***-***-1234`, emails show `j***@domain.com`
7. **Outputs** a single consolidated JSON file to `output/reservations_with_messages.json`

## Output Format

```json
{
  "exported_at": "2026-02-19T09:09:55.629352+00:00",
  "total_reservations": 107,
  "reservations": [
    {
      "id": 54653170,
      "guest_name": "John Doe",
      "listing_id": 446954,
      "listing_name": "3801 Mobile Hwy Unit 8",
      "check_in": "2026-02-20",
      "check_out": "2026-02-23",
      "status": "confirmed",
      "channel": "airbnbOfficial",
      "total_price": 245,
      "currency": "USD",
      "number_of_guests": 2,
      "reservation_details": {
        "phone": "***-***-3456",
        "email": "g***@example.com",
        "guest_note": null,
        "host_note": null
      },
      "conversation": {
        "conversation_id": 40306057,
        "message_count": 3,
        "messages": [
          {
            "id": 388237897,
            "body": "Looking forward to our stay!",
            "sender": "John Doe",
            "sent_at": "2026-02-19 09:00:45",
            "status": "sent"
          }
        ]
      }
    }
  ]
}
```

## Error Handling

| Scenario | Behavior |
|----------|----------|
| Auth failure (401/403) | Raises `AuthenticationError` with clear message |
| Rate limit (429) | Exponential backoff (2s, 4s, 8s), max 3 retries. Respects `Retry-After` header |
| Connection error | Retry with backoff, fails after 3 attempts |
| Timeout | 30s per request, retry with backoff |
| Empty responses | Logged as warning, processing continues |
| Base delay | 0.7s between all API calls to stay under rate limits |

## Tests

49 unit tests covering all public functions:

```
TestLoadCredentials      (3 tests)  - credential loading and validation
TestBuildHeaders         (1 test)   - auth header construction
TestFilterNonCancelled   (5 tests)  - status filtering, case insensitivity
TestRedactPhone          (5 tests)  - phone number redaction
TestRedactEmail          (5 tests)  - email address redaction
TestBuildConversationMap (4 tests)  - reservation-to-conversation mapping
TestApiRequest           (9 tests)  - retry logic, rate limits, timeouts, auth errors
TestAuthenticate         (2 tests)  - token retrieval
TestFetchAllPages        (3 tests)  - pagination handling
TestAssembleOutput       (2 tests)  - reservation + message assembly, PII redaction
TestFetchReservations    (2 tests)  - reservation fetching with params
TestFetchConversations   (2 tests)  - conversation fetching
TestWriteOutput          (2 tests)  - JSON file output
TestMain                 (4 tests)  - full pipeline integration, error exits
```

All API calls are mocked - no live credentials needed to run tests.

## API Endpoints Used

| Endpoint | Purpose |
|----------|---------|
| `POST /v1/accessTokens` | OAuth2 token |
| `GET /v1/reservations` | List reservations (paginated) |
| `GET /v1/conversations` | List conversations (paginated) |
| `GET /v1/conversations/{id}/messages` | Message thread for a conversation |

## Dependencies

- `requests` - HTTP client
- `python-dotenv` - Environment variable loading
- `pytest` - Test framework
