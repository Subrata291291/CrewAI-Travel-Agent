# Wayfarer — AI Travel Agent

This repository includes the original Python/CrewAI CLI and a local full-stack interface built on React, TypeScript, Vite and FastAPI. Existing trip, hotel search and hotel booking JSON files remain the source of truth.

## Local setup (Windows PowerShell)

Open two PowerShell terminals from the project root.

1. Install backend packages into the existing project virtual environment:

   ```powershell
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

2. Copy `.env.example` to `.env` if needed, then fill in provider and LLM keys in `.env`. The browser only receives the local API address; provider keys stay on the backend.

3. Start FastAPI in terminal one:

   ```powershell
   .\.venv\Scripts\python.exe -m uvicorn src.travel_agent.api.main:app --reload --host 127.0.0.1 --port 8000
   ```

4. Install frontend packages and start Vite in terminal two:

   ```powershell
   cd frontend
   npm.cmd install
   npm.cmd run dev
   ```

5. Open `http://localhost:5173`. The backend health endpoint is `http://127.0.0.1:8000/api/health`.

Set `CORS_ORIGINS` to comma-separated local origins when changing the frontend address. Set `VITE_API_URL` only when the backend is hosted at a different local address. Never put provider credentials in a `VITE_` variable.

## Netlify and Render deployment

The repository-root `netlify.toml` configures Netlify to build the frontend from `frontend/` and use the same-origin `/api` path. `frontend/public/_redirects` proxies `/api/*` to the Render service, so browser session cookies stay first-party on the Netlify domain rather than relying on cross-site cookies. `VITE_API_URL` is a frontend build-time setting, not a secret; do not put provider keys in it. Keep the deployed Netlify origin in Render's `CORS_ORIGINS` setting (for example, `https://crewai-travel-agent.netlify.app`). After changing the Netlify configuration, trigger a new deploy.

## API and capabilities

### Accounts and private trip data

The web workspace requires an account. Register at `/register`, sign in at `/login`, and view the authenticated profile at `/account`. `POST /api/v1/auth/register`, `POST /api/v1/auth/login`, `GET /api/v1/auth/me`, and `POST /api/v1/auth/logout` provide the account flow. Successful registration signs the user in. Passwords are stored using salted scrypt hashes; browser sessions use opaque, random HttpOnly cookies backed by the authentication database, expire after 30 days, and are revoked on logout. The cookie is SameSite=Lax, and production must use HTTPS with `AUTH_COOKIE_SECURE=true`. Mutating API requests require the app's same-site request header and configured CORS origins.

Users and sessions are stored in SQLite at `data/auth.sqlite3` by default. Set `AUTH_DB_PATH` to choose another durable path. The store initializes its tables in place and does not alter existing trip or booking JSON records. Existing legacy trips and drafts that predate accounts have no owner and are intentionally not assigned to the first or any subsequent account; they remain on disk but are hidden from authenticated accounts. New trips and drafts receive an owner when created, and booking access is checked through the owning trip. Back up the authentication database alongside the trip/booking data. Configure `APP_ENV=production`, `AUTH_COOKIE_SECURE=true`, and the correct `CORS_ORIGINS` for an HTTPS deployment.

The API reuses JSON trip storage, the trip state merge logic, LiteAPI hotel search and the existing hotel booking summaries. It includes conversational planning drafts, explicit draft saving, separate total-trip and nightly-hotel budgets, LiteAPI weather lookup, live hotel search from chat for a selected saved trip, hotel booking servicing, and the existing flight search/verification and gated sandbox checkout. Flight prebook and booking routes fail closed by default. Flight checkout uses LiteAPI's Nuitee payment SDK; it does not use Stripe Elements or the hotel's `ACC_CREDIT_CARD` method. Keep `ALLOW_FLIGHT_PREBOOK=false` and `ALLOW_FLIGHT_BOOKING=false` until Nuitee confirms sandbox Flights access and the SDK payment/booking flow is authorized. Production booking remains disabled.

Flight API access uses the same `NUITEE_API_KEY` as Hotels, but the Nuitee Connect account must have Flights enabled. Sandbox access is account-dependent and has limited, non-production data; production access needs provider approval. The adapter uses the documented `/flights/rates`, `/flights/verify`, `/flights/prebooks` and `/flights/bookings` endpoints. Search and fare verification are provider requests; no provider request runs on application startup. This project does not claim to know whether the configured account has access until the provider responds.

New chat plans are stored under `data/planning_drafts/`, not `data/trips/`, and are not added to My Trips until the user explicitly requests a save. Follow-ups use the active conversation's draft ID and a pending clarification field persisted with the draft; informational turns leave that field intact. Date/duration conflicts persist explicit alternatives as a decision clarification, and an itinerary request made while required details are missing remains pending until those details and any date conflict are resolved. The active draft ID and conversation ID are stored in browser storage under the authenticated user ID; after refresh, the frontend fetches the draft and verifies its owner and conversation before resuming. The server-side structured draft remains authoritative; the chat transcript itself is not restored after refresh. Updating a saved trip requires an explicit saved-trip update request and a selected trip. `total_trip_budget` / `budget_currency` are distinct from `hotel_budget_per_night` / `currency`; a general budget defaults to the total-trip fields and is never used as the hotel nightly limit. `preferred_transport_mode`, `accommodation_type`, and `interests` are partial draft fields: rail is a preference and may require onward road transfers, and broad sightseeing interests are not a promise that every attraction is feasible or included. Chat date answers support clear natural-language, relative-month, weekday, and ISO dates without guessing ambiguous numeric locales, and are checked against future-date, return-order, and duration requirements. Invalid date changes preserve previously validated dates and valid preferences from the same message. `WAYFARER_TIMEZONE` may specify the timezone used for relative dates; when unset, the host's local timezone is used. `trip_duration_days` counts inclusive calendar days; `nights` is the hotel-night count.

Chat history remains intact in the browser, while each request compacts oversized historical turns and bounds context to 20 turns and 24,000 characters; the current message has its own validation limit and is not compacted as history. Preference-only messages update the structured unsaved draft without regenerating an itinerary. Cabin and seat preferences are requests, not guaranteed selections, and ground-transfer preferences do not indicate a booking.

Chat can make a live hotel search only when the user explicitly asks, a saved trip is selected, and that trip has a combined nightly hotel budget. The actual provider offers are returned and shown in chat; the frontend opens Hotel Search for offer review and selection. Saving a draft is required before searching hotels from an unsaved plan. In Hotel Search, select a LiteAPI city or area suggestion before searching: trip destinations can be broad regions (for example, Sikkim), while hotel rates need a provider-recognized place boundary. The selected Place ID, trip dates, occupancy, currency, and budget are used for the rates request. Hotel booking is available in the web UI using the existing sandbox-only prebook, explicit confirmation, and provider verification workflow; the CLI remains available separately.

The `/api/weather` endpoint remains a LiteAPI daily forecast using server-side `NUITEE_API_KEY`; its locations are resolved via OpenStreetMap Nominatim and cached in memory for 24 hours, with geocoding requests rate-limited to one per second. The chat's current-weather intent and `/api/weather/current` use Open-Meteo geocoding and current observations, including the provider observation time and location timezone. Broad Kashmir requests use Srinagar as an explicitly labelled representative location, not a region-wide reading. Forecast requests beyond the supported 16-day horizon are rejected rather than presented as live forecasts. No weather-provider request was made during implementation. LiteAPI forecasts still require the Nuitee weather data endpoint to be enabled.

In My Bookings, a confirmed sandbox hotel booking can be checked against the provider before cancellation. The app displays the current provider status and cancellation terms, then requires a separate confirmation; uncertain outcomes require status reconciliation and are not automatically retried. The holder amendment form supports only booking-holder first name, last name and email, as documented; other guest or date changes are not represented as supported.

Chat does not perform booking-status lookups, cancellations, refunds, or amendments. It directs users to the supported My Bookings controls for hotel servicing and to Nuitee's support process for flight servicing; it does not claim that a request was submitted.

LiteAPI's current Flights support/billing documentation describes cancellations, refunds and modifications as operations handled by Nuitée's flight operations support model, and documents no general direct flight-cancellation or reissue endpoint. Wayfarer therefore does not issue flight servicing requests or claim a flight is cancelled/changed; use Nuitee's confirmed support channel for your account/support model. No case-creation API is assumed.

Flight checkout is exposed only when the sandbox key, a successful user-triggered Flights access check, both explicit flight gates, and the Nuitee SDK payment flow are configured. The optional **Check flight access** action performs a provider rates request only when clicked. No provider search, prebook, payment, booking, cancellation, or amendment request was made while implementing this change.

The JSON filesystem stores are for local development only. They do not provide user authentication, access isolation, transaction locking or deployment-grade durability. Do not expose this API beyond the local machine without adding authentication and authorization and strengthening persistence. Keep the CLI available with `.\.venv\Scripts\python.exe run_travel_planner.py`.

## Backend environment variable names

Existing LLM and provider configuration: `LLM_PROVIDER_ORDER`, `GROQ_API_KEY`, `GROQ_MODEL`, `OPENROUTER_API_KEY`, `OPENROUTER_MODEL`, `GEMINI_API_KEY`, `GEMINI_MODEL`, `OPENAI_API_KEY`, `OPENAI_MODEL`, `NUITEE_API_KEY`, `NUITEE_BASE_URL`, `LOG_LEVEL`.

Web configuration: `APP_ENV`, `CORS_ORIGINS`, `ALLOW_PRODUCTION_BOOKING`, `ALLOW_FLIGHT_PREBOOK`, `ALLOW_FLIGHT_BOOKING`, and `FLIGHT_PAYMENT_MODE`. Set `FLIGHT_PAYMENT_MODE=nuitee_sdk`; the frontend loads LiteAPI's documented payment SDK and does not need a static publishable key.

## Sandbox booking workspace

The Hotel Search page now offers an explicit sandbox booking review after creating a fresh provider prebook. It submits `POST /rates/book` only when the user checks the confirmation box and presses **Confirm sandbox hotel booking**. It uses `ACC_CREDIT_CARD`, which LiteAPI documents as a no-charge simulation for sandbox hotel bookings, and verifies the returned provider booking ID with `GET /bookings/{bookingId}` before the local record can show `CONFIRMED_SANDBOX`. Unknown outcomes require reconciliation and cannot be resubmitted from the same prebook.

The Flight Search page supports provider search and fare verification, followed by a gated sandbox checkout. Passenger and contact details are sent for the exact selected `offerId`; prebook always uses `usePaymentSdk: true`. The server retains the provider `prebookId`, `transactionId`, and `secretKey` in its checkout record. The browser uses the LiteAPI/Nuitee payment wrapper configured with `publicKey: "sandbox"` and that returned `secretKey`; it does not require a static `publishableKey`. Sandbox test card details are shown only inside the sandbox payment UI. The SDK returns to Wayfarer after payment; the user must explicitly confirm the provider booking, and the server then submits the same `prebookId` with the saved latest `transactionId` before looking up the provider status. Wayfarer records a flight under My Bookings only after the provider lookup reports `CONFIRMED`. Pending or uncertain submissions are kept as reconciliation attempts, are not presented as bookings, and are never blindly resubmitted. Flight checkout does not use `usePaymentSdk: false` or the hotel `ACC_CREDIT_CARD` payment method.

Wayfarer displays `CONFIRMED_SANDBOX` only when LiteAPI's booking lookup returns `CONFIRMED`; uncertain provider responses are recorded separately as reconciliation attempts and must not be blindly resubmitted. Passenger/passport details and raw card details are not saved in Wayfarer's checkout records. The payment `secretKey` is kept in the server-side checkout record until the SDK returns and is never saved in browser storage or logs. `GET /api/capabilities` is local-only and does not test Flights or payment access. **Check flight access** sends a rates-only provider request only when a user explicitly clicks it; request sandbox Flights API access from Nuitee Connect if access is denied.

Keep `ALLOW_FLIGHT_PREBOOK=false`, `ALLOW_FLIGHT_BOOKING=false`, and `ALLOW_PRODUCTION_BOOKING=false` until Nuitee confirms sandbox Flights API and provider-side sandbox booking/payment access, and the user-triggered rates-only access check succeeds. Set `FLIGHT_PAYMENT_MODE=nuitee_sdk`. The documented SDK configuration is `publicKey: "sandbox"` with the exact `secretKey` returned by prebook; no separately provisioned static publishable key is assumed. The sandbox UI test card is `4242424242424242`, any three-digit CVC, and any future expiry date. Do not submit these or real card details to Wayfarer's backend. Nuitee must explicitly confirm any remaining account-specific payment/booking permissions; no payment-bypass mode is enabled or assumed.

The read-only local capability endpoint is `GET http://127.0.0.1:8000/api/capabilities`; it does not call LiteAPI. It reports sandbox-key classification, hotel configuration, whether a user-triggered flight rates request has succeeded during this backend process, checkout-flag state, and the production-booking hard disable. Flight prebooking is rejected until that real access check or a flight search succeeds. Restarting the backend clears this in-memory access confirmation, so run the check again before checkout. Hotel provider-created test records can be compared under the Nuitee Connect dashboard's **Sandbox** environment. Match the provider booking ID and the `WF-SBX-…` client reference shown in Wayfarer. Existing records are retained.

Run the app in PowerShell from the repository root:

```powershell
.\.venv\Scripts\python.exe -m uvicorn src.travel_agent.api.main:app --reload
```

In a second PowerShell window:

```powershell
Set-Location frontend
npm run dev
```

Hotel and flight sandbox workflows require the backend `.env` to contain a `NUITEE_API_KEY` with a recognized `sand_` or `sandbox_` prefix. For flights, leave both `ALLOW_FLIGHT_PREBOOK` and `ALLOW_FLIGHT_BOOKING` false until Nuitee confirms Flights sandbox access and the standard LiteAPI payment SDK flow is enabled for the account. The separate user-triggered Flights access check verifies rate access, not booking permission. The documented SDK flow does not require a separately provisioned static frontend publishable key. Do not enable production booking.

To start a sandbox flight checkout, select a saved trip, open **Flight Search**, select the matching airports, then run **Check flight access** (or search flights). Verify an offer, review its exact route, fare, currency, baggage and conditions, enter each passenger's required details and contact information, and confirm the prebook. Complete payment in LiteAPI's sandbox UI using its test card. After the SDK returns to Wayfarer, explicitly confirm the provider booking. The resulting confirmed booking and provider booking ID/reference appear under **My Bookings** for that trip. Find the same provider record in Nuitee Connect by switching to the **Sandbox** environment and matching the provider booking ID or booking reference shown by Wayfarer.
