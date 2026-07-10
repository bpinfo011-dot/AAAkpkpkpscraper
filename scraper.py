#!/usr/bin/env python3
"""
KupujemProdajem Lead Scraper v3 — 20 parallel workers
======================================================
Usage:
  python scraper.py discover                  → scan_plan.json
  python scraper.py scan --worker 0 --of 20   → leads_0.csv
  python scraper.py merge                     → leads.csv
"""

import argparse, asyncio, csv, json, logging, os, random, re, sys, time, heapq

BASE     = "https://www.kupujemprodajem.com"
DELAY    = (1.2, 2.5)       # conservative for 20 concurrent workers
RETRIES  = 3
TIMEOUT  = 30_000
MIN_APPEAR = 3
MIN_REV    = 50
MIN_ADS    = 20

NEXT_RE = re.compile(r'<script[^>]*id="__NEXT_DATA__"[^>]*>(.*?)</script>', re.DOTALL)
logging.basicConfig(level=logging.INFO, format="%(asctime)s  %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("kp")

def kp_int(v):
    if isinstance(v, int): return v
    return int(str(v or "0").replace(".", "").replace(",", "")) if v else 0

def slugify(t):
    t = t.translate(str.maketrans("šđčćžŠĐČĆŽ", "sdcczsdccz")).lower()
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", t)).strip("-")

# ── Browser ────────────────────────────────────────────────────
async def init_browser(pw):
    br = await pw.chromium.launch(headless=True)
    ctx = await br.new_context(
        viewport={"width": 1366, "height": 768},
        user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/125.0 Safari/537.36")
    await ctx.route(re.compile(r"\.(png|jpe?g|gif|svg|webp|woff2?|ttf|eot|ico)(\?|$)", re.I), lambda r: r.abort())
    await ctx.route(re.compile(r"(google-analytics|googletagmanager|facebook|doubleclick)", re.I), lambda r: r.abort())
    return br, await ctx.new_page()

async def get_redux(page, url):
    for att in range(1, RETRIES + 1):
        try:
            r = await page.goto(url, wait_until="domcontentloaded", timeout=TIMEOUT)
            if r and r.status == 404: return None
            html = await page.content()
            m = NEXT_RE.search(html)
            if not m:
                await asyncio.sleep(2); html = await page.content(); m = NEXT_RE.search(html)
            if m:
                d = json.loads(m.group(1))
                rx = d.get("props", {}).get("initialReduxState")
                if rx: return rx, html
        except Exception as e:
            log.warning("  err %s att %d: %s", url, att, e)
        await asyncio.sleep(2 * att + random.random())
    return None

async def dismiss_cookies(page):
    for sel in ['button:has-text("Prihvatam")', 'button:has-text("Prihvati")', ".cookie-consent button"]:
        try:
            b = page.locator(sel).first
            if await b.is_visible(timeout=1500): await b.click(); return
        except: pass

# ── DISCOVER ───────────────────────────────────────────────────
async def cmd_discover():
    from playwright.async_api import async_playwright
    async with async_playwright() as pw:
        br, pg = await init_browser(pw)
        try:
            # Homepage → categories
            log.info("Loading homepage")
            res = await get_redux(pg, BASE + "/")
            if not res: log.error("Homepage failed"); sys.exit(1)
            await dismiss_cookies(pg)
            rx, _ = res
            cats = rx.get("category", {}).get("categories", {})
            log.info("Found %d categories", len(cats))

            # Visit each category → groups + page count
            all_groups = []  # [(catId, groupId, name, pages)]
            cat_names = {}

            for cid, info in cats.items():
                cid = str(cid)
                cat_names[cid] = info["name"]
                url = f"{BASE}/pretraga?categoryId={cid}"
                res = await get_redux(pg, url)
                if not res:
                    log.warning("  ✗ %s", info["name"]); continue
                rx2, _ = res
                sr = rx2.get("search", {})
                total_pages = sr.get("pages", 0)

                gall = rx2.get("group", {}).get("groups", {})
                cat_g = gall.get(cid, {})
                found = 0
                for gid, g in cat_g.items():
                    if g.get("active") in ("yes", True):
                        all_groups.append({
                            "catId": cid, "groupId": str(gid),
                            "name": f"{info['name']} > {g['name']}",
                            "pages": 0  # will discover on first visit
                        })
                        found += 1

                if not found:
                    # flat category (no groups) → treat as single item
                    all_groups.append({
                        "catId": cid, "groupId": None,
                        "name": info["name"],
                        "pages": total_pages
                    })

                log.info("  %s: %d groups, %d total pages", info["name"], found, total_pages)
                await asyncio.sleep(random.uniform(*DELAY))

            # Balance assignment across workers using greedy approach
            log.info("Total items: %d", len(all_groups))

        finally:
            await br.close()

    # Save plan
    plan = {"cat_names": cat_names, "groups": all_groups}
    with open("scan_plan.json", "w", encoding="utf-8") as f:
        json.dump(plan, f, ensure_ascii=False)
    log.info("Saved scan_plan.json (%d groups)", len(all_groups))


# ── SCAN ───────────────────────────────────────────────────────
async def cmd_scan(worker_id, total_workers):
    from playwright.async_api import async_playwright

    # Load plan
    plan = json.load(open("scan_plan.json", encoding="utf-8"))
    groups = plan["groups"]

    # Assign items round-robin (simple, good enough balance)
    my_items = [g for i, g in enumerate(groups) if i % total_workers == worker_id]
    log.info("Worker %d/%d: %d groups to scan", worker_id, total_workers, len(my_items))

    if not my_items:
        log.info("Nothing to do"); _write_partial(worker_id, []); return

    # Stagger start to avoid thundering herd
    stagger = worker_id * 3
    log.info("Staggering %ds …", stagger)
    await asyncio.sleep(stagger)

    user_data = {}   # uid → {count, category, group}
    profile_slugs = {}
    pages_done = 0
    t0 = time.time()
    max_secs = 5 * 3600 + 10 * 60  # 5h10m

    def budget_ok():
        return (time.time() - t0) < max_secs

    async with async_playwright() as pw:
        br, pg = await init_browser(pw)
        try:
            # ── Phase 1: SCAN ──
            log.info("── PHASE 1: SCAN ──")
            for gi, item in enumerate(my_items):
                if not budget_ok(): break
                cid, gid = item["catId"], item["groupId"]

                # Visit first page to get total pages
                url = f"{BASE}/pretraga?categoryId={cid}"
                if gid: url += f"&groupId={gid}"
                res = await get_redux(pg, url)
                if not res:
                    log.warning("  ✗ %s", item["name"]); continue

                rx, html = res
                sr = rx.get("search", {})
                total_pages = sr.get("pages", 0)
                _harvest_ads(sr, user_data)
                _harvest_slugs(html, profile_slugs)
                pages_done += 1

                log.info("  [%d/%d] %s: %d pages",
                         gi + 1, len(my_items), item["name"], total_pages)

                # Pages 2..N
                for p in range(2, total_pages + 1):
                    if not budget_ok(): break
                    purl = f"{url}&page={p}"
                    res2 = await get_redux(pg, purl)
                    if res2:
                        rx2, html2 = res2
                        _harvest_ads(rx2.get("search", {}), user_data)
                        _harvest_slugs(html2, profile_slugs)
                        pages_done += 1
                    if p % 50 == 0:
                        log.info("    … page %d/%d  (users=%d)", p, total_pages, len(user_data))
                    await asyncio.sleep(random.uniform(*DELAY))

                await asyncio.sleep(random.uniform(*DELAY))

            log.info("Scan done: %d pages, %d users", pages_done, len(user_data))

            # ── Phase 2: PROFILES ──
            log.info("── PHASE 2: PROFILES ──")
            candidates = [uid for uid, d in user_data.items() if d["count"] >= MIN_APPEAR]
            candidates.sort(key=lambda u: -user_data[u]["count"])
            log.info("Candidates: %d (≥%d appearances)", len(candidates), MIN_APPEAR)

            leads = []
            for ci, uid in enumerate(candidates):
                if not budget_ok(): break
                slug = profile_slugs.get(uid, "u")
                url = f"{BASE}/{slug}/svi-oglasi/{uid}/1"
                res = await get_redux(pg, url)
                if not res and slug != "u":
                    res = await get_redux(pg, f"{BASE}/u/svi-oglasi/{uid}/1")
                if res:
                    rx, _ = res
                    sm = rx.get("user", {}).get("summary", {})
                    meta = rx.get("meta", {})
                    pos = kp_int(sm.get("reviewsPositive", "0"))
                    act = kp_int(sm.get("userActiveAdCount", 0))
                    if pos >= MIN_REV and act >= MIN_ADS:
                        leads.append({
                            "ime": sm.get("username", ""),
                            "link_profila": BASE + meta.get("pageUrl", f"/u/svi-oglasi/{uid}/1"),
                            "broj_ocena": pos,
                            "broj_aktivnih_oglasa": act,
                            "kategorija": user_data[uid]["category"]
                        })
                        log.info("  ✓ #%d %s (%d rev, %d ads)",
                                 len(leads), sm.get("username", "?"), pos, act)

                if (ci + 1) % 50 == 0:
                    log.info("  … checked %d/%d, leads=%d", ci + 1, len(candidates), len(leads))
                await asyncio.sleep(random.uniform(*DELAY))

        finally:
            await br.close()

    _write_partial(worker_id, leads)
    log.info("Worker %d done: %d leads", worker_id, len(leads))


def _harvest_ads(search, user_data):
    for ad in search.get("byId", {}).values():
        uid = ad.get("userId")
        if not uid: continue
        uid = str(uid)
        if uid not in user_data:
            user_data[uid] = {"count": 0, "category": ad.get("categoryName", ""),
                              "group": ad.get("groupName", "")}
        user_data[uid]["count"] += 1

def _harvest_slugs(html, slugs):
    for slug, uid in re.findall(r'href="/([^/]+)/svi-oglasi/(\d+)/\d+"', html):
        if slug != "u": slugs[uid] = slug

FIELDS = ["ime", "link_profila", "broj_ocena", "broj_aktivnih_oglasa", "kategorija"]

def _write_partial(worker_id, leads):
    fname = f"leads_{worker_id}.csv"
    with open(fname, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(leads)
    log.info("Wrote %s (%d leads)", fname, len(leads))


# ── MERGE ──────────────────────────────────────────────────────
def cmd_merge():
    all_leads = []
    seen = set()
    for fname in sorted(f for f in os.listdir(".") if f.startswith("leads_") and f.endswith(".csv")):
        with open(fname, encoding="utf-8") as f:
            for row in csv.DictReader(f):
                key = row["link_profila"]
                if key not in seen:
                    seen.add(key)
                    row["broj_ocena"] = int(row["broj_ocena"])
                    row["broj_aktivnih_oglasa"] = int(row["broj_aktivnih_oglasa"])
                    all_leads.append(row)

    all_leads.sort(key=lambda x: -x["broj_ocena"])
    with open("leads.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS); w.writeheader(); w.writerows(all_leads)

    log.info("Merged %d unique leads from %d worker files into leads.csv",
             len(all_leads), len([f for f in os.listdir(".") if f.startswith("leads_")]))


# ── CLI ────────────────────────────────────────────────────────
if __name__ == "__main__":
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("discover")
    s = sub.add_parser("scan")
    s.add_argument("--worker", type=int, required=True)
    s.add_argument("--of", type=int, default=20, dest="total")
    sub.add_parser("merge")
    args = p.parse_args()

    if args.cmd == "discover":
        asyncio.run(cmd_discover())
    elif args.cmd == "scan":
        asyncio.run(cmd_scan(args.worker, args.total))
    elif args.cmd == "merge":
        cmd_merge()
    else:
        p.print_help()
