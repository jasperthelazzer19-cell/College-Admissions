# PostHog Analytics — Setup Report

PostHog product analytics has been instrumented into Candor (`app.py`). The
integration is **additive and optional**: with no `POSTHOG_API_KEY` set it
no-ops cleanly (client snippet skipped, server client stubbed, `/ingest` proxy
returns 404), so it is safe to deploy *before* you create the PostHog account.
No data flows until you set the key.

> Nothing here was pushed or deployed. Review the branch, then deploy as usual.

---

## 1. Files changed

| File | Action | Purpose |
|---|---|---|
| `requirements.txt` | edited | Added `posthog` (server SDK). |
| `app.py` | edited | Env wiring, server client + helpers, client snippet, `/ingest` reverse proxy, 7 event captures, `identify` on auth. |
| `.gitignore` | edited | Ignore the local `.ph-venv` validation venv (not part of the app). |
| `posthog-setup-report.md` | created | This report. |

### Key code locations in `app.py`

| What | Line |
|---|---|
| `POSTHOG_API_KEY` / `POSTHOG_HOST` env vars + server client (`sync_mode`) + `_PostHogStub` | ~253 |
| `ph_server_capture(distinct_id, event, properties)` helper | ~285 |
| `_ph_queue_event()` + `_posthog_head()` (client snippet, identify, queued events) | ~5816 / ~5835 |
| Snippet injected into shared `_page()` `<head>` | ~5982 |
| Snippet injected into standalone landing-page `<head>` | ~11112 |
| `/ingest` reverse proxy route (`posthog_proxy`) | ~18079 |

---

## 2. Events instrumented

`$pageview` and autocapture (clicks, form fields, etc.) are automatic via the
client snippet — not listed below.

| Event | Description | File:line | Client/Server | distinct_id source | Key properties |
|---|---|---|---|---|---|
| `user_signed_up` | New account created | app.py:11732 | Server | new user id (`cur.lastrowid`) | `method: "email"` |
| `user_logged_in` | Successful login | app.py:11753 | Server | user id (`row["id"]`) | `method: "email"` |
| `chances_viewed` | Viewed admissions chances for a school | app.py:7262 | Client (queued, emitted in head) | anon id → user id (after `identify`) | `school_slug`, `odds_tier` (Dream/Reach/Target/Safety), `confidence` |
| `upgrade_viewed` | Viewed `/upgrade` paywall | app.py:17945 | Client | anon id → user id | `logged_in` |
| `checkout_started` | Clicked the Stripe subscribe button (logged-in only) | app.py:17962 | Client (button `onclick`) | user id | `plan: "monthly"`, `interval: "monthly"` |
| `subscription_activated` | Webhook `checkout.session.completed` granted premium | app.py:18192 | Server | our user id (from `client_reference_id`, or email→user lookup) | `plan`, `interval`, `amount_cents`, `currency` |
| `subscription_canceled` | Webhook `customer.subscription.deleted` revoked premium | app.py:18220 | Server | our user id (looked up by Stripe customer/subscription id before revoke) | `plan`, `interval` |

**Identify:** on every authenticated page load, the head snippet calls
`posthog.identify(<user_id>)` so the anonymous pre-signup history merges into
the known user. `distinct_id` is always the internal numeric user id (never
email). The Stripe webhook maps `client_reference_id` / customer id → our user
id before capturing, so server and client events share one identity.

**Server flush:** the server client is built with `sync_mode=True` (+ `flush_at=1`,
`flush_interval=0`) and `ph_server_capture()` also calls `flush()`, so events
send inside the short-lived Flask request — no background thread to miss.

---

## 3. Env vars to set in Railway

| Var | Value | Public? | Required? |
|---|---|---|---|
| `POSTHOG_API_KEY` | Your PostHog **Project API key** (starts `phc_…`) | Yes — safe in client JS | Yes, to turn analytics on |
| `POSTHOG_HOST` | `https://us.i.posthog.com` (US cloud) or `https://eu.i.posthog.com` (EU). Defaults to US if unset. | n/a | Optional |

Get the Project API key from PostHog → **Settings → Project → Project API Key**.
It is the publishable key (`phc_…`), not the personal/secret key — do **not**
use a personal API key here.

Until `POSTHOG_API_KEY` is set, the whole integration is dormant: deploying now
changes nothing user-facing.

---

## 4. CSP / proxy changes

- **Reverse proxy (`/ingest`):** the client is configured with
  `api_host: '/ingest'`, so the browser sends all analytics traffic to
  `candoradmit.com/ingest/*` (same-origin). The new `posthog_proxy` route
  forwards `/ingest/static/*` to the PostHog **assets** CDN
  (`us-assets.i.posthog.com`) and everything else to `POSTHOG_HOST`. This makes
  PostHog first-party, which dodges most tracker blockers and keeps the page
  CSP simple. `ui_host` still points at the real PostHog host so toolbar links
  resolve. When the key is unset, `/ingest` returns 404.

- **CSP — no change.** The app's `Content-Security-Policy` (in
  `_allow_framer_embed`) intentionally declares **only** `frame-ancestors`.
  Because there is no `default-src` / `script-src` / `connect-src`, the browser
  imposes no script or XHR origin restriction, and PostHog rides entirely on the
  same-origin `/ingest` proxy — so it needs no `script-src`/`connect-src`
  allowance. I deliberately did **not** add those directives: introducing a
  `script-src` or `connect-src` now would newly restrict every existing inline
  handler and third-party script on the site and risk breaking unrelated pages.

---

## 5. Suggested PostHog dashboards / insights

1. **Acquisition → paid funnel** (the core one). Funnel insight, ordered steps:
   `$pageview` (landing) → `user_signed_up` → `chances_viewed` →
   `upgrade_viewed` → `checkout_started` → `subscription_activated`. Shows
   exactly where people fall out — e.g. how many who view their chances ever
   reach the paywall, and paywall→checkout→paid drop-off.

2. **Free → paid conversion.** Conversion of `user_signed_up` → (within 30 days)
   `subscription_activated`, broken down by whether the user fired
   `chances_viewed` and by `odds_tier` (do people who see "Reach"/"Dream" odds
   convert more than "Safety"?). Pairs well with a `checkout_started` →
   `subscription_activated` conversion rate to spot Stripe drop-off.

3. **Retention + churn.** Retention insight keyed on `chances_viewed`
   (do users come back to run more schools?), plus a churn view trending
   `subscription_canceled` against `subscription_activated` over time
   (net new paid subscribers per week).

---

## 6. What's skipped and why

- **TikTok-export / profile-export standalone HTML shells** (~app.py:12039,
  14225): internal creator tools, not user product surfaces — no snippet added,
  per the discovery notes.
- **CSP `script-src`/`connect-src` directives:** not added on purpose (see §4) —
  the same-origin proxy makes them unnecessary, and adding them risks breaking
  existing inline scripts.
- **OAuth / magic-link signup:** none exists — auth is email+password only, so
  only `method: "email"` is emitted.
- **Annual plan property on checkout:** there is no monthly/annual picker (a
  single $3/mo plan), so `plan`/`interval` are hardcoded `monthly`.
- **`subscription_payment_failed` / `paywall_seen`:** not in the approved
  taxonomy for this pass; the webhook deliberately does not revoke on a single
  failed payment (Stripe dunning), so no event there.

---

## Next steps for you

1. Create a free PostHog account at https://posthog.com (US or EU cloud).
2. Copy the **Project API key** (`phc_…`) from Settings → Project.
3. In Railway, set `POSTHOG_API_KEY=phc_…` (and optionally `POSTHOG_HOST` if you
   chose EU cloud).
4. Deploy (`git push`). Verify events land in PostHog → Activity, then build the
   funnel in §5.
