"""Chat Avenue (adultchat.chat-avenue.com) broadcast adapter.

GROUP-ONLY, TEXT-ONLY: log in as a guest, join a high-traffic guest room, and broadcast
every 20-45s — the obfuscated handle rides in the TEXT every message (no photos on Chat
Avenue), reusing the FCN AI brain — BU Cloud provisioning, LLM, sanitizers, handle
obfuscation. No DM engine, no photos: the Chat Avenue funnel is broadcast -> Telegram add
(confirmed fast in manual tests).

Selectors mapped live 2026-06-20 (Chrome recon):
  entry         https://adultchat.chat-avenue.com/
  guest button  .intro_guest_btn
  guest form    #guest_username, #guest_gender, #date_day/#date_month/#date_year,
                Cloudflare Turnstile (auto-solved by BU Cloud residential)
  submit        .theme_btn.full_button
  room list     hamburger menu -> "Room list" -> click a room row (e.g. "Adult Chat" ~964)
  send input    #content  (placeholder "Type here...")
  send button   button#submit_button.send_btn   (paper-plane)

Each browser step is marked LIVE-TEST: validate against the running site (deploy +
watch the live_url) before trusting it. Built incrementally.
"""
import asyncio
import logging
import random
import time

from app import browser as fcn  # reuse helpers + BU Cloud provisioning

logger = logging.getLogger(__name__)

CHAT_URL = "https://adultchat.chat-avenue.com/"
# Chat Avenue sites the blaster rotates agents across by slot — same guest-login flow + same
# room-selection page on each. slot 0 -> site 0, slot 1 -> site 1, round-robin.
CA_SITES = [
    "https://adultchat.chat-avenue.com/",
    # Sex Chat: real guest login lives here (Username + Turnstile only — NO gender/DOB, unlike
    # adultchat). The adaptive login handles the missing gender/DOB fields.
    "https://www.chat-avenue.com/sexchat/",
]
# High-traffic, guest-allowed rooms (from recon). Avoid registered-only (DICE, Desktop).
GUEST_ROOMS = ["Adult Chat", "Taboo", "Seniors Room"]
BROADCAST_MIN_S, BROADCAST_MAX_S = 15, 25   # default cadence when the persona has none
BROADCAST_MAX_CHARS = 240                    # Chat Avenue truncates long messages — stay under it


def _broadcast_interval(persona: dict) -> tuple:
    """Seconds between broadcasts (min, max), read from the persona's cooldown_min/max so
    operators tune cadence per-persona. Falls back to 15-25s when unset/invalid, and never
    blasts faster than 5s."""
    try:
        lo = int(persona.get("cooldown_min") or BROADCAST_MIN_S)
        hi = int(persona.get("cooldown_max") or BROADCAST_MAX_S)
    except (TypeError, ValueError):
        lo, hi = BROADCAST_MIN_S, BROADCAST_MAX_S
    lo = max(5, lo)
    hi = max(lo, hi)
    return lo, hi


class ChatAvenueWorker:
    """One Chat Avenue broadcaster. Composes a BotWorker for the BU Cloud browser +
    send_photo + the shared `_page`, so we reuse FCN's provisioning untouched."""

    def __init__(self, persona: dict, agent_id: str, slot: int = 0, agent_total: int = 1):
        self.persona = persona
        self.agent_id = agent_id
        self.slot = slot
        self.agent_total = max(1, agent_total)
        self.login_name = ""
        self.status = "init"
        self.room = ""
        self.started_at = time.time()
        self.send_oks = 0
        self.send_attempts = 0
        self._recent_msgs: list = []          # no-repeat memory
        self._task = None
        self._stop = False
        self._orchestrator = None       # set by start_multi_chatavenue; feeds the dashboard
        self.tabs: list = []            # one {page, site, room} per Chat Avenue site, one browser
        # Cadence (seconds between blasts) from the persona's cooldown_min/max, 15-25s default.
        self.cd_min, self.cd_max = _broadcast_interval(persona)
        # Which Chat Avenue site this agent blasts (round-robin by slot across CA_SITES).
        self.site_url = CA_SITES[slot % len(CA_SITES)]
        # Composition: a BotWorker carries the CDP page + send_photo + slicing slot.
        self._bw = fcn.BotWorker(persona)
        self._bw.agent_id = agent_id
        self._bw.slot = slot
        self._bw.agent_total = self.agent_total
        # Distinct US Decodo IP per agent (first 50 entries = us.decodo.com) so every guest
        # registration comes from a fresh, unused IP — avoids Chat Avenue's per-IP cap.
        # _proxy_idx advances on each kick-recovery to grab a fresh IP.
        self._proxy_idx = slot % 50
        self.custom_proxy = fcn.DECODA_PROXIES[self._proxy_idx]

    @property
    def _page(self):
        return self._bw._page

    # ── login ────────────────────────────────────────────────────────────────
    async def login(self) -> bool:
        """Guest login. LIVE-TEST: Turnstile clears on BU Cloud residential; the gender/DOB
        selects are selectboxit-styled — select_option drives the underlying <select>, but
        confirm the form reads it (may need to also click the styled option)."""
        page = self._page
        if not page:
            return False
        try:
            logger.info(f"[{self.agent_id}] CA site: {self.site_url}")
            await page.goto(self.site_url, wait_until="domcontentloaded", timeout=45000)
            # Intro "Guest login" button — present on the chat-app subdomains, absent on some
            # variants; don't hard-fail if it isn't there.
            try:
                await page.click(".intro_guest_btn", timeout=8000)
            except Exception:
                # Variant: a button/text labelled "Guest login" opens the modal.
                clicked = await page.evaluate("""() => {
                    const b = Array.from(document.querySelectorAll('button,a,div,span')).find(e =>
                        /^\\s*guest login\\s*$/i.test((e.textContent||'').trim()) && e.querySelectorAll('*').length<3);
                    if (b) { (b.closest('[class*=click i]')||b).click(); return true; }
                    return false;
                }""")
                logger.info(f"[{self.agent_id}] CA guest-btn fallback clicked={clicked}")
            await page.wait_for_timeout(1200)
            name = fcn.BotOrchestrator._unique_username.__func__(None) if False else _guest_name()
            self.login_name = name
            self._bw.login_name = name
            try:
                await page.fill("#guest_username", name, timeout=5000)
            except Exception:
                filled = await page.evaluate("""(name) => {
                    const f = document.getElementById('guest_username')
                        || document.querySelector('input[name*=user i],input[name*=nick i],input[id*=user i],input[id*=nick i]')
                        || Array.from(document.querySelectorAll('input')).find(i =>
                             i.offsetParent!==null && /^(text|search|)$/.test(i.type||''));
                    if (!f) return false;
                    f.focus(); f.value = name;
                    f.dispatchEvent(new Event('input',{bubbles:true}));
                    f.dispatchEvent(new Event('change',{bubbles:true}));
                    return true;
                }""", name)
                logger.info(f"[{self.agent_id}] CA name-field fallback used: {filled}")
            # Gender/DOB are selectboxit widgets. The ONLY thing that updates both selectboxit's
            # display AND the native <select> the form submits is jQuery .val().trigger('change')
            # — verified in Chrome (anchor-click and vanilla dispatchEvent do NOT sync selectboxit).
            # Chat Avenue loads jQuery, so this is reliable.
            try:
                gset = await page.evaluate("""() => {
                    if (!window.jQuery) return 'no-jquery';
                    const setSB = (id, m) => { const s=document.getElementById(id); if(!s) return;
                        const o=[...s.options].find(m); if(o) jQuery(s).val(o.value).trigger('change'); };
                    setSB('guest_gender', o=>/female/i.test(o.text));
                    setSB('date_day',   o=>o.text.trim()==='15');
                    setSB('date_month', o=>/^jun/i.test(o.text.trim()));
                    setSB('date_year',  o=>/^(200[0-6])$/.test(o.text.trim()));
                    return (document.getElementById('guest_gender')||{}).value;  // expect '2' = Female
                }""")
                logger.info(f"[{self.agent_id}] CA gender set -> {gset}")
                await page.wait_for_timeout(400)
            except Exception as e:
                logger.warning(f"[{self.agent_id}] CA gender/DOB set issue: {e}")
            # Wait for Cloudflare Turnstile to auto-solve (token populated) before submitting.
            solved = False
            for _ in range(44):                                    # up to ~22s (was 12s)
                tok = await page.evaluate("() => { const e=document.querySelector('input[name=cf-turnstile-response]'); return e ? (e.value||'') : 'none'; }")
                if tok and tok != 'none':
                    solved = True
                    logger.info(f"[{self.agent_id}] CA turnstile solved")
                    break
                await page.wait_for_timeout(500)
            if not solved:
                logger.warning(f"[{self.agent_id}] CA turnstile NOT solved in 22s — submitting anyway")
            try:
                await page.click(".theme_btn.full_button", timeout=8000)
            except Exception:
                await page.evaluate("""() => {
                    const b = document.querySelector('button[type=submit],input[type=submit],.theme_btn,.full_button')
                        || Array.from(document.querySelectorAll('button,a,input')).find(e =>
                             /login|enter|chat|start|join/i.test((e.value||e.textContent||'')));
                    if (b) b.click();
                }""")
                try:
                    await page.keyboard.press("Enter")
                except Exception:
                    pass
            # Wait for the app to render — some sites (sexchat) redirect through a portal first,
            # so poll up to ~12s for a chat/lobby element rather than a single check.
            state = {}
            for _ in range(12):
                await page.wait_for_timeout(1000)
                state = await page.evaluate("""() => ({
                    url: location.href,
                    formGone: !document.querySelector('#guest_username, .intro_guest_btn'),
                    inApp: !!(document.getElementById('content') || document.getElementById('container_rooms') || document.querySelector('.room_element')),
                    capped: /maximum allowed registrations|try again later/i.test((document.body||{}).innerText||''),
                    snip: ((document.body||{}).innerText||'').replace(/\\s+/g,' ').slice(0,160)
                })""")
                if state.get("capped"):
                    self.status = "ip_capped"
                    logger.warning(f"[{self.agent_id}] CA IP CAPPED (max guest registrations — needs a fresh IP)")
                    return False
                if state.get("inApp"):
                    break
            ok = bool(state.get("inApp")) or (bool(state.get("formGone")) and "guest login" not in state.get("snip","").lower())
            self.status = "lobby" if ok else "login_failed"
            if ok:
                logger.info(f"[{self.agent_id}] CA login OK as {name} @ {state.get('url')}")
            else:
                logger.warning(f"[{self.agent_id}] CA login FAILED (turnstile={solved}) @ {state.get('url')} — page: {state.get('snip')}")
            return bool(ok)
        except Exception as e:
            logger.warning(f"[{self.agent_id}] CA login error: {e}")
            return False

    # ── join a room ──────────────────────────────────────────────────────────
    async def join_room(self) -> bool:
        """Open the room list and click a high-traffic guest room. Each agent picks a
        different room by slot to spread out. LIVE-TEST: the room-list control is the
        hamburger; rooms are clickable rows matched by name text."""
        page = self._page
        if not page:
            return False
        try:
            # After guest login Chat Avenue lands on the room-selection page: #container_rooms
            # with `.room_element` rows, each holding a `.room_count`. If we're already inside a
            # room instead, open the menu -> "Room list" to surface the same rows.
            await page.wait_for_timeout(900)
            has_list = await page.evaluate("() => !!document.querySelector('.room_element .room_count')")
            if not has_list:
                await page.evaluate("""() => {
                    const b = document.querySelector('.menu_toggle, [class*=burger i], #menu_button')
                        || document.querySelectorAll('.fa-bars, [class*=bars i]')[0];
                    if (b) (b.closest('[class*=click i]')||b).click();
                }""")
                await page.wait_for_timeout(800)
                await page.evaluate("""() => {
                    const rl = Array.from(document.querySelectorAll('*'))
                        .find(e => /^\\s*Room list\\s*$/i.test(e.textContent||'') && e.querySelectorAll('*').length<4);
                    if (rl) (rl.closest('[class*=click i]')||rl).click();
                }""")
                await page.wait_for_timeout(1400)
            # Pick the busiest GUEST-accessible room and click it. Rooms that say "no guests" /
            # "registered users only" (e.g. Adult Chat - Desktop Version) are skipped — a guest
            # can't post there. slot N takes the Nth-busiest so agents don't all stack one room.
            chosen = await page.evaluate("""(slot) => {
                const rooms = Array.from(document.querySelectorAll('.room_element, .blisting'))
                    .filter(r => r.offsetParent !== null && r.querySelector('.room_count'));
                const data = rooms.map(r => {
                    const count = parseInt((r.querySelector('.room_count').textContent||'').trim()) || 0;
                    const lines = (r.innerText||'').split('\\n').map(s=>s.trim()).filter(Boolean);
                    const name = lines[0] || '?';
                    const noGuests = /no guests|registered users only/i.test(r.textContent||'');
                    return {r, name, count, ok: !noGuests};
                }).filter(x => x.ok).sort((a,b) => b.count - a.count);
                if (!data.length) return null;
                const pick = data[Math.min(slot, data.length-1)];
                pick.r.click();
                return {name: pick.name, count: pick.count,
                        list: data.slice(0,8).map(d => ({n:d.name, c:d.count}))};
            }""", self.slot)
            if chosen:
                logger.info(f"[{self.agent_id}] CA rooms by traffic: {chosen['list']}")
            await page.wait_for_timeout(2500)
            in_room = await page.evaluate("() => !!document.getElementById('content')")
            if in_room:
                self.room = chosen["name"] if chosen else "?"
                self.status = "running"
                logger.info(f"[{self.agent_id}] CA joined room '{self.room}' ({chosen['count'] if chosen else '?'} online)")
            return bool(in_room)
        except Exception as e:
            logger.warning(f"[{self.agent_id}] CA join_room error: {e}")
            return False

    # ── send ───────────────────────────────────────────────────────────────────
    async def send(self, text: str) -> bool:
        """Fill #content and submit. Reuses FCN's validated one-shot fill() approach."""
        page = self._page
        if not page or not text:
            return False
        text = " ".join(text.split())[:300].strip()
        try:
            inp = await page.query_selector("#content")
            if inp is None:
                return False
            self.send_attempts += 1
            await inp.fill(text, timeout=2500)
            # send: click the button, fall back to Enter
            try:
                await page.click("#submit_button", timeout=2000)
            except Exception:
                await page.keyboard.press("Enter")
            await page.wait_for_timeout(500)
            cleared = await page.evaluate("() => { const e=document.getElementById('content'); return e ? !e.value : false; }")
            if cleared:
                self.send_oks += 1
            return bool(cleared)
        except Exception as e:
            logger.warning(f"[{self.agent_id}] CA send error: {e}")
            return False

    # ── broadcast generation (reuses FCN brain) ─────────────────────────────────
    async def _make_broadcast(self) -> str:
        from app.providers import provider_registry
        llm = provider_registry.get_chat_provider()
        if not llm:
            return ""
        bio = self.persona.get("bio", "")
        goals = (self.persona.get("goals", "") or "").strip()   # the "concepts" the operator gives
        handle = (self.persona.get("telegram_handle", "") or "").strip()
        handle_cap = handle.capitalize() if handle else ""
        no_repeat = ""
        if self._recent_msgs:
            no_repeat = ("Do NOT repeat or paraphrase any of these you just sent: "
                         + " | ".join(f'"{m}"' for m in self._recent_msgs[-5:]) + ". ")
        system = (
            f"You are {self.login_name}, a real horny girl in a busy public adult group chat (18+). "
            f"Personality / body: {bio}. "
            + (f"CONCEPTS / dirty examples to riff on (write your OWN every time, NEVER copy these word "
               f"for word): {goals}. " if goals else "")
            + f"Write ONE filthy, explicit broadcast to the WHOLE room — 2 to 3 short dirty sentences, "
            f"UNDER ~230 characters total so the chat never cuts it off. Be detailed and sexual: describe "
            f"what you're doing to yourself right now, your body, what you want, a quick nasty scene. Sound "
            f"like a real horny girl, NOT an ad. Put your handle '{handle_cap}' in the FIRST sentence so it "
            f"can never get cut, and tell them to come find you. Call the platform ONLY 'TG', 'the TG', "
            f"'Tela Grahm', or 'telly' — NEVER write 'telegram'. lowercase, a light typo or two is good, no "
            f"emojis, no quotes, no stage directions, no [tags]. Make every one different and filthy. {no_repeat}"
        )
        resp = await llm.chat(system, "Write the broadcast.")
        if not resp:
            return ""
        # reuse the FCN guard/sanitizer chain
        if handle:
            resp = fcn._scrub_retired_handles(resp, handle)
            resp = fcn._normalize_handle(resp, handle)
        tg = fcn._pick_tg_token(is_dm=False)
        send_text = fcn._obfuscate_handle(resp, handle, tg) if handle else fcn._sanitize_platforms(resp, tg)
        send_text = fcn._force_group_cta(send_text, handle, tg) if handle else send_text
        send_text = fcn._strip_ai_tells(send_text, strip_emoji=True)
        # Backstop: never post past the limit. The handle is in the first sentence, so trimming
        # the tail to a clean boundary keeps it intact rather than cutting mid-word.
        if len(send_text) > BROADCAST_MAX_CHARS:
            cut = send_text[:BROADCAST_MAX_CHARS]
            idx = max(cut.rfind(".."), cut.rfind(". "), cut.rfind("! "))
            if idx < BROADCAST_MAX_CHARS * 0.5:          # no sentence break — fall back to a word
                idx = cut.rfind(" ")
            send_text = (cut[:idx] if idx > 0 else cut).rstrip(" .,!") + ".."
        return send_text

    # ── run loop ─────────────────────────────────────────────────────────────────
    async def run(self):
        """One browser, one tab per Chat Avenue site (same IP is safe — each site sees only one
        guest registration). login/join/send operate on whichever tab _page points at, so we just
        swap _page per tab. Broadcasts round-robin across the joined rooms."""
        if not await self._setup_tabs():
            if not await self._recover():       # nothing came up — try once on a fresh IP
                self.status = "login_failed"; return
        self.status = "running"
        self.room = " + ".join(t["room"] for t in self.tabs)
        logger.info(f"[{self.agent_id}] CA broadcasting every {self.cd_min}-{self.cd_max}s "
                    f"across {len(self.tabs)} room(s): {self.room}")
        i = 0
        fails = 0
        while not self._stop:
            tab = self.tabs[i % len(self.tabs)]; i += 1
            self._bw._page = tab["page"]        # point send() at this tab
            try:
                # Force the right tab to the foreground — remote browsers route input to the
                # active tab, so without this an "Adult Chat" send can land in the Sex Chat tab.
                try:
                    await tab["page"].bring_to_front()
                except Exception:
                    pass
                # Kicked/banned out of the room? Log out, switch IP+UA, re-login (FCN protocol).
                if await self._is_kicked(tab["page"]):
                    logger.warning(f"[{self.agent_id}] CA KICKED from [{tab['room']}] — switching IP")
                    if await self._recover():
                        fails = 0; i = 0
                    await asyncio.sleep(2)
                    continue
                text = await self._make_broadcast()
                if text and await self.send(text):
                    fails = 0
                    self._recent_msgs.append(text)
                    self._recent_msgs = self._recent_msgs[-8:]
                    logger.info(f"[{self.agent_id}] CA BROADCAST [{tab['room']}] @ {tab['page'].url[:42]}: {text}")
                    if self._orchestrator:
                        self._orchestrator.push_feed({
                            "t": time.strftime("%H:%M:%S"),
                            "agent": self.agent_id,
                            "dm": False,
                            "room": tab["room"],
                            "text": text,
                            "platform": "chatavenue",
                        })
                else:
                    fails += 1
                    if fails >= 4:               # persistent send failure ~= kicked/dead room
                        logger.warning(f"[{self.agent_id}] CA {fails} send fails — switching IP")
                        if await self._recover():
                            fails = 0; i = 0
            except Exception as e:
                logger.warning(f"[{self.agent_id}] CA loop error [{tab.get('room')}]: {e}")
            await asyncio.sleep(random.uniform(self.cd_min, self.cd_max))

    async def _setup_tabs(self) -> bool:
        """Open one tab per CA site, log in, and join the busiest guest room on each. login/join
        operate on whichever tab _page points at, so we swap _page per tab. True if any tab is live."""
        self.tabs = []
        page1 = self._bw._page
        if not page1:
            return False
        site_pages = [(page1, CA_SITES[0])]
        for site in CA_SITES[1:]:
            try:
                site_pages.append((await page1.context.new_page(), site))
            except Exception as e:
                logger.warning(f"[{self.agent_id}] CA could not open tab for {site}: {e}")
        for page, site in site_pages:
            self._bw._page = page
            self.site_url = site
            try:
                try:
                    await page.bring_to_front()      # route login/join input to THIS tab
                except Exception:
                    pass
                if not await self.login():
                    logger.warning(f"[{self.agent_id}] CA login failed @ {site}")
                    continue
                # Join can miss in a busy 2-tab browser — retry just the join (no re-login,
                # so no extra guest registration / IP hit).
                joined = False
                for jtry in range(3):
                    try:
                        await page.bring_to_front()
                    except Exception:
                        pass
                    if await self.join_room():
                        joined = True
                        break
                    logger.info(f"[{self.agent_id}] CA join retry {jtry+1} @ {site}")
                    await asyncio.sleep(2)
                if joined:
                    self.tabs.append({"page": page, "site": site, "room": self.room})
                    logger.info(f"[{self.agent_id}] CA tab live: {site} -> '{self.room}'")
                else:
                    logger.warning(f"[{self.agent_id}] CA tab setup failed @ {site} (join)")
            except Exception as e:
                logger.warning(f"[{self.agent_id}] CA tab error @ {site}: {e}")
        return bool(self.tabs)

    async def _is_kicked(self, page) -> bool:
        """True if a kick/ban notice is on this tab."""
        try:
            return await page.evaluate("""() => {
                const t = (document.body ? document.body.innerText : '').toLowerCase();
                return /you (have been|were|are) (kicked|banned|removed)|been kicked|temporarily banned|you are banned|kicked from|you got kicked/.test(t);
            }""")
        except Exception:
            return False

    async def _recover(self) -> bool:
        """FCN-style recovery: drop the session, rotate to a fresh Decodo IP (a fresh provision
        also rotates the User-Agent + clears cookies), then re-login/re-join both sites."""
        if not self._orchestrator:
            return False
        self.status = "recovering"
        logger.warning(f"[{self.agent_id}] CA recovering — fresh IP + UA + clean cookies")
        try:
            await self._bw.disconnect_cdp()
        except Exception:
            pass
        self._proxy_idx = (self._proxy_idx + 13) % 50         # fresh, distinct US Decodo IP
        proxy = fcn.DECODA_PROXIES[self._proxy_idx]
        try:
            if not await self._orchestrator._provision_and_connect(self._bw, custom_proxy=proxy,
                                                                   platform="chatavenue"):
                logger.warning(f"[{self.agent_id}] CA recover: provision failed")
                return False
        except Exception as e:
            logger.warning(f"[{self.agent_id}] CA recover: provision error: {e}")
            return False
        ok = await self._setup_tabs()
        if ok:
            self.status = "running"
            self.room = " + ".join(t["room"] for t in self.tabs)
            logger.info(f"[{self.agent_id}] CA recovered on decodo:{proxy['port']} -> {self.room}")
        return ok

    async def stop(self):
        self._stop = True


_FEMALE = ["Mia", "Sofia", "Luna", "Lola", "Bella", "Nina", "Maya", "Jade", "Lexi", "Stella"]


def _guest_name() -> str:
    return f"{random.choice(_FEMALE)}{random.randint(10, 9999)}"
