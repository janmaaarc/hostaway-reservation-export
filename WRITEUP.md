# Write-Up

## AI Tools I Used

I used **Claude Code** (the CLI tool) as my main coding assistant throughout this project. It helped me scaffold the initial script, iterate on the API integration, and generate tests. I also referenced the Hostaway API docs directly since Claude didn't have specifics about their endpoints baked in.

## Prompts That Worked Well

Honestly, the biggest thing I learned is that vague prompts waste time. The more specific I was about what I needed, the better the output.

- Starting with a clear description of the end goal worked well: I described the full pipeline (auth -> fetch reservations -> fetch conversations -> match them -> output JSON) in one prompt, and that gave me a reasonable starting point to build from.
- Referencing the actual API docs was key. The Hostaway API has some non-obvious field names (`arrivalDate`/`departureDate` instead of the `checkIn`/`checkOut` I expected), so pointing Claude at the documentation saved me from guessing.
- For error handling, I got the best results when I specified the exact constraints — things like "15 requests per 10 seconds" and "use exponential backoff starting at 2s." Without those numbers, the generated retry logic was too generic.
- Test prompts worked best when I told it to mock the session object specifically, since the script uses `requests.Session()` rather than standalone `requests.get()` calls.

## What I Had to Fix

A few things didn't work on the first pass:

- **Pagination logic** — the initial version checked `len(results) < PAGE_LIMIT` to detect the last page, which silently skipped data when the last page happened to have exactly 50 results. I switched it to use the `count` field from the API response instead.
- **Date filtering** — I assumed the field would be `checkOutDate` but Hostaway primarily uses `departureDate`. Some reservations had one, some had the other, so I ended up checking both with a fallback.
- **Sender name inconsistency** — messages sometimes have the sender in `senderName`, sometimes in `communicationFrom`. Had to add fallback logic for that.
- **Token timing gotcha** — the Hostaway docs mention you need to wait 1 second after getting a token before making calls. I skipped that initially and got random 401s on the first API call. Small thing but annoying to debug.

## How Long It Took

Roughly **5–6 hours** spread over a day:

- ~45 min reading through the Hostaway API docs, testing a few endpoints in Postman to understand the response shapes and pagination behavior
- ~1 hr setting up the project structure and building the core script (auth flow, pagination, data assembly)
- ~45 min on error handling and retry logic — took a bit of trial and error to get the rate limiting right without being too aggressive
- ~45 min writing tests and iterating on mocks (28 unit tests). Mocking `requests.Session` properly took a few attempts
- ~1 hr running against the live API, debugging edge cases (the sender name inconsistency and the token timing issue ate up most of this)
- ~30 min on cleanup — tweaking the output format, writing the README, general code review
- ~30 min on breaks, context switching, re-reading docs when something didn't behave as expected

## Bonus: Long-Term Storage for Multi-Platform Data

If I needed to store this data long-term and combine it with SMS, email, and Discord messages for AI-assisted querying, I'd go with **PostgreSQL + pgvector**.

I considered MongoDB first since the data is already JSON and the schema varies across platforms, but the problem is you'd eventually need relational joins (e.g., "show me all messages for guests who stayed at listing X in March") and Mongo makes that painful. I also looked at using a dedicated vector DB like Pinecone or Weaviate for the AI search part, but running a separate system just for embeddings felt like overkill at this scale.

PostgreSQL handles both needs in one place. The structured reservation/guest/listing data goes into normalized tables with proper foreign keys. For messages, I'd create a unified `messages` table with a `source` column (hostaway, sms, email, discord) and a thread ID to link related messages across platforms to the same guest interaction. Each message gets a vector embedding stored via pgvector, so the AI assistant can do semantic search right alongside normal SQL filtering.

That means one database covers both "show me all reservations for listing X in March" and "find conversations where guests had issues with check-in" without needing to sync data between systems. For 17 units this scales fine without anything distributed. Ingestion would be a lightweight Python cron job that pulls from each platform's API, normalizes into the shared schema, and generates embeddings on insert.
