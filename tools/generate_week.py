#!/usr/bin/env python3
"""
Deterministic weekly meal-plan generator for the family site.

Run by the `meal-plan-weekly` cloud routine (claude.ai/code Routines) every Sunday
morning. It reads the repo's own data files and rewrites index.html's `var WEEK`
object plus history.json for the CURRENT Sun-Sat week, then the routine sanity-checks,
commits, and pushes so GitHub Pages redeploys.

Design goal: keep all menu logic HERE (deterministic, testable) so the routine agent
never has to improvise a menu — that improvisation is what silently broke the old
local jobs for weeks. See RUNBOOK.md.

Stdlib only. No external dependencies.

Idempotency: if index.html already shows the current week's label, this exits with
"ALREADY_CURRENT" and changes nothing — safe to run any number of times per week.

Testing: set GEN_WEEK_DATE=YYYY-MM-DD to simulate a run on that date without waiting
for a real Sunday, e.g.  GEN_WEEK_DATE=2026-08-02 python tools/generate_week.py

Rules encoded (from the family's meal-planning rules):
  - Week runs Sun-Sat; label like "August 2 - August 8, 2026".
  - Play-the-whole-deck rotation: prefer least-recently-served dishes (history.json).
  - Don't repeat anything served in the last ~week.
  - Protein variety: no protein more than twice; at most ONE chicken night; no same
    protein on back-to-back days.
  - New-dish cap: at most 2 dishes with no rotation history in a full week.
  - Every default gets 2 swap alternates, each a DIFFERENT protein from the default.
  - Never'd dishes (never.json) are hard-excluded from defaults AND alternates.
  - Liked dishes (likes.json) get a small ranking boost.
  - Quicker dishes are steered to Mon-Thu; involved cooks land Fri/Sat.
  - Every option carries an explicit img:"images/<slug>.jpg" (decouples photo from title).
  - Known-spicy dishes carry a teen-friendly swap note.
"""

import json, re, os, sys, datetime

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import week_format as wf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# ---- tunables (rules that aren't derivable from the data) ----
CHICKEN_MAX = 1
PROTEIN_MAX = 2
NEW_DISH_CAP = 2
RECENT_EXCLUDE_DAYS = 8   # don't reuse anything served within this many days
QUICK_MINUTES = 30        # <= this counts as a "quick" weeknight cook

# Known spicy dishes -> teen-friendly note (the data has no spice field).
SPICY_NOTE = {
    "sheet-pan-kielbasa-peppers": "Kielbasa carries a little kick — for the teen, swap in <b>bratwurst</b> (a milder sausage).",
    "italian-sausage-peppers-subs": "Use <b>sweet</b> Italian sausage (not hot) to keep it teen-friendly.",
    "chicken-tikka-masala": "Make it mild — go easy on the cayenne for the teen.",
}

PROT_LABEL = {"pork":"Pork","chicken":"Chicken","seafood":"Seafood","beef":"Beef",
              "turkey":"Turkey","eggs":"Eggs","lamb":"Lamb","veg":"Veggie","other":"Other"}

MONTHS = ["","January","February","March","April","May","June","July","August",
          "September","October","November","December"]

DAYS = [("sun","Sunday"),("mon","Monday"),("tue","Tuesday"),("wed","Wednesday"),
        ("thu","Thursday"),("fri","Friday"),("sat","Saturday")]


def slugify(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")


def fix_mojibake(o):
    """Repair cp1252-as-utf8 mojibake that exists in recipe_library.json."""
    if isinstance(o, str):
        if any(c in o for c in ("Ã", "Â", "â", "€")):
            try:
                return o.encode("cp1252").decode("utf-8")
            except (UnicodeEncodeError, UnicodeDecodeError):
                return o
        return o
    if isinstance(o, list):
        return [fix_mojibake(x) for x in o]
    if isinstance(o, dict):
        return {k: fix_mojibake(v) for k, v in o.items()}
    return o


def load_json(name):
    with open(os.path.join(ROOT, name), encoding="utf-8") as f:
        return json.load(f)


def parse_date(s):
    return datetime.datetime.strptime(s, "%Y-%m-%d").date()


def week_bounds(today):
    # Sunday-start week containing `today`. weekday(): Mon=0..Sun=6.
    days_since_sun = (today.weekday() + 1) % 7
    start = today - datetime.timedelta(days=days_since_sun)
    end = start + datetime.timedelta(days=6)
    return start, end


def week_label(start, end):
    return "%s %d – %s %d, %d" % (MONTHS[start.month], start.day,
                                  MONTHS[end.month], end.day, end.year)


def build():
    override = os.environ.get("GEN_WEEK_DATE")
    today = parse_date(override) if override else datetime.date.today()
    start, end = week_bounds(today)
    dates = [start + datetime.timedelta(days=i) for i in range(7)]
    label = week_label(start, end)

    idx_path = os.path.join(ROOT, "index.html")
    idx = open(idx_path, encoding="utf-8").read()
    if wf.current_label(idx) == label:
        print("ALREADY_CURRENT: %s" % label)
        return 0

    lib = load_json("recipe_library.json")
    recipes = lib["recipes"] if isinstance(lib, dict) and "recipes" in lib else lib
    recipes = [fix_mojibake(r) for r in recipes]
    by_slug = {r["slug"]: r for r in recipes}

    likes = load_json("likes.json").get("liked", {})
    never = load_json("never.json").get("never", {})
    hist = load_json("history.json")
    last_used_raw = hist.get("lastUsed", {})

    # history keyed by title/coreEntree; normalize to slug -> most-recent date
    hist_by_slug = {}
    for k, v in last_used_raw.items():
        s = slugify(k)
        d = parse_date(v)
        if s not in hist_by_slug or d > hist_by_slug[s]:
            hist_by_slug[s] = d

    never_slugs = set(never.keys()) | {slugify(t) for t in never.values()}
    liked_slugs = set(likes.keys())

    OLD = datetime.date(2000, 1, 1)

    def lu(r):
        return hist_by_slug.get(r["slug"], OLD)

    def is_new(r):
        return r["slug"] not in hist_by_slug

    def recent(r):
        return (today - lu(r)).days < RECENT_EXCLUDE_DAYS

    # eligible pool: not never'd, not served in the last week
    pool = [r for r in recipes if r["slug"] not in never_slugs and not recent(r)]

    # rank: least-recently-served first; liked dishes get a small nudge earlier
    def rank_key(r):
        boost = datetime.timedelta(days=25) if r["slug"] in liked_slugs else datetime.timedelta(0)
        return (lu(r) - boost, r["title"])

    pool.sort(key=rank_key)

    # ---- greedily choose 7 defaults honoring the protein rules ----
    chosen = []
    prot_count = {}
    chicken = 0
    new_ct = 0

    def try_fill(enforce_adjacent, enforce_spread, enforce_chicken, enforce_newcap):
        nonlocal chosen, prot_count, chicken, new_ct
        chosen, prot_count, chicken, new_ct = [], {}, 0, 0
        used = set()
        for _ in range(7):
            prev = chosen[-1]["protein"] if chosen else None
            pick = None
            for r in pool:
                if r["slug"] in used:
                    continue
                p = r["protein"]
                if enforce_spread and prot_count.get(p, 0) >= PROTEIN_MAX:
                    continue
                if enforce_chicken and p == "chicken" and chicken >= CHICKEN_MAX:
                    continue
                if enforce_adjacent and p == prev:
                    continue
                if enforce_newcap and is_new(r) and new_ct >= NEW_DISH_CAP:
                    continue
                pick = r
                break
            if not pick:
                return False
            used.add(pick["slug"])
            chosen.append(pick)
            prot_count[pick["protein"]] = prot_count.get(pick["protein"], 0) + 1
            if pick["protein"] == "chicken":
                chicken += 1
            if is_new(pick):
                new_ct += 1
        return True

    # progressively relax constraints until 7 slots fill (should succeed at full strength)
    for args in [(True, True, True, True),
                 (False, True, True, True),
                 (False, True, False, True),
                 (False, False, False, True),
                 (False, False, False, False)]:
        if try_fill(*args):
            break

    # ---- order the 7 across the week: involved cooks -> Fri/Sat, avoid adjacent protein ----
    def minutes(r):
        m = re.search(r"\d+", r.get("time", "") or "")
        return int(m.group()) if m else 25

    involved = sorted(chosen, key=minutes, reverse=True)[:2]      # two longest -> weekend
    weekday_dishes = [r for r in chosen if r not in involved]     # 5 items: Sun + Mon-Thu
    # Sun gets the longer weekday cook; Mon-Thu get the quicker ones.
    weekday_dishes.sort(key=minutes)               # quick -> long
    sunday = weekday_dishes[-1]                     # longest of the weekday group -> Sunday
    mon_thu = weekday_dishes[:-1]                   # remaining four
    week_order = [sunday] + mon_thu + involved     # Sun, Mon-Thu, Fri, Sat  (7 total)

    # de-adjacent same protein where a simple swap fixes it
    for i in range(1, len(week_order)):
        if week_order[i]["protein"] == week_order[i-1]["protein"]:
            for k in range(i+1, len(week_order)):
                if (week_order[k]["protein"] != week_order[i-1]["protein"] and
                        (i+1 >= len(week_order) or week_order[k]["protein"] != week_order[i+1]["protein"])):
                    week_order[i], week_order[k] = week_order[k], week_order[i]
                    break

    chosen_slugs = {r["slug"] for r in week_order}

    # ---- pick 2 alternates per default: different protein from default & each other ----
    def alt_pool(default):
        cands = [r for r in recipes
                 if r["slug"] not in never_slugs
                 and r["slug"] not in chosen_slugs
                 and r["protein"] != default["protein"]]
        cands.sort(key=rank_key)
        return cands

    def option(r, dayabbr, is_default):
        o = {
            "title": r["title"],
            "sides": r.get("sides", ""),
            "protein": PROT_LABEL.get(r.get("protein", "other"), r.get("protein", "").title()),
            "time": r.get("time", ""),
            "img": "images/%s.jpg" % r["slug"],
            "included": r.get("included", ""),
        }
        if r["slug"] in SPICY_NOTE:
            o["swapNote"] = SPICY_NOTE[r["slug"]]
        o["steps"] = r.get("steps", [])
        o["lunchTitle"] = r.get("lunchTitle", "")
        o["lunch"] = r.get("lunch", "")
        o["groceries"] = [{"name": g.get("name", ""), "det": dayabbr, "cat": g.get("cat", "Pantry")}
                          for g in r.get("groceries", [])]
        return o

    meals = []
    used_alts = set()          # spread alternates across the week — don't repeat the same swaps
    for (mid, dayname), r in zip(DAYS, week_order):
        abbr = dayname[:3]
        opts = [option(r, abbr, True)]
        seen_prot = {r["protein"]}
        # pass 1 avoids alternates already used earlier this week; pass 2 relaxes that
        # only if the different-protein pool runs thin, so every night still gets 2 swaps.
        for avoid_used in (True, False):
            for cand in alt_pool(r):
                if len(opts) >= 3:
                    break
                if cand["protein"] in seen_prot:
                    continue
                if avoid_used and cand["slug"] in used_alts:
                    continue
                opts.append(option(cand, abbr, False))
                seen_prot.add(cand["protein"])
                used_alts.add(cand["slug"])
            if len(opts) >= 3:
                break
        meals.append({"id": mid, "day": dayname, "polo": False, "options": opts})

    # ---- intro / portions text (auto, accurate) ----
    prot_list = []
    for r in week_order:
        pl = PROT_LABEL.get(r["protein"], r["protein"])
        if pl not in prot_list:
            prot_list.append(pl)
    new_titles = [r["title"] for r in week_order if is_new(r)]
    new_phrase = ""
    if new_titles:
        new_phrase = " New this week: %s." % ", ".join(new_titles)
    intro = ("A fresh Sun–Sat week pulled from the least-recently-served dishes in the "
             "rotation. %d proteins across the week (%s)%s%s Weeknights stay quick; the "
             "more involved cooks land Friday and Saturday." % (
                 len(prot_list), ", ".join(p.lower() for p in prot_list),
                 " with one chicken night." if any(x["protein"] == "chicken" for x in week_order) else ".",
                 new_phrase))
    portions = ("Quantities are family-sized with next-day lunches in mind — each dinner is "
                "meant to leave leftovers.")

    WEEK = {
        "label": label,
        "version": today.isoformat(),
        "intro": intro,
        "portions": portions,
        "meals": meals,
        # `staples` is reset empty each new week; a persistent-staples feature would
        # carry these forward, but today they're author-added per week. See RUNBOOK.
        "staples": [],
    }

    new_js = wf.render_week(WEEK)          # compact style matching the hand-authored file
    out = wf.splice_week(idx, new_js)      # replaces ONLY the WEEK block; UI untouched
    with open(idx_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(out)

    # ---- update history.json with the 7 defaults at their day dates ----
    for r, d in zip(week_order, dates):
        last_used_raw[r["title"]] = d.isoformat()
    hist["lastUsed"] = last_used_raw
    with open(os.path.join(ROOT, "history.json"), "w", encoding="utf-8", newline="\n") as f:
        json.dump(hist, f, ensure_ascii=False, indent=2)

    # ---- summary for the routine log ----
    print("GENERATED: %s" % label)
    print("New dishes: %d (%s)" % (len(new_titles), ", ".join(new_titles) or "none"))
    print("Protein spread: %s" % ", ".join("%s x%d" % (PROT_LABEL.get(k, k), v)
                                            for k, v in prot_count.items()))
    for (mid, dayname), r in zip(DAYS, week_order):
        alts = [o["title"] for o in next(m for m in meals if m["id"] == mid)["options"][1:]]
        print("  %-9s %-38s [%s]  alts: %s" % (
            dayname, r["title"], PROT_LABEL.get(r["protein"], r["protein"]), ", ".join(alts)))
    return 0


if __name__ == "__main__":
    sys.exit(build())
