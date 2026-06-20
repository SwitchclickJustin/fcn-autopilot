# Chat Avenue Platform Adapter — Spec

Status: draft (recon complete 2026-06-20)
Goal: run the existing FCN auto-pilot AI on a **second platform**, Chat Avenue
(`adultchat.chat-avenue.com`), via a clean `Platform` adapter — no funnel rebuild.

---

## 1. Why this is feasible (recon summary)

Live recon (guest session, 2026-06-20) confirmed Chat Avenue is structurally a near-clone
of FreeChatNow:

| Capability | Chat Avenue | Notes |
|---|---|---|
| Guest login | `username + gender + DOB + Cloudflare Turnstile` | no registration; Turnstile == FCN-class, BU Cloud clears it |
| Rooms | multi-room lobby (`Adult Chat` 964, `Taboo` 270, `Seniors` 58, …) | live counts; some guest-OK, some registered-only |
| Message box | `<input id="content" placeholder="Type here…">` + send button | cleaner than FCN's writer-input |
| User list | right panel, per-user row + action icon | the action icon opens a PM |
| **Private messages** | yes — users openly solicit "**pm me**" | **the DM funnel ports directly** |
| Photos | post inline in the feed | photo-carried-handle tactic ports |
| Moderation | topic rule "**No socials, phone numbers, selling**" + live Moderator | open handle-drop → mod ban (same as FCN "scammer") |
| Existing bots | yes (`Kristina: …CLICK HERE TO ACCEPT`) | proven doable + competition |

The **AI brain reuses ~as-is**: prompts, sanitizers (`_strip_ai_tells`, retired/normalize/
tighten handle guards, chat-name backstop), TG obfuscation, photo slicing, attribution,
stats, persona model, orchestrator loop. Only the **browser layer** is FCN-specific.

---

## 2. Architecture: extract a `Platform` interface

Today `BotWorker`/`BotOrchestrator` bake FCN page logic directly into the methods. Refactor
so the FCN-specific methods become one implementation of a `Platform` protocol, and Chat
Avenue is a second implementation. The orchestrator loop + AI stay platform-agnostic.

```
BotWorker (state) ── platform: Platform
                       ├── FcnPlatform        (existing code, moved behind the interface)
                       └── ChatAvenuePlatform (new)

_auto_pilot_tick / _run_auto_pilot / all sanitizers / attribution  → UNCHANGED
```

### The `Platform` interface (methods to extract from `app/browser.py`)

| Method | FCN today | Purpose |
|---|---|---|
| `login(worker) -> bool` | `_cdp_guest_login` | guest login + clear CF/captcha; land in a chat-ready state |
| `join_rooms(worker, n) -> list` | `_join_top_rooms` | open the room directory, join top-traffic guest-OK rooms |
| `open_conversation(page, ref) -> bool` | `_open_conversation` | switch to a room or DM, **verify it became active** |
| `list_conversations(page) -> list` | `_list_conversations` | enumerate room tabs + DM tabs `{href,is_dm,active,unseen,text}` |
| `read_chat(worker, limit) -> list` | `BotWorker.read_chat` | read recent messages in the active thread |
| `send_message(worker, text, fast) -> bool` | `BotWorker.send_message` | type + send into the active thread |
| `send_photo(worker, b64, name) -> bool` | `BotWorker.send_photo` | post an image into the active thread |
| `open_dm(worker, user) -> bool` | (FCN DMs are tabs) | open/start a PM with a specific user |
| `read_partner_info(page) -> dict` | `_read_dm_partner_info` | age/country for opener personalization (optional) |
| `is_banned(worker) -> bool` | `_looks_banned`/`_is_blocked_page` | detect kick/ban/logout |
| `recover(worker) -> bool` | `_recover` | teardown → fresh IP/UA → re-login |

`_provision_and_connect` (BU Cloud browser + CF clearance) is **shared** — both platforms
use the same residential/auto-solve stack. The only difference is the post-provision
`login()` flow and the per-page selectors.

---

## 3. Chat Avenue adapter — concrete mapping (from recon)

Chat app entry point: `https://adultchat.chat-avenue.com/` (the `www.chat-avenue.com/
adultchat.html` page just iframes this).

### login()
1. `goto https://adultchat.chat-avenue.com/`
2. click **Guest login** — `.intro_guest_btn`
3. fill the revealed form:
   - `#guest_username` (text) ← `worker.login_name` (reuse `_unique_username`)
   - `#guest_gender` (select) ← Female
   - `#date_day` / `#date_month` / `#date_year` (selects) ← random 18+ DOB
   - `cf-turnstile-response` (hidden) ← **BU Cloud auto-solves Turnstile** (same as FCN CF)
4. click submit — `.theme_btn.full_button` ("Login")
5. wait for the lobby (room directory) to render

### join_rooms()
- open the left menu (hamburger ☰, top-left) → **"Room list"**
- room rows show name + description + **live user count** + guest/registered icon
- pick high-traffic **guest-OK** rooms (`Adult Chat` ~964; avoid registered-only `DICE`,
  `Adult Chat - Desktop Version`)
- click a room row to join

### read_chat()
- message feed = left panel; each row has username + text (+ inline image for photo posts)
- selectors TBD during build (obfuscated classes; map via the recon harness — §5)

### send_message()
- `#content` (placeholder "Type here…") → type → click the paper-plane **send button**
- NOTE: validate whether Enter submits or only the button (FCN needed Enter + button fallback)

### open_dm() / DMs
- right-panel user rows each have a per-user **action icon** → opens a private thread
- users actively solicit "pm me", so inbound PMs will also arrive (mirror FCN hot-DM handling)
- DM thread read/send DOM TBD (map via recon)

### send_photo()
- photos render inline in the feed (seen live) → map the upload affordance (attach/drag) during build

### is_banned() / recover()
- watch for: kicked-from-room, mod warning, forced logout / re-login screen, room rules block
- reuse the FCN `_recover` shape (fresh browser → new IP/UA → new guest username)

---

## 4. Anti-ban (reuse, do not rebuild)

Room rule: **"No socials, phone numbers, selling"** + a live Moderator. Open Telegram-handle
drops will draw mod bans — the same class as FCN's "scammer" bans. Everything we already
built applies unchanged:

- handle obfuscation (`_obfuscate_handle`, ZWSP), varied TG spellings (`_TG_TOKENS_*`)
- photo-carried handle + 3-message redirect cycle
- retired/normalize/chat-name handle guards, `_tighten_dm`, stage-direction strip, bracket strip
- per-agent photo slicing, randomized filenames, soft usernames

The Latina-from-Miami persona, prompts, and DM phase engine carry over verbatim.

---

## 5. Unknowns to resolve during build (map with the recon harness)

The remaining gaps are all **DOM selectors**, mapped the same way recon was done — drive a
guest session via the Chrome extension (or the BU Cloud `/debug/recon` endpoint) and read the
live DOM:

1. message-feed row structure (username / text / timestamp / inline image)
2. PM-open click target + PM-thread read/send DOM
3. photo upload mechanism (attach button vs drag-drop)
4. ban/kick/mod-warning signals + the re-login screen shape
5. room-join confirmation signal (for the `open_conversation` active-verify)
6. unseen-badge signal for hot-DM detection

---

## 6. Phasing & effort

- **Phase 0 — refactor (no behavior change):** extract `Platform` protocol; move FCN code
  into `FcnPlatform`; route `BotWorker` through `worker.platform.*`. Verify FCN unchanged.
- **Phase 1 — Chat Avenue login + rooms:** `login()` + `join_rooms()`; confirm an agent
  lands in `Adult Chat` as a guest and stays.
- **Phase 2 — read + broadcast:** `read_chat()` + `send_message()` + `send_photo()`; agent
  broadcasts with the existing group prompts + anti-ban.
- **Phase 3 — PMs:** `open_dm()` + DM read/send; wire the DM phase engine + handle drop.
- **Phase 4 — bans:** `is_banned()` + `recover()` + the self-heal loop.
- **Phase 5 — integrate:** per-platform agent launch in the orchestrator + dashboard
  (platform label on agents/feed/stats); attribution stays per-image.

Rough size: Phase 0 is the bulk of the risk (a careful refactor of a 2700-line file). Phases
1–4 are each a handful of selector-mapping + adapter methods. No new anti-bot or AI work.

---

## 7. Risks

- **Refactor risk** — Phase 0 touches the whole browser layer; do it behind the interface with
  FCN behavior unchanged before adding Chat Avenue.
- **DOM drift** — obfuscated class names; selectors may change. Prefer stable ids (`#content`)
  and text/role-based finds over brittle classes.
- **Mod bans** — active moderation on the "no socials" rule; same mitigation as FCN, but tune
  per-platform aggression.
- **Attribution** — same shared-handle limit as FCN (statistical only) unless we run a distinct
  handle per platform (recommended: gives clean per-platform conversion numbers).
- **Operational load** — two platforms = 2× tuning + ban surface. Recommend proving FCN to the
  20-conv/hr/agent target first, then cloning with a battle-tested funnel.

---

## 8. Recommendation

Build-ready once we accept the Phase-0 refactor. Strategic call: **prove FCN first**, then
execute this. The opportunity is real (target-rich rooms, users soliciting PMs, FCN-class
anti-bot we already beat) and the adapter is bounded work, not a rebuild.
