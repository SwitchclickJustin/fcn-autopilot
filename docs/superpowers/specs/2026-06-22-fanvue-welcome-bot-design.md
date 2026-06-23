# Fanvue Welcome Bot — Design Spec

**Date:** 2026-06-22
**Status:** Approved design, pre-implementation
**Author:** Justin + Claude

## 1. Goal

A standalone service that does two jobs:

**A. New-subscriber welcome** — for every new Fanvue subscriber:
1. Sends an **instant Telegram notification** to the operator ("new sub: @handle on creator X").
2. **~60 seconds after Fanvue's own generic auto-welcome** appears in the chat, sends a
   custom welcome message + a free photo, designed to push the fan toward Telegram.

**B. Unanswered-message watchdog** — across ALL chats (any fan, new or old):
3. If a chat has gone **unanswered for ≥5 minutes** (fan messaged, operator hasn't replied),
   send a Telegram notification so someone goes and responds. Once per unanswered message.

Must work in **both** agency mode (many managed creators under one agency token) and
single-creator mode (one creator's own token).

## 1b. Deployment & branding

- **Product name / domain:** **BoltChatter.com**.
- **v1 (this build):** headless backend service on Railway. No public web UI; only a `/health`
  endpoint. The domain can be pointed at the service or reserved for the future dashboard.
- **Future:** a **downloadable PWA** dashboard (operator console — view subs, alerts, override
  copy). Out of scope for v1. When built, the PWA **install walkthrough must be copied from
  Papacito** (`~/Projects/papacito/artifacts/papacito/public/install.html`), per standing
  instruction — not a static GIF/MP4.

## 2. Why standalone

The existing FCN Auto-Pilot app is built entirely on browser automation (Browser Use Cloud
+ CDP). Fanvue exposes a clean REST API, so this shares no infrastructure with the browser
bots. It is a small, separate async Python service deployed on Railway.

## 3. Verified API facts (from live OpenAPI spec, version `2025-06-26`)

Base URL `https://api.fanvue.com`. All requests send `Authorization: Bearer <token>` and
`X-Fanvue-API-Version: 2025-06-26`. Rate limit: **100 requests / 60s** (agency-wide),
honors `Retry-After` + `X-RateLimit-*` headers on 429.

### Detect new subscribers
- **Agency:** `GET /agencies/subscribers?size=50` (scopes `read:agency`, `read:creator`).
  Newest-first. Each row: `uuid` (subscriber), `displayName`, `handle`, `nickname`,
  `avatarUrl`, `creatorUuid`, `subscribedAt` (date-time), `expiresAt`, `registeredAt`.
- **Single creator:** `GET /subscribers?sortField=subscribedAt&sortDirection=desc&size=50`
  (scope implicit to token). Each row: `uuid`, `displayName`, `handle`, `nickname`,
  `avatarUrl`, `registeredAt`. **Note:** does NOT return `subscribedAt` value (only
  sortable by it). This is fine — we anchor timing on the welcome message, not subscription time.

### List managed creators (agency mode)
- `GET /creators?size=50` (scope `read:creator`). Each: `uuid`, `displayName`, `handle`, `role`.

### Read a fan's chat to find the generic welcome
- **Agency:** `GET /creators/{creatorUserUuid}/chats/{userUuid}/messages`
- **Creator:** `GET /chats/{userUuid}/messages`
- Each message has a `type` enum. The relevant values:
  `AUTOMATED_NEW_SUBSCRIBER` (the generic welcome we anchor on),
  `AUTOMATED_RE_SUBSCRIBED`, plus others.
- Message fields: `uuid`, `text`, `sentAt` (labelled `date` — **may be date-only; do not
  depend on time-of-day**), `sender`, `recipient`, `type`, `hasMedia`, `mediaUuids`,
  `pricing`, `isRead`.

### Send our welcome
- **Agency:** `POST /creators/{creatorUserUuid}/chats/{userUuid}/message` (scopes `write:chat`, `read:creator`)
- **Creator:** `POST /chats/{userUuid}/message` (scope `write:chat`)
- Body: `{ "text": string(1..5000)|null, "mediaUuids": [uuid], "price": number|null, "templateUuid": uuid|null }`
- `price: null` = **free**. (PPV floor is 300, i.e. ~$3.00 — not used here.)
- Returns `{ messageUuid }`.

### Detect unanswered chats (watchdog)
- **Creator:** `GET /chats?filter=not_answered&sortBy=most_recent_messages&size=50`
- **Agency:** `GET /creators/{creatorUserUuid}/chats?filter=not_answered&size=50` (per managed creator)
- Fanvue **pre-computes** the unanswered state — `filter=not_answered` returns only chats where
  the fan messaged and the operator hasn't replied (there's also `sortBy=most_unanswered_chats`).
  We do NOT have to reconstruct "who replied last."
- Each chat row gives us everything the alert needs:
  - `user`: `uuid`, `handle`, `displayName`, `nickname` (the fan)
  - `lastMessage`: `text`, `uuid` (stable dedupe key), `sentAt` (labelled `date` — may be
    date-only), `senderUuid` (sanity-check it equals `user.uuid`), `type`
  - `lastMessageAt`, `isRead`, `unreadMessagesCount`
- Cost: **1 request per creator per loop.** Cheap.

### Resolve the welcome photo
- **Agency:** `GET /creators/{creatorUserUuid}/vault/folders/{folderName}/media` (scopes `read:creator`, `read:media`)
- **Creator:** `GET /vault/folders/{folderName}/media` (scope `read:media`)
- Convention: a vault folder named `Welcome`. Bot sends the newest media UUID in it.
  If the folder is empty/missing → send text only (no media), log a warning.

### NOT available (design constraints)
- **No webhooks / SSE / websockets.** Detection is poll-only.
- **No documented notification event-type enum** (`GET /notifications` `eventType` is an
  opaque integer) — so we use subscriber set-diff, not the notification feed.

## 4. Architecture

Single async Python process, three cooperating loops over a small SQLite state file,
plus a tiny `/health` endpoint for Railway.

```
                  ┌───────────────────────────────────────────────┐
  every ~30s ───▶ │ POLLER                                          │
                  │  source.list_recent_subscribers()               │
                  │  diff rows vs `seen` (creator_uuid, sub_uuid)   │
                  │  for each NEW sub:                              │
                  │    1. telegram.notify(...)                      │
                  │    2. insert job  status=AWAITING_GENERIC       │
                  │    3. insert seen row                           │
                  └───────────────────────────────────────────────┘
                  ┌───────────────────────────────────────────────┐
  every ~20s ───▶ │ ANCHOR LOOP   (jobs WHERE status=AWAITING_GENERIC)
                  │  source.find_generic_welcome(creator, sub)      │
                  │    found?  → fire_at = now + 60s; status=PENDING│
                  │    not yet → leave; bump attempts; give up after│
                  │              ANCHOR_TIMEOUT (e.g. 30 min)       │
                  └───────────────────────────────────────────────┘
                  ┌───────────────────────────────────────────────┐
  every ~10s ───▶ │ SCHEDULER     (jobs WHERE status=PENDING AND    │
                  │                fire_at <= now)                  │
                  │  photo = source.resolve_welcome_photo(creator)  │
                  │  source.send_message(creator, sub, text, photo) │
                  │  status=SENT  (or FAILED+retry w/ backoff)      │
                  └───────────────────────────────────────────────┘
                  ┌───────────────────────────────────────────────┐
  every ~60s ───▶ │ UNANSWERED WATCHDOG  (independent of new subs) │
                  │  for each creator:                             │
                  │    source.list_unanswered_chats(creator)        │
                  │  upsert unanswered_watch (key: last_message_uuid)
                  │    new last_message_uuid → reset first_seen,    │
                  │                            notified=false       │
                  │  if now-first_seen >= 5m AND not notified:      │
                  │    telegram.notify_unanswered(...); notified=t  │
                  │  rows no longer returned → answered → delete    │
                  └───────────────────────────────────────────────┘
```

## 5. Mode abstraction

A `SubscriberSource` interface with two implementations; everything downstream is shared.

```
class SubscriberSource(Protocol):
    async def list_recent_subscribers() -> list[NewSub]      # NewSub(creator_uuid, sub_uuid, display_name, handle)
    async def find_generic_welcome(creator_uuid, sub_uuid) -> bool   # True once AUTOMATED_NEW_SUBSCRIBER seen
    async def resolve_welcome_photo(creator_uuid) -> str | None      # media uuid or None
    async def send_message(creator_uuid, sub_uuid, text, media_uuid) -> str
    async def list_unanswered_chats(creator_uuid) -> list[UnansweredChat]
        # UnansweredChat(creator_uuid, user_uuid, handle, display_name, last_message_uuid, last_message_text)
```

In agency mode, `list_unanswered_chats` is called once per managed creator (from `GET /creators`).
In creator mode it's called once against `/chats`. Watchdog and welcome share the same creator list.

- `AgencySource` → `/agencies/subscribers`, `/creators/{c}/chats/{u}/...`, `/creators/{c}/vault/...`.
  In agency mode `creator_uuid` is the real creator UUID from each row.
- `CreatorSource` → `/subscribers`, `/chats/{u}/...`, `/vault/...`.
  In creator mode `creator_uuid` is a fixed sentinel (the token's own creator; `"self"`).

Mode is chosen by `FANVUE_MODE=agency|creator`.

## 6. Data model (SQLite)

```
seen(
  creator_uuid TEXT, sub_uuid TEXT, first_seen TEXT,
  PRIMARY KEY (creator_uuid, sub_uuid)
)

welcome_jobs(
  creator_uuid TEXT, sub_uuid TEXT,
  display_name TEXT, handle TEXT,
  status TEXT,            -- AWAITING_GENERIC | PENDING | SENT | FAILED | EXPIRED
  fire_at TEXT,           -- null until anchored
  attempts INTEGER DEFAULT 0,
  created_at TEXT, updated_at TEXT,
  PRIMARY KEY (creator_uuid, sub_uuid)
)

unanswered_watch(
  creator_uuid TEXT, user_uuid TEXT,
  last_message_uuid TEXT,   -- the fan message we're watching; changes => new alert cycle
  handle TEXT, display_name TEXT, last_message_text TEXT,
  first_seen_at TEXT,       -- when WE first observed this chat as unanswered (anchor)
  notified_at TEXT,         -- null until first alert; set to send time (supports renotify)
  PRIMARY KEY (creator_uuid, user_uuid)
)

meta(key TEXT PRIMARY KEY, value TEXT)   -- e.g. bootstrap_done, service_first_start
```

**Watchdog dedupe:** keyed on (creator, user). When the `last_message_uuid` for that chat
changes (fan sent a newer message), `first_seen_at` resets and `notified` clears — so each
distinct unanswered message gets at most one alert. When a chat stops appearing in
`not_answered` (operator replied), its row is deleted; if the fan messages again later it's a
fresh row → fresh alert.

`seen` is the dedupe key: **welcome once per (creator, subscriber) ever.** Re-subscribes
are not re-welcomed in v1.

## 7. Cold-start guard (critical)

On first ever boot the `seen` table is empty, so naively every existing subscriber looks
"new." Guard:

1. On first run (when `meta.bootstrap_done` is unset): page through current subscribers and
   insert them all into `seen` **without** creating any jobs. Set `bootstrap_done=true`.
2. Thereafter, only subscribers absent from `seen` create jobs.

This guarantees the entire existing fanbase is never mass-welcomed.

## 8. Welcome message

Default template (per-creator overridable via config):

> Love, so happy you actually came! 🥰 What's your TG name btw? I feel way safer sharing pics and videos here and we can really get to know each other 😈

- Optional `{name}` placeholder → filled from `display_name`/`nickname`, falls back to "" / "love".
- Sent **free** (`price: null`) with the `Welcome` folder photo attached.

## 9. Config / env vars

| Var | Required | Notes |
|-----|----------|-------|
| `FANVUE_API_TOKEN` | ✅ | Bearer token (OAuth access token) |
| `FANVUE_MODE` | ✅ | `agency` or `creator` |
| `FANVUE_API_VERSION` | — | default `2025-06-26` |
| `TELEGRAM_BOT_TOKEN` | ✅ | Bot API token |
| `TELEGRAM_CHAT_ID` | ✅ | operator chat/channel id for notifications |
| `WELCOME_DELAY_SECONDS` | — | default `60` |
| `WELCOME_FOLDER` | — | vault folder name, default `Welcome` |
| `WELCOME_TEXT` | — | overrides default copy |
| `POLL_INTERVAL_SECONDS` | — | default `30` |
| `UNANSWERED_THRESHOLD_MINUTES` | — | default `5` (alert after this long unanswered) |
| `UNANSWERED_POLL_INTERVAL_SECONDS` | — | default `60` (watchdog loop) |
| `UNANSWERED_RENOTIFY_MINUTES` | — | default `0` = single alert; >0 = remind every N min while still unanswered |
| `UNANSWERED_SUBSCRIBERS_ONLY` | — | default `false`; if true, also pass `filter=subscribers` |
| `WATCHDOG_ENABLED` | — | default `true`; lets the welcome bot run without the watchdog |
| `ANCHOR_INTERVAL_SECONDS` | — | default `20` |
| `ANCHOR_TIMEOUT_MINUTES` | — | default `30` (give up waiting for generic welcome) |
| `DB_PATH` | — | default `/data/fanvue.db` (Railway volume) |
| `DRY_RUN` | — | if true, log sends instead of calling the API |
| `WELCOME_TEXT_<creatorUuid>` | — | per-creator copy override (agency mode) |

**Auth note:** OAuth access tokens typically expire. v1 reads a static `FANVUE_API_TOKEN`.
If the token proves short-lived, a follow-up adds refresh-token support (client_id/secret +
refresh_token). Flagged as a known risk, not built in v1.

## 10. Rate limiting & resilience

- One shared async HTTP client with a token-bucket capped under 100/60s; on 429, sleep
  `Retry-After` then retry.
- Telegram failures must not block the welcome send, and vice versa (independent try/except).
- **Telegram helper reuses the Papacito/Aurora pattern** (`artifacts/api-server/src/utils/telegramNotify.ts`
  in both repos): `POST https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage` with
  `{ chat_id, text, parse_mode: "HTML" }`, every interpolated value HTML-escaped. Contract:
  env-gated (missing token/chat → log warning + no-op, never throw), best-effort, never raises.
  Ported 1:1 from TypeScript to an async `httpx` helper. Reuse the **same shared bot token +
  chat id** as Papacito/Aurora so all alerts land in one Telegram chat; only the header differs
  (e.g. `🟣 New Fanvue Sub!`). See memory `[[fcn-telegram-shared-bot]]` /
  `[[aurora-sale-telegram-alert]]`.
- **Two alert types, distinct headers:**
  - New sub: `🟣 New Fanvue Sub!` + `@handle` + creator (agency).
  - Unanswered: `⏰ Unanswered {N}m — reply needed!` + `@handle` (+ creator) + the fan's
    last message text (escaped, truncated ~200 chars). Call-to-action tone.
- All loops survive individual-item errors (per-job try/except; a bad job → FAILED, not a crash).
- State is durable in SQLite, so a restart resumes pending/awaiting jobs without double-sending
  (status transitions are the idempotency guard).

## 11. Edge cases

- **>50 new subs between polls:** paginate `/agencies/subscribers` until a row already in
  `seen` is hit (watermark), then stop.
- **Generic welcome never appears** (creator disabled it): job EXPIRES after
  `ANCHOR_TIMEOUT_MINUTES`; log + optional Telegram heads-up. (Decision: do NOT send ours if
  the generic never fired, to preserve ordering intent.)
- **Fan replies/messages before we send:** irrelevant to v1; we still send the welcome.
- **`sentAt` date-only:** we anchor on detection time (`now + 60s`), never on `sentAt`, so this
  doesn't matter.
- **Duplicate detection across restarts:** `seen` + job status prevent re-notify and re-send.

**Watchdog-specific:**
- **`lastMessage.sentAt` date-only:** we anchor the 5-min timer on `first_seen_at` (our own
  first observation of the chat as unanswered), not on `sentAt` — so date granularity doesn't
  matter. Trade-off: if a message was already old when the watchdog first sees it (e.g. after
  downtime), the alert can lag by up to one extra threshold. Acceptable.
- **Operator's own bot replying:** our welcome send (job B) counts as an operator reply, so a
  freshly-welcomed chat won't trip the watchdog unless the fan messages again. Correct behaviour.
- **Last message is from the operator but filter still returns it:** guard by checking
  `lastMessage.senderUuid == user.uuid`; skip if not (defensive — shouldn't happen).
- **Restart:** `unanswered_watch` is durable, so `notified` flags survive and we don't
  re-alert already-alerted chats. Rows for now-answered chats are reaped on the next loop.
- **Welcome alert vs watchdog alert for the same chat:** distinct Telegram messages with
  distinct headers; the new-sub ping is informational, the watchdog ping is a call to action.

## 12. Testing strategy (TDD)

- **FakeSource** implementing `SubscriberSource` + a fake clock to test:
  - cold-start seeding (no jobs created on bootstrap),
  - new-sub detection + dedupe,
  - anchor transition (AWAITING_GENERIC → PENDING only after generic seen),
  - scheduler fires at/after `fire_at`, marks SENT,
  - anchor timeout → EXPIRED,
  - restart resumes without double-send,
  - **watchdog:** chat unanswered < threshold → no alert; ≥ threshold → exactly one alert;
    new `last_message_uuid` → new alert; chat answered (drops from `not_answered`) → row reaped,
    no alert; restart preserves `notified` (no duplicate alert); `senderUuid != user.uuid` skipped.
- **DRY_RUN** integration mode: run against the live API read endpoints, log intended sends.
- One real smoke test: confirm `sentAt` granularity and a real send on a throwaway sub
  before going live.

## 13. Out of scope (future)

- **v2 reply-capture:** poll welcomed fans' chats, extract the Telegram handle they reply
  with, push it to the operator's Telegram. State schema already supports adding this.
- **Per-traffic-source welcome variants** via `/creators/{c}/tracking-links/...` metadata.
- **PPV drip** of vault content during conversation.
- **OAuth token auto-refresh.**

## 14. Risks / notes

- ⚠️ **ToS:** directing fans off-platform to Telegram is a commonly enforced Fanvue ToS
  violation (bypasses their payment cut). The API permits it; the risk is account-level.
  Operator has accepted this; recorded here for completeness.
- `sentAt` time-of-day granularity unconfirmed — design avoids depending on it.
- OAuth token lifetime unconfirmed — v1 uses a static token.
