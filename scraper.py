#!/usr/bin/env python3
"""
KupujemProdajem Lead Scraper
=============================
Finds high-volume sellers (50+ positive reviews, 20+ active ads)
across ALL categories and subcategories on kupujemprodajem.com.

Designed to run on GitHub Actions with automatic checkpoint/resume.
Each run processes up to ~5 h 20 min, then saves state and re-triggers.

Phases
------
0  DISCOVER   Visit homepage + each category page → build list of all groups
1  SCAN       Paginate every group listing → collect userId appearances
2  PROFILES   Visit profile page for users with ≥ MIN_APPEARANCES
3  DONE       Write leads.csv

Data extraction
---------------
All data comes from the __NEXT_DATA__ JSON blob embedded by Next.js.
No DOM interaction required beyond navigation + cookie consent.
"""

import asyncio
import csv
import json
import logging
import os
import random
import re
import sys
import time
import traceback
from pathlib import Path

# ── Configuration ──────────────────────────────────────────────
BASE_URL = "https://www.kupujemprodajem.com"
CHECKPOINT_FILE = "checkpoint.json"
OUTPUT_FILE = "leads.csv"

MAX_RUN_SECONDS = 5 * 3600 + 20 * 60        # 5 h 20 min per GH Actions run
SAVE_EVERY = 80                               # save checkpoint every N pages
DELAY_MIN, DELAY_MAX = 0.7, 1.6              # seconds between requests
RETRY_ATTEMPTS = 3
PAGE_TIMEOUT_MS = 30_000

MIN_APPEARANCES = 3          # Phase-1 pre-filter (listing appearances)
MIN_REVIEWS = 50             # Phase-2 filter
MIN_ACTIVE_ADS = 20          # Phase-2 filter

NEXT_DATA_RE = re.compile(
    r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL
)

# ── Logging ────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("kp")

# ── Helpers ────────────────────────────────────────────────────

def slugify(text: str) -> str:
    """Convert Serbian text to a URL-safe slug."""
    table = str.maketrans(
        "šđčćžŠĐČĆŽ",
        "sdcczsdccz",       # đ→d (KP uses single-char, not 'dj')
    )
    s = text.translate(table).lower()
    s = re.sub(r"[|.&/\\]", " ", s)
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-")


def parse_kp_int(value) -> int:
    """Parse KP's dot-as-thousand-sep numbers: '2.394' → 2394, 0 → 0."""
    if isinstance(value, int):
        return value
    if not value:
        return 0
    return int(str(value).replace(".", "").replace(",", ""))


# ── State management ──────────────────────────────────────────

def blank_state() -> dict:
    return {
        "phase": "discover",
        "categories": {},       # catId → {name, slug, groups: {gId: {name, slug, pages}}}
        "cat_queue": [],        # catIds left to discover groups for
        "scan_queue": [],       # [{catId, groupId, catSlug, groupSlug, page, maxPage}]
        "user_data": {},        # str(userId) → {count, category, group}
        "profile_queue": [],    # [userId, ...]
        "profile_done": [],     # [userId, ...]
        "leads": [],
        "stats": {"pages": 0, "ads_seen": 0, "profiles_checked": 0},
    }


def load_state() -> dict:
    if os.path.exists(CHECKPOINT_FILE):
        with open(CHECKPOINT_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
            log.info(
                "Loaded checkpoint  phase=%s  pages=%d  leads=%d",
                state["phase"],
                state["stats"]["pages"],
                len(state["leads"]),
            )
            return state
    return blank_state()


def save_state(state: dict):
    tmp = CHECKPOINT_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, CHECKPOINT_FILE)


# ── Browser helpers ────────────────────────────────────────────

async def make_browser(pw):
    browser = await pw.chromium.launch(headless=True)
    ctx = await browser.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0.0.0 Safari/537.36"
        ),
    )
    # Block heavy resources to speed up navigation
    await ctx.route(
        re.compile(r"\.(png|jpg|jpeg|gif|svg|webp|woff2?|ttf|eot|ico)(\?|$)", re.I),
        lambda route: route.abort(),
    )
    await ctx.route(
        re.compile(r"(google-analytics|googletagmanager|facebook|doubleclick)", re.I),
        lambda route: route.abort(),
    )
    page = await ctx.new_page()
    return browser, ctx, page


async def dismiss_cookie_banner(page):
    """Try to click the cookie-consent accept button."""
    for selector in [
        'button:has-text("Prihvatam")',
        'button:has-text("Prihvati")',
        'button:has-text("Accept")',
        'button:has-text("Slažem se")',
        '[data-testid="cookie-accept"]',
        ".cookie-consent button",
    ]:
        try:
            btn = page.locator(selector).first
            if await btn.is_visible(timeout=1500):
                await btn.click()
                await asyncio.sleep(0.3)
                return
        except Exception:
            pass


async def fetch_next_data(page, url: str, retries: int = RETRY_ATTEMPTS):
    """Navigate to *url*, extract and return (redux_state, full_data) or None."""
    for attempt in range(1, retries + 1):
        try:
            resp = await page.goto(url, wait_until="domcontentloaded", timeout=PAGE_TIMEOUT_MS)
            status = resp.status if resp else 0

            if status == 404:
                return None

            # Some pages do a client-side redirect; give it a moment
            if status in (301, 302, 303, 307, 308):
                await page.wait_for_load_state("domcontentloaded", timeout=10_000)

            html = await page.content()
            m = NEXT_DATA_RE.search(html)
            if not m:
                # Page might not be fully rendered yet
                await asyncio.sleep(2)
                html = await page.content()
                m = NEXT_DATA_RE.search(html)

            if m:
                data = json.loads(m.group(1))
                redux = data.get("props", {}).get("initialReduxState")
                if redux:
                    return redux, data

            log.warning("  No __NEXT_DATA__ at %s (attempt %d)", url, attempt)

        except Exception as exc:
            log.warning("  Error %s (attempt %d): %s", url, attempt, exc)

        await asyncio.sleep(2 * attempt + random.random())

    return None


# ── Phase 0: DISCOVER ─────────────────────────────────────────

async def discover_categories_from_homepage(page, state: dict):
    """Visit the homepage and catalog all 88 categories."""
    log.info("Phase 0 · loading homepage")
    result = await fetch_next_data(page, BASE_URL + "/")
    if not result:
        log.error("Cannot load homepage — aborting")
        return False

    await dismiss_cookie_banner(page)
    redux, _ = result

    cats = redux.get("category", {}).get("categories", {})
    if not cats:
        log.error("No categories in homepage __NEXT_DATA__")
        return False

    log.info("Phase 0 · found %d categories", len(cats))

    # Also harvest any group-listing links already on the page
    # (the mega-menu usually has links of the form /slug/slug/grupa/catId/groupId/1)
    html = await page.content()
    link_re = re.compile(r'href="(/[^"]+/grupa/(\d+)/(\d+)/\d+)"')
    nav_groups: dict[str, dict[str, str]] = {}  # catId → {groupId → slug_pair}
    for href, cid, gid in link_re.findall(html):
        nav_groups.setdefault(cid, {})[gid] = href

    queue = []
    for cid_str, info in cats.items():
        if cid_str in state["categories"]:
            continue  # already discovered
        name = info["name"]
        state["categories"][cid_str] = {
            "name": name,
            "slug": slugify(name),
            "groups": {},
        }
        queue.append(cid_str)

        # Pre-fill groups from homepage links
        if cid_str in nav_groups:
            for gid, href in nav_groups[cid_str].items():
                parts = href.strip("/").split("/")
                if len(parts) >= 5:
                    state["categories"][cid_str]["groups"][gid] = {
                        "name": gid,
                        "slug": parts[1],
                    }

    state["cat_queue"] = queue
    save_state(state)
    return True


async def discover_groups_for_category(page, state: dict, cat_id: str):
    """Visit one page of a category to discover all its groups."""
    cat_info = state["categories"].get(cat_id, {})
    cat_slug = cat_info.get("slug", "c")
    cat_name = cat_info.get("name", cat_id)

    # Strategy: visit the category's own listing.
    # KP URL: /{catSlug}/{page}  — shows ALL ads in the category.
    url = f"{BASE_URL}/{cat_slug}/1"
    result = await fetch_next_data(page, url)

    if not result:
        # Fallback: maybe the slug is wrong.  Try slugify variants.
        for variant_slug in _slug_variants(cat_name):
            if variant_slug == cat_slug:
                continue
            url = f"{BASE_URL}/{variant_slug}/1"
            result = await fetch_next_data(page, url)
            if result:
                cat_info["slug"] = variant_slug
                cat_slug = variant_slug
                break

    if not result:
        log.warning("  ✗ Could not load category page for %s (id=%s)", cat_name, cat_id)
        return

    redux, _ = result
    state["stats"]["pages"] += 1

    # Extract groups
    groups_all = redux.get("group", {}).get("groups", {})
    # groups_all might be keyed by catId (str) or nested differently
    cat_groups = groups_all.get(cat_id, groups_all.get(int(cat_id), {}))

    if not cat_groups:
        # Try groupsIds to discover the groupIds at least
        gids = redux.get("group", {}).get("groupsIds", {})
        cat_gids = gids.get(cat_id, gids.get(str(cat_id), []))
        for gid in cat_gids:
            gid_s = str(gid)
            if gid_s not in cat_info["groups"]:
                cat_info["groups"][gid_s] = {"name": gid_s, "slug": gid_s}
        if cat_gids:
            log.info("  %s: %d groups (from groupsIds)", cat_name, len(cat_gids))
        else:
            # This category might have NO groups (flat listing).
            # In that case, the search results are at the category level.
            # We'll treat the category itself as a single "group" and scan
            # via the category URL instead.
            search = redux.get("search", {})
            total_pages = search.get("pages", 0)
            if total_pages > 0:
                cat_info["groups"]["0"] = {
                    "name": cat_name,
                    "slug": "_flat_",
                    "pages": total_pages,
                }
                log.info("  %s: flat category, %d pages", cat_name, total_pages)
                # Process the first page ads now
                _process_listing_ads(search, state, cat_name)
            else:
                log.warning("  %s: no groups and no ads", cat_name)
        return

    for gid_str, grp in cat_groups.items():
        if grp.get("active") not in ("yes", True):
            continue
        gid_s = str(gid_str)
        if gid_s not in cat_info["groups"]:
            cat_info["groups"][gid_s] = {
                "name": grp["name"],
                "slug": slugify(grp["name"]),
            }

    log.info("  %s: %d groups discovered", cat_name, len(cat_info["groups"]))


def _slug_variants(name: str) -> list[str]:
    """Generate plausible slug variants for a Serbian category name."""
    base = slugify(name)
    variants = [base]

    # đ can be transliterated as 'd' or 'dj'
    if "đ" in name.lower() or "Đ" in name:
        alt = name
        for old, new in [("đ", "dj"), ("Đ", "Dj")]:
            alt = alt.replace(old, new)
        variants.append(slugify(alt))

    # Pipe variants: "A | B" might be "a-b" or "a-oprema-i-delovi"
    if "|" in name:
        parts = [p.strip() for p in name.split("|")]
        variants.append(slugify(parts[0]))
        variants.append(slugify("-".join(parts)))

    return list(dict.fromkeys(variants))  # dedupe keeping order


async def phase_discover(page, state: dict, budget_ok):
    """Run discovery phase: categories → groups."""
    # Step 1: get categories from homepage (if not done yet)
    if not state["categories"]:
        ok = await discover_categories_from_homepage(page, state)
        if not ok:
            return False

    # Step 2: discover groups for each category
    queue = state["cat_queue"]
    while queue and budget_ok():
        cat_id = queue[0]
        await discover_groups_for_category(page, state, cat_id)
        queue.pop(0)
        save_state(state)
        await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    if not queue:
        # Build scan queue
        build_scan_queue(state)
        state["phase"] = "scan"
        save_state(state)
        return True

    save_state(state)
    return False  # ran out of time


def build_scan_queue(state: dict):
    """Create the ordered list of (category, group, page) items to scan."""
    q = []
    for cat_id, cat_info in state["categories"].items():
        for grp_id, grp_info in cat_info.get("groups", {}).items():
            q.append({
                "catId": cat_id,
                "groupId": grp_id,
                "catSlug": cat_info["slug"],
                "groupSlug": grp_info["slug"],
                "page": 1,
                "maxPage": grp_info.get("pages"),   # may be None
            })

    # Shuffle to spread load across categories (avoids hammering one section)
    random.shuffle(q)
    state["scan_queue"] = q
    log.info("Built scan queue: %d group(s) to scan", len(q))


# ── Phase 1: SCAN LISTINGS ────────────────────────────────────

def _process_listing_ads(search: dict, state: dict, fallback_category: str = ""):
    """Extract seller info from a search result page."""
    by_id = search.get("byId", {})
    ud = state["user_data"]
    count = 0

    for ad in by_id.values():
        uid = ad.get("userId")
        if not uid:
            continue
        uid_s = str(uid)
        cat_name = ad.get("categoryName", fallback_category)
        grp_name = ad.get("groupName", "")

        if uid_s not in ud:
            ud[uid_s] = {"count": 0, "category": cat_name, "group": grp_name}

        ud[uid_s]["count"] += 1
        count += 1

    state["stats"]["ads_seen"] += count
    return count


def _harvest_profile_slugs(page_html: str, state: dict):
    """Extract userId → profile-slug from links like /slug/svi-oglasi/uid/1."""
    slugs = state.setdefault("profile_slugs", {})
    for slug, uid in re.findall(r'href="/([^/]+)/svi-oglasi/(\d+)/\d+"', page_html):
        if slug != "u":
            slugs[uid] = slug


async def phase_scan(page, state: dict, budget_ok):
    """Paginate through every group listing and collect seller appearances."""
    queue = state["scan_queue"]
    total_groups = len(queue)

    while queue and budget_ok():
        item = queue[0]
        cat_id = item["catId"]
        grp_id = item["groupId"]
        cat_slug = item["catSlug"]
        grp_slug = item["groupSlug"]
        pg = item["page"]
        max_pg = item.get("maxPage")

        # Skip if already past last page
        if max_pg is not None and pg > max_pg:
            queue.pop(0)
            continue

        # Build URL
        if grp_slug == "_flat_":
            url = f"{BASE_URL}/{cat_slug}/{pg}"
        else:
            url = f"{BASE_URL}/{cat_slug}/{grp_slug}/grupa/{cat_id}/{grp_id}/{pg}"

        result = await fetch_next_data(page, url)

        if result:
            redux, _ = result
            search = redux.get("search", {})
            total_pages = search.get("pages", 0)

            # Also harvest profile slugs from the rendered HTML
            try:
                html = await page.content()
                _harvest_profile_slugs(html, state)
            except Exception:
                pass
            item["maxPage"] = total_pages
            state["stats"]["pages"] += 1

            ads_found = _process_listing_ads(search, state)

            # Log progress
            if pg == 1:
                cat_name = state["categories"].get(cat_id, {}).get("name", cat_id)
                grp_name = grp_slug if grp_slug != "_flat_" else "(flat)"
                log.info(
                    "  [%d queued] %s > %s : %d pages, %d ads (pg %d)",
                    len(queue), cat_name, grp_name, total_pages, ads_found, pg,
                )
            elif pg % 30 == 0:
                log.info("    ... page %d/%d", pg, total_pages)

            # Advance to next page or next group
            if pg < total_pages:
                item["page"] = pg + 1
            else:
                queue.pop(0)
        else:
            # Failed after retries — skip
            cat_name = state["categories"].get(cat_id, {}).get("name", cat_id)
            log.warning("  ✗ Skipping failed: %s > %s (page %d)", cat_name, grp_slug, pg)
            queue.pop(0)

        # Periodic save
        if state["stats"]["pages"] % SAVE_EVERY == 0:
            save_state(state)
            log.info(
                "  ── checkpoint  pages=%d  ads=%d  unique_users=%d",
                state["stats"]["pages"],
                state["stats"]["ads_seen"],
                len(state["user_data"]),
            )

        await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    if not queue:
        # Build profile queue
        build_profile_queue(state)
        state["phase"] = "profiles"
        save_state(state)
        log.info(
            "Phase 1 complete · %d pages · %d ads · %d unique sellers",
            state["stats"]["pages"],
            state["stats"]["ads_seen"],
            len(state["user_data"]),
        )
        return True

    save_state(state)
    return False


def build_profile_queue(state: dict):
    """Select users with enough listing appearances to warrant a profile visit."""
    already_done = set(state.get("profile_done", []))
    queue = [
        uid
        for uid, d in state["user_data"].items()
        if d["count"] >= MIN_APPEARANCES and uid not in already_done
    ]
    # Sort by appearance count descending (check biggest sellers first)
    queue.sort(key=lambda uid: -state["user_data"][uid]["count"])
    state["profile_queue"] = queue
    log.info(
        "Profile queue: %d users with ≥%d appearances (out of %d total)",
        len(queue), MIN_APPEARANCES, len(state["user_data"]),
    )


# ── Phase 2: VISIT PROFILES ───────────────────────────────────

async def phase_profiles(page, state: dict, budget_ok):
    """Visit profile pages and apply the final filter."""
    queue = state["profile_queue"]
    leads = state["leads"]
    done = state["profile_done"]

    while queue and budget_ok():
        uid = queue.pop(0)

        # Use known slug if we harvested it during scan, else dummy slug
        known_slugs = state.get("profile_slugs", {})
        slug = known_slugs.get(uid, "u")
        url = f"{BASE_URL}/{slug}/svi-oglasi/{uid}/1"
        result = await fetch_next_data(page, url)

        # Fallback: if known slug failed, try the dummy one (or vice versa)
        if not result and slug != "u":
            result = await fetch_next_data(page, f"{BASE_URL}/u/svi-oglasi/{uid}/1")
        elif not result and slug == "u":
            # Try using userId itself as slug
            result = await fetch_next_data(page, f"{BASE_URL}/{uid}/svi-oglasi/{uid}/1")

        if result:
            redux, _ = result
            summary = redux.get("user", {}).get("summary", {})
            meta = redux.get("meta", {})

            username = summary.get("username", "")
            positive = parse_kp_int(summary.get("reviewsPositive", "0"))
            active_ads = parse_kp_int(summary.get("userActiveAdCount", 0))
            profile_path = meta.get("pageUrl", f"/u/svi-oglasi/{uid}/1")

            state["stats"]["profiles_checked"] += 1

            if positive >= MIN_REVIEWS and active_ads >= MIN_ACTIVE_ADS:
                category = state["user_data"].get(uid, {}).get("category", "")
                lead = {
                    "ime": username,
                    "link_profila": BASE_URL + profile_path,
                    "broj_ocena": positive,
                    "broj_aktivnih_oglasa": active_ads,
                    "kategorija": category,
                }
                leads.append(lead)
                log.info(
                    "  ✓ LEAD #%d: %s  (%d reviews, %d ads, %s)",
                    len(leads), username, positive, active_ads, category,
                )
        else:
            log.warning("  ✗ Could not load profile for userId=%s", uid)

        done.append(uid)

        if len(done) % 30 == 0:
            save_state(state)
            log.info(
                "  ── profiles checked: %d / leads: %d",
                state["stats"]["profiles_checked"],
                len(leads),
            )

        await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    if not queue:
        state["phase"] = "done"
        save_state(state)
        return True

    save_state(state)
    return False


# ── CSV output ─────────────────────────────────────────────────

def write_csv(state: dict):
    leads = state["leads"]
    if not leads:
        log.info("No leads to write.")
        # Write empty CSV with headers so the artifact is valid
        with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["ime", "link_profila", "broj_ocena", "broj_aktivnih_oglasa", "kategorija"])
        return

    # Sort by review count descending
    leads_sorted = sorted(leads, key=lambda x: -x["broj_ocena"])

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["ime", "link_profila", "broj_ocena", "broj_aktivnih_oglasa", "kategorija"],
        )
        writer.writeheader()
        writer.writerows(leads_sorted)

    log.info("Wrote %d leads to %s", len(leads_sorted), OUTPUT_FILE)


# ── Main entry point ──────────────────────────────────────────

async def main():
    from playwright.async_api import async_playwright

    state = load_state()

    if state["phase"] == "done":
        log.info("Scraper already finished. %d leads in %s.", len(state["leads"]), OUTPUT_FILE)
        write_csv(state)
        sys.exit(0)

    t0 = time.time()

    def budget_ok():
        return (time.time() - t0) < MAX_RUN_SECONDS

    async with async_playwright() as pw:
        browser, ctx, page = await make_browser(pw)
        try:
            # ── Phase 0: DISCOVER ──
            if state["phase"] == "discover":
                log.info("═" * 50)
                log.info("PHASE 0: DISCOVER CATEGORIES & GROUPS")
                log.info("═" * 50)
                done = await phase_discover(page, state, budget_ok)
                if not done:
                    log.info("Discovery incomplete — will continue next run")

            # ── Phase 1: SCAN LISTINGS ──
            if state["phase"] == "scan" and budget_ok():
                log.info("═" * 50)
                log.info("PHASE 1: SCAN LISTING PAGES")
                log.info("═" * 50)
                done = await phase_scan(page, state, budget_ok)
                if not done:
                    remaining = len(state["scan_queue"])
                    log.info("Scan incomplete — %d groups remaining", remaining)

            # ── Phase 2: PROFILES ──
            if state["phase"] == "profiles" and budget_ok():
                log.info("═" * 50)
                log.info("PHASE 2: CHECK SELLER PROFILES")
                log.info("═" * 50)
                done = await phase_profiles(page, state, budget_ok)
                if not done:
                    remaining = len(state["profile_queue"])
                    log.info("Profiles incomplete — %d remaining", remaining)

        finally:
            await browser.close()

    # Always write CSV with whatever leads we have so far
    write_csv(state)

    elapsed = time.time() - t0
    log.info(
        "Run stats: %.1f min elapsed · %d pages · %d leads",
        elapsed / 60, state["stats"]["pages"], len(state["leads"]),
    )

    if state["phase"] == "done":
        log.info("🎉  ALL DONE — %d leads written to %s", len(state["leads"]), OUTPUT_FILE)
        sys.exit(0)
    else:
        log.info("⏸  Paused (time limit) — will continue on next run")
        sys.exit(2)


if __name__ == "__main__":
    asyncio.run(main())
