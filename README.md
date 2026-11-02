# IITP OptiRide

This archive contains the existing OptiRide backend and frontend with a separate **My Rides** activity page and **My Upcoming Ride** confirmed-participation page.

## Run locally

From the project root:

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python -m rideshare serve
```

Open `http://127.0.0.1:8000`.

## New API routes

- `GET /api/my-rides` — authenticated user's own offers, group requests, incoming join requests and sent join requests.
- `GET /api/my-upcoming` — only confirmed accepted cab participations and confirmed booking-manager bookings.
- `POST /api/my-upcoming/{participation_id}/cancel` — cancel the logged-in user's own confirmed participation.
- `POST /api/student/join-requests/{join_id}/accept` — owner-only acceptance, with capacity and luggage rechecks.
- `POST /api/student/join-requests/{join_id}/reject` — owner-only rejection.

All these endpoints require the existing HttpOnly session cookie. User identity is taken from the session, not from a roll number submitted by the browser. The API routes execute under the server's shared request lock; JSON writes use a temporary file and atomic replacement.

## Cancellation behaviour

- Cancellation is allowed through the instant exactly 24 hours before departure and rejected after that point.
- The backend returns the required message when the deadline has passed: `Cancellation is allowed only at least 24 hours before departure.`
- Accepted join requests are marked `CANCELLED`. Capacity and luggage usage are computed from active join records, so cancelled participation no longer consumes either resource. The original cab offer remains `OPEN`.
- Confirmed Optimization/BookingManager bookings use the existing booking store and cancellation logic. Fare breakdowns are recalculated from active bookings.
- Pending join requests do not appear on My Upcoming Ride.

## Tests

```bash
python -m unittest discover -s tests -v
```

The included tests exercise the student-join cancellation flow, session-owner checks at the API-method level, the 24-hour rule, duplicate cancellation prevention, owner acceptance and pending-versus-confirmed visibility. The test module stubs the optional PuLP import because these focused tests do not execute the optimizer. Install the declared requirements to run the complete application and its IP solver.

## Important integration limits in the supplied source archive

The uploaded archives contained the Python package and three frontend files, but did not contain the project's `data/` CSV files, `outputs/` JSON data, or a pre-existing test suite. Therefore this change cannot verify your real production records or all optimizer-to-booking transitions. Existing submitted group requests are associated with the authenticated roll number from this change onward; legacy requests without that association cannot safely be attributed to a student and are not shown as that student's confirmed upcoming ride.

The current application persists users, sessions, offers, join requests and bookings in local JSON files. Render's free filesystem is ephemeral; this patch does not introduce PostgreSQL or claim durable persistence across Render restarts. For production, configure persistent storage or migrate these repositories to PostgreSQL before relying on the service for real student bookings. Do not commit `outputs/`, user files, session files, or secrets.
