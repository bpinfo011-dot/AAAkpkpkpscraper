#!/usr/bin/env python3
"""
KupujemProdajem Lead Scraper  v2
=================================
Finds sellers with 50+ positive reviews AND 20+ active ads
across ALL categories on kupujemprodajem.com.

URL Strategy (v2 — tested against live site):
  Listing:  /pretraga?categoryId={catId}&page={page}
  Profile:  /u/svi-oglasi/{userId}/1  (dummy slug, KP follows redirect)

Phases:
  0  DISCOVER  Homepage → 88 category IDs → per-category first page → groups
  1  SCAN      Paginate every group listing → collect userId appearances
  2  PROFILES  Visit profile pages for users with ≥ MIN_APPEARANCES
  3  DONE      Write leads.csv
"""

import asyncio, csv, json, logging, os, random, re, sys, time

# ── Config ─────────────────────────────────────────────────────
BASE       = "https://www.kupujemprodajem.com"
CKPT_FILE  = "checkpoint.json"
OUT_FILE   = "leads.csv"

MAX_SECS   = 5 * 3600 + 20 * 60   # 5 h 20 min per GH Actions job
SAVE_EVERY = 60
DELAY      = (0.7, 1.6)           # random range in seconds
RETRIES    = 3
TIMEOUT    = 30_000               # ms

MIN_APPEAR = 3                    # phase-1 threshold to check profile
MIN_REV    = 50                   # final filter
MIN_ADS    = 20                   # final filter
# Groups with more than this many pages get scanned; smaller ones too.
# All pages are visited for comprehensiveness.

NEXT_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("kp")

# ── Helpers ────────────────────────────────────────────────────
def kp_int(v):
    if isinstance(v, int): return v
    if not v: return 0
    return int(str(v).replace(".", "").replace(",", ""))

def slugify(t):
    t = t.translate(str.maketrans("šđčćžŠĐČĆŽ", "sdcczsdccz")).lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", t)).strip("-")

# ── State ──────────────────────────────────────────────────────
def blank():
    return dict(version=2, phase="discover", categories={}, scan_queue=[],
                user_data={}, profile_slugs={},
                profile_queue=[], profile_done=[],
                leads=[], stats=dict(pages=0, ads=0, profiles=0))

def load():
    if os.path.exists(CKPT_FILE):
        s = json.load(open(CKPT_FILE, encoding="utf-8"))
        # Reset if old version or done-with-zero-leads (failed v1 run)
        if s.get("version") != 2 or (s["phase"] == "done" and not s["leads"]):
            log.info("Resetting stale/v1 checkpoint → fresh start")
            return blank()
        log.info("Checkpoint: phase=%s pages=%d leads=%d",
                 s["phase"], s["stats"]["pages"], len(s["leads"]))
        s.setdefault("profile_slugs", {})
        return s
    return blank()

def save(s):
    tmp = CKPT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)
    os.replace(tmp, CKPT_FILE)

# ── Browser ────────────────────────────────────────────────────
async def init_browser(pw):
    br = await pw.chromium.launch(headless=True)
    ctx = await br.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 Chrome/125.0 Safari/537.36",
    )
    # block images, fonts, trackers
    await ctx.route(re.compile(
        r"\.(png|jpe?g|gif|svg|webp|woff2?|ttf|eot|ico)(\?|$)", re.I),
        lambda r: r.abort())
    await ctx.route(re.compile(
        r"(google-analytics|googletagmanager|facebook|doubleclick)", re.I),
        lambda r: r.abort())
    return br, await ctx.new_page()

async def get_redux(page, url, retries=RETRIES):
    """Navigate and extract initialReduxState, or None."""
    for att in range(1, retries + 1):
        try:
            r = await page.goto(url, wait_until="domcontentloaded",
                                timeout=TIMEOUT)
            if r and r.status == 404:
                return None
            html = await page.content()
            m = NEXT_RE.search(html)
            if not m:
                await asyncio.sleep(2)
                html = await page.content()
                m = NEXT_RE.search(html)
            if m:
                d = json.loads(m.group(1))
                rx = d.get("props", {}).get("initialReduxState")
                if rx:
                    return rx, d, html
            log.warning("  no __NEXT_DATA__ %s (att %d)", url, att)
        except Exception as e:
            log.warning("  error %s (att %d): %s", url, att, e)
        await asyncio.sleep(2 * att + random.random())
    return None

async def dismiss_cookies(page):
    for sel in ['button:has-text("Prihvatam")', 'button:has-text("Prihvati")',
                'button:has-text("Accept")', ".cookie-consent button"]:
        try:
            b = page.locator(sel).first
            if await b.is_visible(timeout=1500):
                await b.click(); await asyncio.sleep(0.3); return
        except: pass

# ── Phase 0: DISCOVER ─────────────────────────────────────────
async def phase_discover(page, S, ok):
    log.info("═" * 50)
    log.info("PHASE 0 — DISCOVER")
    log.info("═" * 50)

    # Step 1: homepage → category IDs
    if not S["categories"]:
        log.info("Loading homepage …")
        res = await get_redux(page, BASE + "/")
        if not res:
            log.error("Homepage failed"); return False
        await dismiss_cookies(page)
        rx, _, _ = res
        cats = rx.get("category", {}).get("categories", {})
        log.info("Found %d categories", len(cats))
        for cid, info in cats.items():
            S["categories"][str(cid)] = {
                "name": info["name"], "groups": {}, "total_pages": None}
        save(S)

    # Step 2: visit first page of each category → discover groups + page count
    todo = [cid for cid, ci in S["categories"].items()
            if ci["total_pages"] is None]
    log.info("Categories to discover: %d", len(todo))

    for cid in todo:
        if not ok(): break
        ci = S["categories"][cid]
        url = f"{BASE}/pretraga?categoryId={cid}"
        res = await get_redux(page, url)
        if res:
            rx, _, html = res
            S["stats"]["pages"] += 1
            sr = rx.get("search", {})
            ci["total_pages"] = sr.get("pages", 0)
            total = sr.get("total", 0)

            # harvest groups
            gall = rx.get("group", {}).get("groups", {})
            gids_map = rx.get("group", {}).get("groupsIds", {})
            cat_g = gall.get(cid, gall.get(str(cid), {}))
            for gid, g in cat_g.items():
                if g.get("active") in ("yes", True):
                    ci["groups"][str(gid)] = {
                        "name": g["name"], "slug": slugify(g["name"])}
            # fallback: groupsIds
            if not ci["groups"]:
                for gid in gids_map.get(cid, gids_map.get(str(cid), [])):
                    ci["groups"][str(gid)] = {"name": str(gid), "slug": str(gid)}

            # process first page ads already
            _process_ads(sr, S)

            # harvest profile slugs
            for slug, uid in re.findall(
                    r'href="/([^/]+)/svi-oglasi/(\d+)/\d+"', html):
                if slug != "u": S["profile_slugs"][uid] = slug

            log.info("  %s: %d groups, %d pages (%d ads)",
                     ci["name"], len(ci["groups"]), ci["total_pages"], total)
        else:
            ci["total_pages"] = 0
            log.warning("  ✗ %s (id=%s)", ci["name"], cid)

        save(S)
        await asyncio.sleep(random.uniform(*DELAY))

    # Step 3: build scan queue
    if all(ci["total_pages"] is not None for ci in S["categories"].values()):
        _build_scan_queue(S)
        S["phase"] = "scan"
        save(S)
        return True
    save(S)
    return False

def _build_scan_queue(S):
    q = []
    for cid, ci in S["categories"].items():
        tp = ci.get("total_pages", 0)
        if tp <= 0: continue

        if len(ci["groups"]) > 0 and tp > 200:
            # large category → scan per group
            for gid, gi in ci["groups"].items():
                q.append({"catId": cid, "groupId": gid,
                          "page": 1, "maxPage": None})
        else:
            # small / medium category → scan at category level (page 2+)
            # page 1 already processed in discover
            q.append({"catId": cid, "groupId": None,
                      "page": 2, "maxPage": tp})

    random.shuffle(q)
    S["scan_queue"] = q
    log.info("Scan queue: %d items", len(q))

# ── Phase 1: SCAN ─────────────────────────────────────────────
def _process_ads(search, S):
    n = 0
    for ad in search.get("byId", {}).values():
        uid = ad.get("userId")
        if not uid: continue
        uid = str(uid)
        cat = ad.get("categoryName", "")
        grp = ad.get("groupName", "")
        if uid not in S["user_data"]:
            S["user_data"][uid] = {"count": 0, "category": cat, "group": grp}
        S["user_data"][uid]["count"] += 1
        n += 1
    S["stats"]["ads"] += n
    return n

async def phase_scan(page, S, ok):
    log.info("═" * 50)
    log.info("PHASE 1 — SCAN LISTINGS")
    log.info("═" * 50)

    q = S["scan_queue"]
    while q and ok():
        it = q[0]
        cid, gid = it["catId"], it["groupId"]
        pg, mx = it["page"], it.get("maxPage")

        if mx is not None and pg > mx:
            q.pop(0); continue

        # Build URL
        url = f"{BASE}/pretraga?categoryId={cid}"
        if gid: url += f"&groupId={gid}"
        url += f"&page={pg}"

        res = await get_redux(page, url)
        if res:
            rx, _, html = res
            sr = rx.get("search", {})
            tp = sr.get("pages", 0)
            it["maxPage"] = tp
            S["stats"]["pages"] += 1
            _process_ads(sr, S)

            # harvest profile slugs
            for slug, uid in re.findall(
                    r'href="/([^/]+)/svi-oglasi/(\d+)/\d+"', html):
                if slug != "u": S["profile_slugs"][uid] = slug

            if pg == 1:
                name = S["categories"].get(cid, {}).get("name", cid)
                g_label = f" > group {gid}" if gid else ""
                log.info("  [%d q] %s%s: %d pages (pg %d)",
                         len(q), name, g_label, tp, pg)
            elif pg % 50 == 0:
                log.info("    … page %d/%d", pg, tp)

            if pg < tp:
                it["page"] = pg + 1
            else:
                q.pop(0)
        else:
            name = S["categories"].get(cid, {}).get("name", cid)
            log.warning("  ✗ skip %s gid=%s pg=%d", name, gid, pg)
            q.pop(0)

        if S["stats"]["pages"] % SAVE_EVERY == 0:
            save(S)
            log.info("  ── ckpt pages=%d ads=%d users=%d",
                     S["stats"]["pages"], S["stats"]["ads"],
                     len(S["user_data"]))
        await asyncio.sleep(random.uniform(*DELAY))

    if not q:
        _build_profile_queue(S)
        S["phase"] = "profiles"; save(S)
        log.info("Scan done · %d pages · %d ads · %d users",
                 S["stats"]["pages"], S["stats"]["ads"],
                 len(S["user_data"]))
        return True
    save(S); return False

def _build_profile_queue(S):
    done = set(S.get("profile_done", []))
    q = [u for u, d in S["user_data"].items()
         if d["count"] >= MIN_APPEAR and u not in done]
    q.sort(key=lambda u: -S["user_data"][u]["count"])
    S["profile_queue"] = q
    log.info("Profile queue: %d (≥%d appearances out of %d)",
             len(q), MIN_APPEAR, len(S["user_data"]))

# ── Phase 2: PROFILES ─────────────────────────────────────────
async def phase_profiles(page, S, ok):
    log.info("═" * 50)
    log.info("PHASE 2 — CHECK PROFILES")
    log.info("═" * 50)

    q, leads, done = S["profile_queue"], S["leads"], S["profile_done"]
    while q and ok():
        uid = q.pop(0)
        slug = S.get("profile_slugs", {}).get(uid, "u")
        url = f"{BASE}/{slug}/svi-oglasi/{uid}/1"
        res = await get_redux(page, url)

        # fallback if slug failed
        if not res and slug != "u":
            res = await get_redux(page, f"{BASE}/u/svi-oglasi/{uid}/1")

        if res:
            rx, _, _ = res
            sm = rx.get("user", {}).get("summary", {})
            meta = rx.get("meta", {})
            uname = sm.get("username", "")
            pos = kp_int(sm.get("reviewsPositive", "0"))
            act = kp_int(sm.get("userActiveAdCount", 0))
            purl = meta.get("pageUrl", f"/{slug}/svi-oglasi/{uid}/1")
            S["stats"]["profiles"] += 1

            if pos >= MIN_REV and act >= MIN_ADS:
                cat = S["user_data"].get(uid, {}).get("category", "")
                leads.append({"ime": uname,
                              "link_profila": BASE + purl,
                              "broj_ocena": pos,
                              "broj_aktivnih_oglasa": act,
                              "kategorija": cat})
                log.info("  ✓ #%d %s (%d rev, %d ads)",
                         len(leads), uname, pos, act)
        else:
            log.warning("  ✗ profile uid=%s", uid)

        done.append(uid)
        if len(done) % 30 == 0:
            save(S)
            log.info("  ── profiles: %d checked, %d leads",
                     S["stats"]["profiles"], len(leads))
        await asyncio.sleep(random.uniform(*DELAY))

    if not q:
        S["phase"] = "done"; save(S); return True
    save(S); return False

# ── CSV output ─────────────────────────────────────────────────
FIELDS = ["ime","link_profila","broj_ocena","broj_aktivnih_oglasa","kategorija"]

def write_csv(S):
    rows = sorted(S["leads"], key=lambda x: -x["broj_ocena"])
    with open(OUT_FILE, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader()
        w.writerows(rows)
    log.info("Wrote %d leads to %s", len(rows), OUT_FILE)

# ── Main ───────────────────────────────────────────────────────
async def main():
    from playwright.async_api import async_playwright

    S = load()
    if S["phase"] == "done":
        log.info("Already done — %d leads", len(S["leads"]))
        write_csv(S); sys.exit(0)

    t0 = time.time()
    ok = lambda: (time.time() - t0) < MAX_SECS

    async with async_playwright() as pw:
        br, pg = await init_browser(pw)
        try:
            if S["phase"] == "discover":
                await phase_discover(pg, S, ok)
            if S["phase"] == "scan" and ok():
                await phase_scan(pg, S, ok)
            if S["phase"] == "profiles" and ok():
                await phase_profiles(pg, S, ok)
        finally:
            await br.close()

    write_csv(S)
    elapsed = (time.time() - t0) / 60
    log.info("%.0f min · %d pages · %d leads", elapsed,
             S["stats"]["pages"], len(S["leads"]))

    if S["phase"] == "done":
        log.info("🎉 DONE"); sys.exit(0)
    else:
        log.info("⏸ Paused — will continue next run"); sys.exit(2)

if __name__ == "__main__":
    asyncio.run(main())
