#!/usr/bin/env python3
"""
Build duas.json from the Google Sheet (one tab per category).

Run by the GitHub Action. Reads each tab as CSV via the public gviz endpoint
(the sheet must be shared "anyone with the link can view" — no API key /
service account needed), validates everything, and writes duas.json. Exits
non-zero on ANY validation error so CI fails loudly *before* a broken file is
committed and fetched by the app.

Usage:
    DUAS_SHEET_KEY=<sheet-id> python build_duas.py
    python build_duas.py --local ./csv      # offline dry-run: reads ./csv/<tab>.csv
    python build_duas.py --out duas.json
"""
import argparse, collections, csv, io, json, os, re, sys, urllib.parse, urllib.request

# Tab name -> JSON section. Occasion tabs map to "special" with category == tab.
OCCASIONS = ["friday", "ramadan", "lastTenRamadan", "laylatAlQadr",
             "eid", "arafah", "dhulHijjahFirstTen"]
ALL_TABS = ["general"] + OCCASIONS + ["fridayDefault", "quranDuaas"]

# Required non-empty fields per section — mirrors the app's DuaOverlayStore
# mappers, so the build never emits an entry the app would silently drop.
# id presence is validated by check_ids_* (which also suggests the next id), so
# it's deliberately not repeated here.
REQ_GENERAL = ["arabicMale", "arabicFemale", "arabicLibraryMale", "arabicLibraryFemale"]
REQ_SPECIAL = ["arabicMale", "arabicFemale", "arabicLibraryMale", "arabicLibraryFemale"]
REQ_FRIDAY  = ["arabic", "english"]
REQ_QD      = ["ayahText", "surah", "ayatStart", "ayatEnd",
               "duaaMale", "duaaFemale", "duaaLibraryMale", "duaaLibraryFemale"]

errors = []


def fail(msg):
    errors.append(msg)


# ---------------------------------------------------------------- fetch tabs
def fetch_tab_remote(key, tab):
    url = ("https://docs.google.com/spreadsheets/d/%s/gviz/tq?tqx=out:csv&sheet=%s"
           % (key, urllib.parse.quote(tab)))
    req = urllib.request.Request(url, headers={"User-Agent": "duas-build"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8")


def fetch_tab_local(d, tab):
    path = os.path.join(d, tab + ".csv")
    if not os.path.exists(path):
        return None
    return open(path, encoding="utf-8").read()


def rows_of(csv_text):
    """Parse CSV → list of dicts with stripped keys/values; drop blank rows."""
    out = []
    for raw in csv.DictReader(io.StringIO(csv_text)):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items() if k}
        if any(row.values()):
            out.append(row)
    return out


# ---------------------------------------------------------------- id helpers
def check_ids_numeric(rows, section):
    """For tabs whose ids are '<prefix><number>'. The prefix is DERIVED from the
    existing ids (so it adapts to your scheme, e.g. 'gen-', 'ramadan-',
    'dhul-hijjah-') rather than being imposed. Validates: present, unique within
    the tab, sharing that prefix; suggests the next free id (preserving any
    zero-padding) when one is missing."""
    ids = [r.get("id", "") for r in rows if r.get("id", "")]
    prefixes = collections.Counter(re.sub(r"\d+$", "", i) for i in ids)
    prefix = prefixes.most_common(1)[0][0] if prefixes else ""
    suffixes = [m.group(1) for i in ids if (m := re.search(r"(\d+)$", i))]
    zero_pad = any(s.startswith("0") and len(s) > 1 for s in suffixes)
    width = max((len(s) for s in suffixes), default=0) if zero_pad else 0
    maxn = max((int(s) for s in suffixes), default=0)
    nxt = ("%s%0*d" % (prefix, width, maxn + 1)) if zero_pad else ("%s%d" % (prefix, maxn + 1))

    seen = {}
    for i, r in enumerate(rows):
        rid, loc = r.get("id", ""), "[%s] row %d" % (section, i + 2)  # +2: header + 1-indexed
        if not rid:
            fail("%s: missing 'id' — next free id is '%s'" % (loc, nxt)); continue
        if prefix and not rid.startswith(prefix):
            fail("%s: id '%s' doesn't match this tab's prefix '%s' (wrong tab?)" % (loc, rid, prefix))
        if rid in seen:
            fail("%s: duplicate id '%s' (also row %d)" % (loc, rid, seen[rid]))
        else:
            seen[rid] = i + 2


def check_ids_loose(rows, section, prefix):
    """For tabs with free-form semantic ids (quranDuaas, e.g. 'qd-maryam-63').
    Validates: present, starts with `prefix`, unique within the tab."""
    seen = {}
    for i, r in enumerate(rows):
        rid, loc = r.get("id", ""), "[%s] row %d" % (section, i + 2)
        if not rid:
            fail("%s: missing 'id' (use '%s<surah>-<ayah>', e.g. '%smaryam-63')" % (loc, prefix, prefix)); continue
        if not rid.startswith(prefix):
            fail("%s: id '%s' must start with '%s'" % (loc, rid, prefix))
        if rid in seen:
            fail("%s: duplicate id '%s' (also row %d)" % (loc, rid, seen[rid]))
        else:
            seen[rid] = i + 2


def require(rows, section, fields):
    for i, r in enumerate(rows):
        for f in fields:
            if not r.get(f, ""):
                fail("[%s] row %d: missing required '%s'" % (section, i + 2, f))


def as_int(rows, section, field):
    for i, r in enumerate(rows):
        v = r.get(field, "")
        if v and not v.lstrip("-").isdigit():
            fail("[%s] row %d: '%s' must be a whole number, got %r"
                 % (section, i + 2, field, v))


# ---------------------------------------------------------------- build
def build(get_tab):
    duas, special, friday, quran_duaas = [], [], [], []

    # general -> duas
    g = rows_of(get_tab("general") or "")
    require(g, "general", REQ_GENERAL)
    check_ids_numeric(g, "general")
    for r in g:
        duas.append({
            "id": r["id"], "arabicMale": r["arabicMale"], "arabicFemale": r["arabicFemale"],
            "englishMale": r.get("englishMale", ""), "englishFemale": r.get("englishFemale", ""),
            "arabicLibraryMale": r["arabicLibraryMale"], "arabicLibraryFemale": r["arabicLibraryFemale"],
            "category": "dua",
        })

    # occasion tabs -> special (category == tab name)
    for cat in OCCASIONS:
        rows = rows_of(get_tab(cat) or "")
        require(rows, cat, REQ_SPECIAL)
        check_ids_numeric(rows, cat)
        for r in rows:
            special.append({
                "id": r["id"], "category": cat,
                "arabicMale": r["arabicMale"], "arabicFemale": r["arabicFemale"],
                "arabicLibraryMale": r["arabicLibraryMale"], "arabicLibraryFemale": r["arabicLibraryFemale"],
                "englishMale": r.get("englishMale", ""), "englishFemale": r.get("englishFemale", ""),
                "englishLibraryMale": r.get("englishLibraryMale", ""),
                "englishLibraryFemale": r.get("englishLibraryFemale", ""),
            })

    # fridayDefault -> friday
    fd = rows_of(get_tab("fridayDefault") or "")
    require(fd, "fridayDefault", REQ_FRIDAY)
    seen_ar = set()
    for i, r in enumerate(fd):
        if r["arabic"] in seen_ar:
            fail("[fridayDefault] row %d: duplicate Arabic text" % (i + 2))
        seen_ar.add(r["arabic"])
        friday.append({"arabic": r["arabic"], "english": r["english"]})

    # quranDuaas -> quranDuaas
    qd = rows_of(get_tab("quranDuaas") or "")
    require(qd, "quranDuaas", REQ_QD)
    as_int(qd, "quranDuaas", "surah"); as_int(qd, "quranDuaas", "ayatStart"); as_int(qd, "quranDuaas", "ayatEnd")
    check_ids_loose(qd, "quranDuaas", "qd-")
    for i, r in enumerate(qd):
        try:
            su, a1, a2 = int(r["surah"]), int(r["ayatStart"]), int(r["ayatEnd"])
            if not (1 <= su <= 114):
                fail("[quranDuaas] row %d: surah %d out of 1…114" % (i + 2, su))
            if a1 > a2:
                fail("[quranDuaas] row %d: ayatStart %d > ayatEnd %d" % (i + 2, a1, a2))
        except ValueError:
            continue
        def opt(k): return r[k] if r.get(k) else None
        quran_duaas.append({
            "id": r["id"], "surah": int(r["surah"]),
            "ayatStart": int(r["ayatStart"]), "ayatEnd": int(r["ayatEnd"]),
            "ayahText": r["ayahText"], "ayahEnglish": opt("ayahEnglish"),
            "reference": opt("reference"), "englishReference": opt("englishReference"),
            "duaaMale": r["duaaMale"], "duaaFemale": r["duaaFemale"],
            "duaaLibraryMale": r["duaaLibraryMale"], "duaaLibraryFemale": r["duaaLibraryFemale"],
            "duaaEnglishMale": opt("duaaEnglishMale"), "duaaEnglishFemale": opt("duaaEnglishFemale"),
            "duaaEnglishLibraryMale": opt("duaaEnglishLibraryMale"),
            "duaaEnglishLibraryFemale": opt("duaaEnglishLibraryFemale"),
        })

    # Global id uniqueness across every id-bearing section (friday/default has none).
    seen = {}
    for sect, items in (("general", duas), ("special", special), ("quranDuaas", quran_duaas)):
        for it in items:
            rid = it["id"]
            if rid in seen and seen[rid] != sect:   # within-section dups already reported by check_ids_*
                fail("duplicate id '%s' used in both [%s] and [%s]" % (rid, seen[rid], sect))
            else:
                seen.setdefault(rid, sect)

    return {"duas": duas, "special": special, "friday": friday, "quranDuaas": quran_duaas}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--local", help="read ./<dir>/<tab>.csv instead of the live Sheet")
    ap.add_argument("--out", default="duas.json")
    args = ap.parse_args()

    if args.local:
        get_tab = lambda tab: fetch_tab_local(args.local, tab)
    else:
        key = os.environ.get("DUAS_SHEET_KEY", "").strip()
        if not key:
            print("ERROR: DUAS_SHEET_KEY is not set.", file=sys.stderr); sys.exit(2)
        get_tab = lambda tab: fetch_tab_remote(key, tab)

    sections = build(get_tab)

    if errors:
        print("VALIDATION FAILED (%d issue(s)) — duas.json NOT written:\n" % len(errors), file=sys.stderr)
        for e in errors:
            print("  • " + e, file=sys.stderr)
        sys.exit(1)

    # Bump version only when content changed, so identical content produces a
    # byte-identical file and the Action makes no commit.
    old_version = 0
    old_body = None
    if os.path.exists(args.out):
        try:
            prev = json.load(open(args.out, encoding="utf-8"))
            old_version = int(prev.get("version", 0) or 0)
            old_body = {k: prev.get(k) for k in ("duas", "special", "friday", "quranDuaas")}
        except Exception:
            pass
    changed = old_body != sections
    version = (old_version + 1) if changed else old_version or 1

    doc = {"schemaVersion": 1, "version": version, **sections}
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print("OK: wrote %s  (duas=%d special=%d friday=%d quranDuaas=%d, version=%d, changed=%s)"
          % (args.out, len(sections["duas"]), len(sections["special"]),
             len(sections["friday"]), len(sections["quranDuaas"]), version, changed))


if __name__ == "__main__":
    main()
