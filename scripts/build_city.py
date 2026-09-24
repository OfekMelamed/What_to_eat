#!/usr/bin/env python3
"""
בונה מאגר מסעדות לעיר אחת — בחינם, בלי מפתחות.

מקורות:
  1. Overture Maps (places)   — רשימת המקומות המלאה (מבוסס בעיקר על Meta/Microsoft)
  2. OpenStreetMap (Overpass) — שעות פתיחה, סוג מטבח, כשרות, שם בעברית
  3. האתר של כל מקום          — תמונת השיתוף (og:image) והאייקון (לוגו)

שימוש:
  python scripts/build_city.py "תל אביב"
  python scripts/build_city.py "Rome" --add-to-list
  python scripts/build_city.py --all                 # בונה מחדש כל עיר ב-cities.txt
  python scripts/build_city.py "חיפה" --no-images    # מהיר יותר, בלי מעבר על אתרים

הפלט: data/<slug>.json ו-data/index.json
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import math
import re
import sys
import time
import unicodedata
from pathlib import Path
from urllib.parse import urljoin, urlparse

import duckdb
import requests

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CITIES_FILE = ROOT / "cities.txt"
UA = "bis-food-swipe/1.0 (personal, non-commercial; github pages)"
DEFAULT_RELEASE = "2026-09-23.0"
S3_BUCKET = "overturemaps-us-west-2"
OVERPASS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
    "https://maps.mail.ru/osm/tools/overpass/api/interpreter",
]

session = requests.Session()
session.headers["User-Agent"] = UA


def log(*a):
    print(*a, file=sys.stderr, flush=True)


# ---------------------------------------------------------------- geo helpers
def km(lat1, lon1, lat2, lon2):
    r = math.radians
    h = math.sin(r(lat2 - lat1) / 2) ** 2 + math.cos(r(lat1)) * math.cos(r(lat2)) * math.sin(r(lon2 - lon1) / 2) ** 2
    return 2 * 6371 * math.asin(math.sqrt(h))


def slugify(s: str) -> str:
    s = unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-") or "city"


def geocode(city: str) -> dict:
    """Nominatim: שם העיר -> מרכז + גבולות. מחזיר גם שם באנגלית (ל-slug) ובעברית (לתצוגה)."""
    out = {}
    for lang in ("en", "he"):
        r = session.get("https://nominatim.openstreetmap.org/search",
                        params={"q": city, "format": "jsonv2", "limit": 1, "accept-language": lang}, timeout=20)
        r.raise_for_status()
        j = r.json()
        if not j:
            raise SystemExit(f"לא מצאתי את העיר '{city}'. נסה שם מדויק יותר, למשל 'Tel Aviv, Israel'.")
        out[lang] = j[0]
        time.sleep(1.1)  # מדיניות השימוש של Nominatim
    en = out["en"]
    s, n, w, e = map(float, en["boundingbox"])
    return {
        "name": out["he"]["name"] or city,
        "slug": slugify(en["name"]),
        "center": [float(en["lat"]), float(en["lon"])],
        "bbox": [s, w, n, e],
    }


def bbox_around(lat, lon, radius_km):
    dlat = radius_km / 111
    dlon = radius_km / (111 * math.cos(math.radians(lat)))
    return [lat - dlat, lon - dlon, lat + dlat, lon + dlon]


# ---------------------------------------------------------------- Overture
def latest_release() -> str:
    try:
        r = session.get(f"https://{S3_BUCKET}.s3.us-west-2.amazonaws.com/",
                        params={"list-type": "2", "prefix": "release/", "delimiter": "/"}, timeout=20)
        rel = sorted(re.findall(r"<Prefix>release/([^/<]+)/</Prefix>", r.text))
        if rel:
            return rel[-1]
    except Exception as e:  # noqa: BLE001
        log("  could not list Overture releases:", e)
    return DEFAULT_RELEASE


EXCLUDE = re.compile(r"(store|market|grocery|supermarket|wholesale|catering|distribut|butcher|liquor|"
                     r"supplier|manufactur|delivery_service|vending|farm|kiosk_food_supply)", re.I)
BAR = {"bar", "pub", "cocktail_bar", "wine_bar", "beer_bar", "sports_bar", "lounge", "beer_garden",
       "brewery", "brewpub", "nightclub", "hookah_bar", "whiskey_bar", "gastropub"}
CAFE = {"cafe", "coffee_shop", "tea_room", "coffee_roastery", "internet_cafe", "bubble_tea"}
SWEET = {"ice_cream_shop", "ice_cream", "frozen_yoghurt_shop", "gelato", "dessert_shop", "donut_shop",
         "bakery", "patisserie_cake_shop", "cupcake_shop", "chocolatier", "creperie", "waffle"}
FAST = {"fast_food_restaurant", "fast_food", "sandwich_shop", "hot_dog", "food_truck", "falafel_restaurant",
        "shawarma_restaurant", "pizza_restaurant", "burger_restaurant", "hamburger_restaurant", "kebab"}


def classify(primary: str, hierarchy: list, alternates: list):
    labels = [x for x in [primary, *(alternates or [])] if x]
    if not labels or EXCLUDE.search(primary or ""):
        return None
    everything = set(labels) | set(hierarchy or [])
    if everything & BAR:
        amenity = "pub" if everything & {"pub", "brewery", "brewpub", "gastropub"} else "bar"
    elif everything & CAFE:
        amenity = "cafe"
    elif everything & {"ice_cream_shop", "ice_cream", "frozen_yoghurt_shop", "gelato"}:
        amenity = "ice_cream"
    elif everything & SWEET:
        amenity = "cafe"
    elif everything & {"fast_food_restaurant", "fast_food", "food_truck", "sandwich_shop"}:
        amenity = "fast_food"
    else:
        amenity = "restaurant"
    cuisines = []
    for l in labels:
        k = re.sub(r"_(restaurant|shop|bar|house)$", "", l)
        k = {"hamburger": "burger", "coffee": "coffee_shop", "ice_cream": "ice_cream", "patisserie_cake": "cake",
             "steak": "steak_house", "casual_eatery": "", "restaurant": "", "cafe": "", "food_and_drink": ""}.get(k, k)
        if k and k not in cuisines:
            cuisines.append(k)
    return amenity, cuisines[:4]


def load_overture(bbox, release, parquet_override=None, min_conf=0.35):
    s, w, n, e = bbox
    path = parquet_override or f"s3://{S3_BUCKET}/release/{release}/theme=places/type=place/*"
    con = duckdb.connect()
    if not parquet_override:
        con.execute("INSTALL httpfs; LOAD httpfs; SET s3_region='us-west-2';")
    src = f"read_parquet('{path}', hive_partitioning=1)"
    cols = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall()}
    log(f"  Overture columns: {sorted(cols)}")

    if "taxonomy" in cols:
        food = "list_contains(taxonomy.hierarchy, 'food_and_drink')"
    elif "categories" in cols:
        food = ("(categories.primary LIKE '%restaurant%' OR categories.primary IN "
                "('cafe','coffee_shop','bar','pub','bakery','ice_cream_shop','dessert_shop','fast_food_restaurant',"
                "'sandwich_shop','cocktail_bar','wine_bar','food_truck','juice_bar','tea_room','bistro','diner'))")
    else:
        raise SystemExit("Overture schema changed: no taxonomy/categories column")
    json_cols = [c for c in ("names", "taxonomy", "categories", "basic_category", "websites", "socials",
                             "phones", "addresses", "brand", "sources", "operating_status") if c in cols]
    select = ", ".join(["id", "bbox.xmin AS lon", "bbox.ymin AS lat", "confidence"]
                       + [f"to_json({c}) AS {c}" for c in json_cols])
    sql = f"""
        SELECT {select} FROM {src}
        WHERE bbox.xmin BETWEEN {w} AND {e} AND bbox.ymin BETWEEN {s} AND {n}
          AND {food} AND coalesce(confidence, 0) >= {min_conf}
    """
    t = time.time()
    rows = con.execute(sql).fetchall()
    names = ["id", "lon", "lat", "confidence", *json_cols]
    log(f"  Overture: {len(rows)} food places ({time.time() - t:.0f}s)")
    out = []
    for row in rows:
        r = dict(zip(names, row))
        for c in json_cols:
            if isinstance(r[c], str):
                try:
                    r[c] = json.loads(r[c])
                except json.JSONDecodeError:
                    pass
        out.append(r)
    return out


def overture_to_place(r):
    status = (r.get("operating_status") or "")
    if isinstance(status, str) and "closed" in status:
        return None
    tax = r.get("taxonomy") or {}
    cat = r.get("categories") or {}
    primary = tax.get("primary") or cat.get("primary") or r.get("basic_category") or ""
    hierarchy = tax.get("hierarchy") or []
    alternates = tax.get("alternates") or cat.get("alternate") or []
    cls = classify(primary, hierarchy, alternates)
    if not cls:
        return None
    amenity, cuisines = cls
    names = r.get("names") or {}
    common = names.get("common") or {}
    name = common.get("he") or names.get("primary")
    if not name:
        return None
    addr = (r.get("addresses") or [{}])[0] or {}
    socials = r.get("socials") or []
    websites = [w for w in (r.get("websites") or []) if w]
    site = next((w for w in websites if not re.search(r"facebook|instagram|wa\.me|linktr", w, re.I)), "")
    insta = next((s for s in socials + websites if "instagram.com" in s), "")
    brand = r.get("brand") or {}
    nsrc = len({(s or {}).get("dataset") for s in (r.get("sources") or [])}) or 1
    return {
        "id": "ov/" + str(r["id"]),
        "name": name,
        "lat": round(r["lat"], 6), "lon": round(r["lon"], 6),
        "amenity": amenity, "cuisines": cuisines,
        "website": site, "instagram": insta,
        "facebook": next((s for s in socials if "facebook.com" in s), ""),
        "phone": (r.get("phones") or [""])[0] or "",
        "address": ", ".join(x for x in [addr.get("freeform"), addr.get("locality")] if x),
        "brandQid": brand.get("wikidata") or "",
        "_conf": r.get("confidence") or 0.5, "_nsrc": nsrc, "_social": bool(socials),
    }


# ---------------------------------------------------------------- OpenStreetMap
def load_osm(bbox):
    s, w, n, e = bbox
    q = f"""[out:json][timeout:90];
    (nwr["amenity"~"^(restaurant|cafe|fast_food|ice_cream|bar|pub|food_court|biergarten)$"]["name"]({s},{w},{n},{e}););
    out center tags;"""
    for url in OVERPASS:
        try:
            r = session.post(url, data={"data": q}, timeout=120)
            r.raise_for_status()
            els = r.json()["elements"]
            log(f"  OSM: {len(els)} places from {urlparse(url).hostname}")
            return els
        except Exception as ex:  # noqa: BLE001
            log(f"  OSM {urlparse(url).hostname} failed: {ex}")
    return []


def norm_name(s: str) -> str:
    s = unicodedata.normalize("NFKC", s or "").lower()
    s = re.sub(r"[\"'׳״`’.,\-–&()/|!]+", " ", s)
    s = re.sub(r"\b(restaurant|cafe|café|bar|the|מסעדת|מסעדה|קפה|בר)\b", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def names_match(a: str, b: str) -> bool:
    a, b = norm_name(a), norm_name(b)
    if not a or not b:
        return False
    if a == b or (len(a) > 3 and a in b) or (len(b) > 3 and b in a):
        return True
    ta, tb = set(a.split()), set(b.split())
    return len(ta & tb) / max(1, min(len(ta), len(tb))) >= 0.6


def osm_tags_to_fields(t):
    f = {}
    if t.get("opening_hours"):
        f["hours"] = t["opening_hours"]
    if t.get("cuisine"):
        f["cuisines"] = [c.strip().lower().replace(" ", "_") for c in re.split(r"[;,]", t["cuisine"]) if c.strip()]
    for k, key in (("kosher", "diet:kosher"), ("vegan", "diet:vegan"), ("vegetarian", "diet:vegetarian"),
                   ("takeaway", "takeaway")):
        if re.search(r"yes|only", t.get(key, "")):
            f[k] = True
    for k, key in (("outdoor", "outdoor_seating"), ("delivery", "delivery"), ("wheelchair", "wheelchair")):
        if t.get(key) == "yes":
            f[k] = True
    if t.get("description:he") or t.get("description"):
        f["summary"] = t.get("description:he") or t.get("description")
    site = t.get("website:menu") or t.get("menu:url")
    if site:
        f["menu"] = site
    if t.get("brand:wikidata"):
        f["brandQid"] = t["brand:wikidata"]
    if re.match(r"^https?://.+\.(jpe?g|png|webp)$", t.get("image", ""), re.I):
        f["photo"] = t["image"]
    return f


def merge_osm(places, osm_elements):
    """מצמיד לכל מקום מ-Overture את הנתונים מ-OSM (שעות, מטבח, כשרות). מקומות שקיימים רק ב-OSM מתווספים."""
    grid = {}
    for p in places:
        grid.setdefault((round(p["lat"], 3), round(p["lon"], 3)), []).append(p)
    matched = added = 0
    for el in osm_elements:
        t = el.get("tags") or {}
        lat = el.get("lat") or (el.get("center") or {}).get("lat")
        lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if lat is None or not t.get("name"):
            continue
        cands = []
        for dy in (-0.001, 0, 0.001):
            for dx in (-0.001, 0, 0.001):
                cands += grid.get((round(lat + dy, 3), round(lon + dx, 3)), [])
        osm_names = [t.get("name"), t.get("name:he"), t.get("name:en")]
        best = None
        for p in cands:
            if km(lat, lon, p["lat"], p["lon"]) < 0.08 and any(names_match(p["name"], nm) for nm in osm_names if nm):
                best = p
                break
        f = osm_tags_to_fields(t)
        if best:
            matched += 1
            if t.get("name:he"):
                best["name"] = t["name:he"]
            best["cuisines"] = list(dict.fromkeys((f.pop("cuisines", []) or []) + best["cuisines"]))[:4]
            if not best.get("website") and t.get("website"):
                best["website"] = t["website"]
            if not best.get("phone") and t.get("phone"):
                best["phone"] = t["phone"]
            best.update({k: v for k, v in f.items() if not best.get(k)})
            best["_osm"] = True
        else:
            added += 1
            street = f'{t.get("addr:street", "")} {t.get("addr:housenumber", "")}'.strip()
            p = {
                "id": f"osm/{el['type']}/{el['id']}", "name": t.get("name:he") or t["name"],
                "lat": round(lat, 6), "lon": round(lon, 6), "amenity": t.get("amenity", "restaurant"),
                "cuisines": f.pop("cuisines", []), "website": t.get("website") or t.get("contact:website") or "",
                "instagram": t.get("contact:instagram", ""), "phone": t.get("phone") or t.get("contact:phone") or "",
                "address": ", ".join(x for x in [street, t.get("addr:city")] if x),
                "_conf": 0.55, "_nsrc": 1, "_social": bool(t.get("contact:instagram")), "_osm": True,
            }
            p.update(f)
            places.append(p)
            grid.setdefault((round(lat, 3), round(lon, 3)), []).append(p)
    log(f"  merged OSM: {matched} matched, {added} added")


# ---------------------------------------------------------------- website images
META_RE = re.compile(r"<meta\b[^>]*>", re.I)
LINK_RE = re.compile(r"<link\b[^>]*>", re.I)
ATTR_RE = re.compile(r'([a-zA-Z:-]+)\s*=\s*("([^"]*)"|\'([^\']*)\')')


def attrs(tag):
    return {m.group(1).lower(): (m.group(3) if m.group(3) is not None else m.group(4)) for m in ATTR_RE.finditer(tag)}


def normalize_url(u):
    u = (u or "").split(";")[0].strip()
    if not u:
        return ""
    return u if re.match(r"^https?://", u, re.I) else "https://" + u


def site_images(url):
    """מחזיר (תמונת שיתוף, אייקון) מהאתר של המקום."""
    try:
        r = session.get(url, timeout=8, stream=True, allow_redirects=True)
        if r.status_code >= 400 or "html" not in r.headers.get("content-type", ""):
            return None, None
        html = r.raw.read(400_000, decode_content=True).decode(r.encoding or "utf-8", "ignore")
        base = r.url
    except Exception:  # noqa: BLE001
        return None, None
    photo = icon = None
    for tag in META_RE.findall(html):
        a = attrs(tag)
        key = (a.get("property") or a.get("name") or "").lower()
        if key in ("og:image", "og:image:url", "og:image:secure_url", "twitter:image") and a.get("content"):
            photo = photo or urljoin(base, a["content"].strip())
    icons = []
    for tag in LINK_RE.findall(html):
        a = attrs(tag)
        rel = (a.get("rel") or "").lower()
        if "icon" in rel and a.get("href") and not a["href"].startswith("data:"):
            size = max([int(x) for x in re.findall(r"(\d+)x\d+", a.get("sizes", ""))] or [0])
            icons.append((("apple-touch-icon" in rel) * 1000 + size, urljoin(base, a["href"].strip())))
    if icons:
        icon = max(icons)[1]
    if photo and (photo.startswith("data:") or re.search(r"favicon|\.ico($|\?)|\.svg($|\?)", photo, re.I)):
        photo = None
    return photo, icon


def enrich_images(places, workers=16):
    todo = [p for p in places if p.get("website") and not p.get("photo")]
    log(f"  reading {len(todo)} websites for images…")
    done = 0
    with cf.ThreadPoolExecutor(workers) as ex:
        futs = {ex.submit(site_images, normalize_url(p["website"])): p for p in todo}
        for fut in cf.as_completed(futs):
            p = futs[fut]
            photo, icon = fut.result()
            if photo:
                p["photo"] = photo
                done += 1
            if icon:
                p["logo"] = icon
    log(f"  got {done} photos from websites")


# ---------------------------------------------------------------- scoring & output
def popularity(p):
    """אין דירוגים חינמיים, אז זה מדד של 'כמה המקום מבוסס': ביטחון של Overture, מספר מקורות, רשתות, אתר, שעות."""
    s = 0.40 * float(p.get("_conf", 0.5))
    s += 0.18 * min(1.0, (p.get("_nsrc", 1) - 1) / 2)
    s += 0.12 * bool(p.get("_social") or p.get("instagram"))
    s += 0.10 * bool(p.get("website"))
    s += 0.08 * bool(p.get("hours"))
    s += 0.07 * bool(p.get("brandQid"))
    s += 0.05 * bool(p.get("photo"))
    return round(min(1.0, s), 3)


def finalize(places):
    out, seen = [], set()
    for p in places:
        key = (norm_name(p["name"]), round(p["lat"], 3), round(p["lon"], 3))
        if key in seen:
            continue
        seen.add(key)
        p["website"] = normalize_url(p.get("website"))
        if p.get("menu"):
            p["menu"] = normalize_url(p["menu"])
        p["pop"] = popularity(p)
        out.append({k: v for k, v in p.items() if not k.startswith("_") and v not in ("", None, [], False)})
    out.sort(key=lambda p: -p["pop"])
    return out


def write_city(meta, places, release):
    DATA.mkdir(exist_ok=True)
    doc = {
        "city": meta["name"], "slug": meta["slug"], "center": meta["center"], "bbox": meta["bbox"],
        "built": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d"), "overture": release,
        "attribution": "Overture Maps Foundation (CDLA-Permissive-2.0), © OpenStreetMap contributors (ODbL)",
        "count": len(places), "places": places,
    }
    (DATA / f"{meta['slug']}.json").write_text(json.dumps(doc, ensure_ascii=False, separators=(",", ":")), "utf-8")
    idx_path = DATA / "index.json"
    idx = json.loads(idx_path.read_text("utf-8")) if idx_path.exists() else {"cities": []}
    idx["cities"] = [c for c in idx["cities"] if c["slug"] != meta["slug"]]
    idx["cities"].append({k: doc[k] for k in ("city", "slug", "center", "bbox", "built", "count")})
    idx["cities"].sort(key=lambda c: c["city"])
    idx_path.write_text(json.dumps(idx, ensure_ascii=False, indent=1), "utf-8")
    log(f"✔ {meta['name']}: {len(places)} places -> data/{meta['slug']}.json")


def build(city, args):
    log(f"\n=== {city} ===")
    if args.bbox:
        s, w, n, e = map(float, args.bbox.split(","))
        meta = {"name": args.name or city, "slug": args.slug or slugify(city), "center": [(s + n) / 2, (w + e) / 2],
                "bbox": [s, w, n, e]}
    else:
        meta = geocode(city)
        if args.radius_km:
            meta["bbox"] = bbox_around(*meta["center"], args.radius_km)
        if args.slug:
            meta["slug"] = args.slug
    s, w, n, e = meta["bbox"]
    if (n - s) > 0.6 or (e - w) > 0.6:
        log("  area is very large; limiting to 15 km around the center (use --radius-km to change)")
        meta["bbox"] = bbox_around(*meta["center"], 15)
    release = args.release or latest_release()
    log(f"  bbox={[round(x, 4) for x in meta['bbox']]}  overture={release}")

    rows = load_overture(meta["bbox"], release, args.parquet, args.min_conf)
    places = [p for p in (overture_to_place(r) for r in rows) if p]
    if not args.no_osm:
        merge_osm(places, load_osm(meta["bbox"]))
    if not args.no_images:
        enrich_images(places)
    write_city(meta, finalize(places), release)
    return meta


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("city", nargs="?", help="שם העיר, למשל 'תל אביב' או 'Rome, Italy'")
    ap.add_argument("--all", action="store_true", help="לבנות מחדש כל עיר שב-cities.txt")
    ap.add_argument("--add-to-list", action="store_true", help="להוסיף את העיר ל-cities.txt (לריענון החודשי)")
    ap.add_argument("--radius-km", type=float, help="במקום גבולות העיר: ריבוע ברדיוס הזה סביב המרכז")
    ap.add_argument("--bbox", help="s,w,n,e — לדלג על חיפוש העיר")
    ap.add_argument("--name"), ap.add_argument("--slug")
    ap.add_argument("--release", help="גרסת Overture (ברירת מחדל: האחרונה)")
    ap.add_argument("--parquet", help=argparse.SUPPRESS)  # לבדיקות מקומיות
    ap.add_argument("--min-conf", type=float, default=0.35)
    ap.add_argument("--no-osm", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    args = ap.parse_args()

    if args.all:
        cities = [c.strip() for c in CITIES_FILE.read_text("utf-8").splitlines()
                  if c.strip() and not c.startswith("#")] if CITIES_FILE.exists() else []
        failed = []
        for c in cities:
            try:
                build(c, args)
            except Exception as ex:  # noqa: BLE001
                log(f"✘ {c}: {ex}")
                failed.append(c)
        if failed:
            raise SystemExit(f"failed: {failed}")
        return
    if not args.city:
        ap.error("צריך לכתוב שם עיר או --all")
    build(args.city, args)
    if args.add_to_list:
        existing = CITIES_FILE.read_text("utf-8").splitlines() if CITIES_FILE.exists() else []
        if args.city not in existing:
            CITIES_FILE.write_text("\n".join([*existing, args.city]).strip() + "\n", "utf-8")


if __name__ == "__main__":
    main()
