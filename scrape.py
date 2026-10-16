#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scrape.py — scraper generik berbasis base-url + endpoint (buat Termux / Linux / macOS).

Dependensi:
    pip install requests beautifulsoup4 lxml
    (requests saja sudah cukup untuk target JSON; bs4 cuma dipakai kalau parse HTML)

Lihat contoh lengkap di README.md, atau jalankan: python scrape.py --help
"""

from __future__ import annotations

import argparse
import csv
import io
import json
import os
import random
import re
import sys
import time
from dataclasses import dataclass
from urllib.parse import parse_qsl, urlencode, urljoin, urlparse, urlunparse

try:
    import requests
except ImportError:
    sys.exit("Butuh 'requests':  pip install requests")

__version__ = "1.0.0"

# --------------------------------------------------------------------------- #
# util: path & header
# --------------------------------------------------------------------------- #

TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _is_int(tok: str) -> bool:
    return bool(re.fullmatch(r"-?\d+", tok))


def split_path(path: str) -> list[str]:
    """'data.items[*].name' -> ['data', 'items', '*', 'name']"""
    out: list[str] = []
    for part in path.replace("[*]", ".*").split("."):
        if part == "":
            continue
        out.append(part)
    return out


def get_path(obj, path: str, default=None):
    """Ambil nilai dari dict/list pakai dot-path. '[*]' / angka = masuk ke list."""
    cur = obj
    for tok in split_path(path):
        if tok == "*":
            if not isinstance(cur, list):
                return default
            return cur
        if isinstance(cur, dict):
            if tok not in cur:
                return default
            cur = cur[tok]
        elif isinstance(cur, list):
            if not _is_int(tok):
                return default
            i = int(tok)
            if -len(cur) <= i < len(cur):
                cur = cur[i]
            else:
                return default
        else:
            return default
    return cur


def flatten(obj, prefix: str = "") -> dict:
    """Ratakan JSON jadi {'a.b.0.c': nilai} — enak buat CSV."""
    out: dict = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            out.update(flatten(v, f"{prefix}{k}."))
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out.update(flatten(v, f"{prefix}{i}."))
    else:
        obj_s = "" if obj is None else obj
        out[prefix.rstrip(".")] = obj_s
    return out


def parse_headers(pairs: list[str] | None) -> dict:
    """'Key: Value' atau 'Key=Value'."""
    headers = {}
    for raw in pairs or []:
        if ":" in raw:
            k, v = raw.split(":", 1)
        elif "=" in raw:
            k, v = raw.split("=", 1)
        else:
            sys.exit(f"--header gak valid (pakai 'Nama: Nilai'): {raw}")
        headers[k.strip()] = v.strip()
    return headers


def parse_params(pairs: list[str] | None) -> dict:
    """'page=2' atau 'page:2'."""
    params = {}
    for raw in pairs or []:
        sep = "=" if "=" in raw else ":"
        if sep not in raw:
            sys.exit(f"--param gak valid (pakai 'nama=nilai'): {raw}")
        k, v = raw.split(sep, 1)
        params[k.strip()] = v.strip()
    return params


def parse_auth(s: str | None):
    if not s:
        return None
    if ":" not in s:
        sys.exit("--auth harus 'user:password'")
    u, p = s.split(":", 1)
    return (u, p)


# --------------------------------------------------------------------------- #
# definisi field
# --------------------------------------------------------------------------- #

@dataclass
class Field:
    """
    Format --field:  nama=lokasi
      JSON : items[*].price
      HTML : h3 a@title        (selector CSS + atribut)
             .price@text       (text = isi teks, default kalau @ tidak ditulis)
    Prefix opsional:  'json:' atau 'css:'
    """
    name: str
    loc: str
    attr: str = "text"

    @classmethod
    def parse(cls, raw: str) -> "Field":
        if "=" not in raw:
            sys.exit(f"--field harus 'nama=lokasi': {raw}")
        name, loc = raw.split("=", 1)
        name, loc = name.strip(), loc.strip()
        attr = "text"
        for pref in ("json:", "css:"):
            if loc.startswith(pref):
                loc = loc[len(pref):]
        if loc.startswith("@"):                      # @href  =>  * @href
            attr, loc = loc[1:].strip(), "*"
        elif "@" in loc and loc.count("@") == 1:
            loc, attr = [x.strip() for x in loc.split("@")]
        if not name:
            sys.exit(f"nama field kosong: {raw}")
        return cls(name=name, loc=loc or "*", attr=attr or "text")


@dataclass
class Record:
    url: str
    status: int
    data: dict


# --------------------------------------------------------------------------- #
# ekstraksi
# --------------------------------------------------------------------------- #

def extract_json(payload, args) -> list[dict]:
    items = get_path(payload, args.items) if args.items else payload
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        sys.exit(
            f"--items '{args.items}' hasilnya bukan list (dapat: {type(items).__name__}).\n"
            f"Coba mode inspeksi:  python scrape.py --base-url {args.base_url} "
            f"--endpoint {args.endpoint[0]} --inspect"
        )

    fields = [Field.parse(f) for f in args.field] if args.field else None
    out = []
    for it in items:
        if not isinstance(it, (dict, list)):
            it = {"value": it}
        if fields:
            row = {f.name: get_path(it, f.loc) for f in fields}
        else:
            row = flatten(it)
        out.append(row)
    return out


def extract_html(soup, page_url: str, args) -> list[dict]:
    try:
        from bs4 import BeautifulSoup  # noqa: F401
    except ImportError:
        sys.exit("Parse HTML butuh bs4:  pip install beautifulsoup4 lxml")

    fields = [Field.parse(f) for f in args.field] if args.field else []
    if not fields:
        return []

    # field pertama dianggap "scope" kalau --item diisi; kalau tidak, tiap field ambil semua match
    scope = soup.select(args.item) if args.item else [soup]
    if not scope:
        return []

    def pick(node, f: Field):
        if f.loc in ("", "*"):
            el = node
        else:
            el = node.select_one(f.loc) if node is not soup else node.select_one(f.loc)
        if el is None:
            return ""
        a = f.attr.lower()
        if a in ("text", "get_text", ""):
            return el.get_text(args.sep, strip=True)
        if a in ("html", "inner"):
            return "".join(str(x) for x in el.contents)
        if a == "outer":
            return str(el)
        if a in ("href", "src") and el.has_attr(a):
            return urljoin(page_url, el[a])          # jadikan URL absolut
        v = el.get(a, "")
        if isinstance(v, list):                     # mis. class
            v = " ".join(v)
        return v

    out = []
    for node in scope:
        out.append({f.name: pick(node, f) for f in fields})
    return out


def find_next(payload_or_soup, page_url: str, args) -> str | None:
    if args.next is None:
        return None
    if args.as_mode == "json" or (args.as_mode == "auto" and isinstance(payload_or_soup, (dict, list))):
        val = get_path(payload_or_soup, args.next)
        if isinstance(val, (int, float)) and args.next_param:
            return f"?{args.next_param}={int(val) + 1}"
        if isinstance(val, str) and val:
            return urljoin(page_url, val)
        return None
    # HTML: cari selector 'a.next' dsb
    if args.next_param:
        m = re.search(rf"{re.escape(args.next_param)}=(\d+)", page_url)
        nxt = (int(m.group(1)) + 1) if m else (args.start_page + 1)
        base, _, _ = page_url.partition("?")
        return f"{base}?{args.next_param}={nxt}"
    try:
        el = payload_or_soup.select_one(args.next)
    except Exception:
        return None
    if el is None:
        return None
    href = el.get("href")
    return urljoin(page_url, href) if href else None


# --------------------------------------------------------------------------- #
# fetch
# --------------------------------------------------------------------------- #

SKIP_TAG = {"html", "body", "head", "script", "style", "link", "meta", "title",
            "noscript", "svg", "path", "br", "hr"}


def auto_items_path(payload):
    """JSON: cari list-of-dict terbesar, kembalikan dot-path-nya."""
    best = {"path": "", "n": -1}

    def walk(node, path):
        if isinstance(node, list):
            if node and all(isinstance(x, dict) for x in node) and len(node) > best["n"]:
                best["path"], best["n"] = path, len(node)
            for i, v in enumerate(node):
                walk(v, f"{path}.{i}" if path else str(i))
        elif isinstance(node, dict):
            for k, v in node.items():
                walk(v, f"{path}.{k}" if path else str(k))

    walk(payload, "")
    return best["path"], best["n"]


def auto_item_selector(soup):
    """
    Tebak selector item berulang (kartu / baris tabel) dari sebuah halaman.

    Sinyal yang dipakai, hasil pengukuran di halaman nyata:
      jumlah      -> item biasanya banyak (n ** 0.8, bukan linear biar wrapper gak menang)
      isi teks    -> item punya teks; elemen dekoratif (ikon, spacer) disingkirkan
      jumlah anak -> kartu punya beberapa anak; tapi >60 = pembungkus halaman
      keseragaman -> item asli mirip satu sama lain (CV panjang teks <= 0.4)
    """
    groups = {}
    for el in soup.find_all(True):
        if el.name in SKIP_TAG:
            continue
        cls = el.get("class")
        if not cls:
            continue
        key = f"{el.name}.{'.'.join(cls[:3])}"
        groups.setdefault(key, []).append(el)

    def nilai_untuk(min_count):
        out = []
        for key, el_list in groups.items():
            if len(el_list) < min_count:
                continue
            sampel = el_list[:12]
            n = len(sampel)
            teks = [len(e.get_text(" ", strip=True)) for e in sampel]
            anak = [len(e.find_all(True)) for e in sampel]
            t_avg = sum(teks) / n
            a_avg = sum(anak) / n

            # elemen tanpa isi apa pun (ikon, garis, spacer) -> buang
            if t_avg <= 0:
                if not (sampel[0].name in ("a", "img") or sampel[0].find(["a", "img"])):
                    continue

            # keseragaman panjang teks antar item
            if n >= 3 and t_avg > 0:
                var = sum((x - t_avg) ** 2 for x in teks) / n
                cv = (var ** 0.5) / t_avg
                seragam = 1.0 if cv <= 0.4 else max(0.15, 0.4 / cv)
            else:
                seragam = 0.7

            kaya = (1 + min(t_avg, 300) / 60) * (1 + min(a_avg, 20) / 5)
            if a_avg > 60:                       # pembungkus sekelas halaman
                kaya *= 60 / a_avg

            out.append(((len(el_list) ** 0.8) * kaya * seragam, key, len(el_list)))
        return out

    for ambang in (3, 2):
        skor = nilai_untuk(ambang)
        if skor:
            skor.sort(key=lambda x: -x[0])
            return skor[0][1], skor[0][2]
    return None, 0


def auto_html_fields(soup, selector, maksimum=10):
    """Tebak kolom dari anak elemen yang konsisten muncul di tiap item."""
    items = soup.select(selector)
    if not items:
        return []
    hitung = {}
    contoh = {}
    for it in items:
        for el in it.find_all(True):
            if el.name in SKIP_TAG:
                continue
            cls = el.get("class")
            key = f"{el.name}.{'.'.join(cls[:2])}" if cls else el.name
            hitung[key] = hitung.get(key, 0) + 1
            if key not in contoh:
                contoh[key] = el
    ambang = max(2, len(items) * 0.5)
    kandidat = [k for k, n in hitung.items() if n >= ambang]
    kandidat.sort(key=lambda k: -hitung[k])

    fields = []
    for k in kandidat[:maksimum]:
        nama = re.sub(r"[^a-z0-9]+", "_", k.lower()).strip("_")
        attr = "href" if contoh[k].name == "a" else ("src" if contoh[k].name == "img" else "text")
        fields.append(Field(name=nama, loc=k, attr=attr))
    if not fields:
        # item-nya elemen daun (gak punya anak ber-class) -> ambil teks + href/src dirinya
        pertama = items[0]
        if pertama.name == "a":
            fields = [Field(name="teks", loc="", attr="text"), Field(name="link", loc="", attr="href")]
        elif pertama.name == "img":
            fields = [Field(name="alt", loc="", attr="alt"), Field(name="src", loc="", attr="src")]
        else:
            fields = [Field(name="teks", loc="", attr="text")]
    return fields


def join_url(base: str | None, ep: str) -> str:
    """Gabung base-url + endpoint. Endpoint boleh URL absolut."""
    ep = (ep or "").strip()
    if ep.startswith("http://") or ep.startswith("https://"):
        return ep
    if not base:
        sys.exit(f"endpoint '{ep}' butuh --base-url (atau pakai URL absolut)")
    return urljoin(base.rstrip("/") + "/", ep.lstrip("/"))


def merge_url(url: str, params: dict | None) -> str:
    """Gabungkan query param ke URL tanpa duplikasi (param baru menang)."""
    if not params:
        return url
    p = urlparse(url)
    q = {k: v for k, v in parse_qsl(p.query, keep_blank_values=True)}
    for k, v in params.items():
        q[k] = str(v)
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))


def build_session(args) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": args.user_agent,
        "Accept": "application/json, text/html;q=0.9, */*;q=0.8",
        "Accept-Language": "id-ID,id;q=0.9,en;q=0.8",
    })
    s.headers.update(args.headers or {})
    if args.auth:
        s.auth = parse_auth(args.auth)
    if args.proxy:
        s.proxies = {"http": args.proxy, "https": args.proxy}
    if args.cookies and os.path.exists(args.cookies):
        try:
            with open(args.cookies, encoding="utf-8") as f:
                for c in json.load(f):
                    s.cookies.set(c["name"], c["value"], domain=c.get("domain", ""), path=c.get("path", "/"))
            log(f"cookie dimuat dari {args.cookies} ({len(s.cookies)} item)")
        except Exception as e:
            log(f"peringatan: gagal baca cookie ({e})")
    return s


def fetch(url: str, sesi: requests.Session, args, method: str | None = None,
          data=None, json_body=None, params=None, percobaan: int | None = None) -> dict | None:
    percobaan = percobaan if percobaan is not None else args.retry
    for i in range(max(1, percobaan)):
        try:
            url_final = merge_url(url, params)
            if args.verbose:
                log(f"  {method or args.method} {url_final}"
                    + (f"  body={json.dumps(json_body, ensure_ascii=False)[:120]}" if json_body else ""))
            r = sesi.request(
                method or args.method, url_final,
                data=data, json=json_body,
                timeout=args.timeout, verify=not args.insecure,
                allow_redirects=not args.no_redirect,
            )
            if r.status_code == 429 or r.status_code >= 500:
                tunggu = args.delay * (2 ** i) + random.uniform(0, 1)
                retry_after = r.headers.get("Retry-After")
                if retry_after and retry_after.isdigit():
                    tunggu = max(tunggu, int(retry_after))
                log(f"  HTTP {r.status_code} — tunggu {tunggu:.1f}s lalu retry ({i + 1}/{percobaan})")
                time.sleep(tunggu)
                continue
            if not args.allow_status and not (200 <= r.status_code < 300):
                log(f"  HTTP {r.status_code} untuk {url}")
                if args.verbose:
                    log("  " + r.text[:400].replace("\n", " "))
                return None
            r.encoding = args.encoding or r.apparent_encoding or "utf-8"
            return {
                "url": r.url, "status": r.status_code, "text": r.text,
                "headers": dict(r.headers), "elapsed": r.elapsed.total_seconds(),
                "ctype": r.headers.get("Content-Type", ""),
            }
        except requests.RequestException as e:
            log(f"  error: {e} — retry ({i + 1}/{percobaan})")
            time.sleep(args.delay * (i + 1))
    return None


def save_cookies(sesi, path: str):
    data = [{"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in sesi.cookies]
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    log(f"cookie disimpan ke {path} ({len(data)} item)")


def log(msg: str):
    if not ARGS.quiet:
        print(msg, file=sys.stderr, flush=True)


# --------------------------------------------------------------------------- #
# mode inspeksi — buat nemu path/selector tanpa buka laptop
# --------------------------------------------------------------------------- #

JS_URL_RE = re.compile(r"""["'`](assets/[A-Za-z0-9._@/-]+?\.js|[A-Za-z0-9._@/-]*?\.js)["'`]""")
ABS_URL_RE = re.compile(r"https?://[A-Za-z0-9._~:/?#@!$&*+,;=%-]{6,120}")
API_PATH_RE = re.compile(r"""["'`](/(?:api|v\d|rest|graphql|ajax|wp-json)/[A-Za-z0-9._/{},:$-]{1,80})["'`]""")
SUPA_TABLE_RE = re.compile(r'\.from\(\s*[\x22\x27\x60]([a-z_][a-z0-9_]{1,40})[\x22\x27\x60]\s*\)')
RPC_RE = re.compile(r'\.rpc\(\s*[\x22\x27\x60]([a-zA-Z_][a-zA-Z0-9_]{1,50})[\x22\x27\x60]')
SUPA_URL_RE = re.compile(r"https://([a-z0-9]{10,30})\.supabase\.co")
FIREBASE_RE = re.compile(r"https://([a-z0-9-]+)\.firebaseio\.com|([a-z0-9-]+)\.firebaseapp\.com")

# URL yang jelas bukan API (aset/CDN/sosial) -> disaring dari hasil
NOISE = ("fonts.g", "cdn.jsdelivr", "unpkg.com", "googleapis.com/css", "gstatic",
         "facebook.com/sharer", "twitter.com/intent", "t.me/share", "wa.me",
         "api.whatsapp", "wsrv.nl", "gravatar.com", "schema.org", "w3.org",
         "sentry.io", "googletagmanager", "google-analytics", "cloudflareinsights")


def analisis_situs(url: str, teks: str, sesi: requests.Session, args, cetak=None):
    """
    Bedah sebuah halaman: struktur HTML + semua bundle JS.
    Kembalikan dict temuan (endpoint, tabel, URL eksternal, sinyal SPA).
    `cetak` = fungsi log (None = senyap).
    """
    def katakan(*a):
        if cetak:
            cetak(*a)

    try:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(teks, "lxml")
        except Exception:
            soup = BeautifulSoup(teks, "html.parser")
    except ImportError:
        soup = None
    # --- sinyal SPA ---
    n_a = len(soup.find_all("a", href=True)) if soup else 0
    n_script = len(soup.find_all("script")) if soup else 0
    root = soup.select_one("#root, #app, #__next, #__nuxt") if soup else None
    teks_body = soup.get_text(" ", strip=True) if soup else ""
    spa = bool(root) and n_a == 0 and len(teks_body) < 300

    katakan(f"\n[struktur] <a>={n_a}  <script>={n_script}  teks terlihat={len(teks_body)} karakter")
    if spa:
        katakan("★ Ini SPA (React/Vue/Next) — HTML-nya kerangka kosong, data diambil dari API.")
        katakan("  Scraping HTML gak akan dapat apa-apa. Harus nemu endpoint API-nya (di bawah).")
    elif n_a >= 3:
        katakan("→ Halaman HTML biasa, scraping langsung bisa jalan.")

    # --- link di HTML ---
    if soup and n_a:
        katakan(f"\n[link di HTML] ({n_a} total, maks 25 ditampilkan)")
        seen = set()
        for a in soup.find_all("a", href=True):
            u = urljoin(url, a["href"])
            if u in seen or u.startswith(("javascript:", "#", "mailto:")):
                continue
            seen.add(u)
            katakan(f"  {u[:95]:95s} | {a.get_text(' ', strip=True)[:35]}")
            if len(seen) >= 25:
                break

    # --- kumpulkan file JS ---
    js_files = []
    for sc in (soup.find_all("script", src=True) if soup else []):
        js_files.append(urljoin(url, sc["src"]))
    for m in JS_URL_RE.findall(teks):
        cand = urljoin(url, m if m.startswith("assets/") else m.lstrip("/"))
        if cand not in js_files:
            js_files.append(cand)
    js_files = list(dict.fromkeys(js_files))[: args.max_js]

    bundle = ""
    if js_files:
        katakan(f"\n[js] mengunduh {len(js_files)} file bundle...")
        diunduh = set()

        def ambil(daftar):
            nonlocal bundle
            for j in daftar:
                if j in diunduh or len(diunduh) >= args.max_js:
                    continue
                diunduh.add(j)
                try:
                    jr = sesi.get(j, headers={"User-Agent": args.user_agent},
                                  timeout=args.timeout, verify=not args.insecure)
                    if jr.status_code == 200:
                        bundle += jr.text + "\n"
                except requests.RequestException:
                    pass

        ambil(js_files)
        # pass 2: bundle SPA biasanya lazy-load chunk lain (mis. __vite__mapDeps)
        ekstra = [urljoin(url, m if m.startswith("assets/") else m.lstrip("/"))
                  for m in JS_URL_RE.findall(bundle)]
        baru = [e for e in dict.fromkeys(ekstra) if e not in diunduh]
        if baru:
            katakan(f"[js] +{min(len(baru), args.max_js - len(diunduh))} chunk lazy-load...")
            ambil(baru)
        katakan(f"     terkumpul {len(bundle):,} byte JavaScript dari {len(diunduh)} file")

    sumber = teks + "\n" + bundle
    temuan = {}

    def catat(kategori, nilai, catatan=""):
        nilai = nilai.strip().rstrip("'\"`,);")
        if not nilai or len(nilai) < 4:
            return
        if any(nz in nilai for nz in NOISE):
            return
        temuan.setdefault(kategori, {})
        if nilai not in temuan[kategori]:
            temuan[kategori][nilai] = catatan

    for m in SUPA_URL_RE.findall(sumber):
        catat("Supabase", f"https://{m}.supabase.co", "project")
    for m in FIREBASE_RE.findall(sumber):
        for x in m:
            if x:
                catat("Firebase", x, "project")
    for m in ABS_URL_RE.findall(sumber):
        if ".js" in m or ".css" in m or ".png" in m or ".jpg" in m or ".svg" in m or ".woff" in m:
            continue
        catat("URL eksternal", m)
    for m in API_PATH_RE.findall(sumber):
        catat("Path API", m)
    for m in SUPA_TABLE_RE.findall(sumber):
        catat("Tabel Supabase", m)
    for m in RPC_RE.findall(sumber):
        catat("Fungsi RPC", m)

    hasil = {
        "url": url, "soup": soup, "spa": spa, "n_link": n_a,
        "teks_body": teks_body, "temuan": temuan, "js_bytes": len(bundle),
    }
    if not temuan:
        katakan("\nGak ketemu endpoint API di HTML/JS. Kemungkinan:")
        katakan("  - URL API dibentuk dinamis (template string) -> butuh DevTools tab Network")
        katakan("  - atau datanya memang server-rendered")
        return hasil

    for kat in ("Supabase", "Firebase", "Path API", "URL eksternal", "Tabel Supabase", "Fungsi RPC"):
        if kat not in temuan:
            continue
        katakan(f"\n[{kat}]")
        for nilai, cat in list(temuan[kat].items())[:30]:
            katakan(f"  {nilai[:100]}" + (f"   ({cat})" if cat else ""))

    # --- saran perintah siap pakai ---
    katakan("\n" + "=" * 62)
    katakan("SARAN PERINTAH")
    katakan("=" * 62)
    if "Tabel Supabase" in temuan and "Supabase" in temuan:
        base = next(iter(temuan["Supabase"]))
        for tb in list(temuan["Tabel Supabase"])[:3]:
            katakan(f"\n# Supabase/PostgREST — tabel '{tb}'")
            katakan(f"python scrape.py -b {base}/rest/v1 -e /{tb} \\")
            katakan(f"    -H 'apikey: ANON_KEY' -H 'Authorization: Bearer ANON_KEY' \\")
            katakan(f"    --param select=* -o {tb}.csv")
    if "Path API" in temuan:
        pr = urlparse(url)
        for pa in list(temuan["Path API"])[:3]:
            katakan(f"\npython scrape.py -b {pr.scheme}://{pr.netloc} -e {pa} --inspect")
    if "URL eksternal" in temuan:
        for ue in list(temuan["URL eksternal"])[:3]:
            katakan(f"\npython scrape.py {ue} --inspect")
    katakan("\nTip: selalu akhiri dengan --inspect buat lihat struktur datanya sebelum scrape.")
    return hasil


def discover(url: str, sesi: requests.Session, args):
    """Mode --discover: lacak endpoint API dari sebuah halaman."""
    print(f"\n=== DISCOVER: {url} ===")
    try:
        r = sesi.get(url, headers={"User-Agent": args.user_agent}, timeout=args.timeout,
                     verify=not args.insecure)
    except requests.RequestException as e:
        print(f"Gagal menghubungi {url}: {e}")
        return 1
    print(f"HTTP {r.status_code} | {r.headers.get('Content-Type','-')} | {len(r.text):,} byte")
    if not (200 <= r.status_code < 300):
        print("Gagal ambil halaman. Cek URL / butuh login / diblokir.")
        return 1
    try:
        json.loads(r.text)
        print("\nHalaman ini langsung balas JSON — gak perlu discover.")
        print(f"Langsung scrape:\n  python scrape {url} -o hasil.csv")
        return 0
    except Exception:
        pass
    analisis_situs(url, r.text, sesi, args, cetak=print)
    return 0


# --------------------------------------------------------------------------- #
# MODE OTOMATIS PENUH — user cuma kasih URL, tool yang mikir
# --------------------------------------------------------------------------- #

def uji_kandidat(u: str, sesi: requests.Session, args):
    """Coba satu URL: kalau balas JSON berisi list objek, kembalikan info + skornya."""
    try:
        r = sesi.get(u, headers={"User-Agent": args.user_agent}, timeout=args.timeout,
                     verify=not args.insecure)
    except requests.RequestException:
        return None
    if r.status_code != 200:
        return None
    try:
        payload = json.loads(r.text)
    except Exception:
        return None
    path, n = auto_items_path(payload)
    if n < 2:
        return None
    return {"url": u, "items": path, "n": n, "payload": payload,
            "text": r.text, "status": r.status_code}


NEXT_KEYS = ("next", "next_page", "nextPage", "pagination.next", "meta.next",
             "links.next", "data.next", "paging.next", "next_url", "nextUrl")


def ikuti_paginasi(u, sesi, args, items_path, halaman_pertama, payload_pertama):
    """Kumpulkan semua halaman dari satu endpoint. Balikin (semua_items, url_terakhir)."""
    semua = list(halaman_pertama)
    cur = u
    for hal in range(1, max(1, args.max_pages)):
        nxt = None
        for k in NEXT_KEYS:
            v = get_path(payload_pertama if hal == 1 else payload, k)
            if isinstance(v, str) and v.strip():
                nxt = urljoin(cur, v)
                break
        if not nxt:
            pr = urlparse(cur)
            q = dict(parse_qsl(pr.query))
            kunci = next((k for k in ("page", "p", "halaman", "pageNum", "offset") if k in q), "page")
            if kunci == "offset":
                q["offset"] = str(int(q.get("offset", 0) or 0) + len(halaman_pertama))
            else:
                q[kunci] = str(int(q.get(kunci, 1) or 1) + 1)
            nxt = urlunparse(pr._replace(query=urlencode(q)))
        if not nxt or nxt == cur:
            break
        try:
            r = sesi.get(nxt, headers={"User-Agent": args.user_agent},
                         timeout=args.timeout, verify=not args.insecure)
        except requests.RequestException:
            break
        if r.status_code != 200:
            break
        try:
            payload = json.loads(r.text)
        except Exception:
            break
        baru = get_path(payload, items_path) if items_path else payload
        if not isinstance(baru, list) or not baru:
            break
        if baru == halaman_pertama:            # mentok, isinya diulang
            break
        semua += baru
        cur = nxt
        time.sleep(0.2)
    else:
        return semua, cur
    return semua, cur


def kandidat_api(info: dict, url_halaman: str) -> list:
    """Susun daftar URL API yang layak dicoba dari hasil analisis."""
    asal = urlparse(url_halaman)
    base_asal = f"{asal.scheme}://{asal.netloc}"
    temuan = info.get("temuan", {})
    out = []

    def tambah(u, prioritas):
        u = u.strip().rstrip("/")
        if u and u not in [x[1] for x in out]:
            out.append((prioritas, u))

    # 1) path API di origin yang sama  -> paling mungkin benar
    for pa in temuan.get("Path API", {}):
        tambah(urljoin(base_asal + "/", pa.lstrip("/")), 0)
    # 2) host API eksternal yang ketemu di bundle
    for ue in temuan.get("URL eksternal", {}):
        if any(x in ue for x in (".js", ".css", ".png", ".jpg", ".svg", ".woff", "localhost")):
            continue
        tambah(ue, 1)
        for pa in temuan.get("Path API", {}):
            tambah(urljoin(ue.rstrip("/") + "/", pa.lstrip("/")), 2)
    # 3) Supabase REST
    for sb in temuan.get("Supabase", {}):
        for tb in temuan.get("Tabel Supabase", {}):
            tambah(f"{sb.rstrip('/')}/rest/v1/{tb}", 3)

    out.sort(key=lambda x: x[0])
    return [u for _, u in out]


def nama_file_otomatis(url: str) -> str:
    pr = urlparse(url)
    host = pr.netloc.replace(":", "_")
    jalur = "_".join([x for x in pr.path.split("/") if x][-2:]) or "index"
    nama = f"{host}_{jalur}".replace("..", ".")
    return re.sub(r"[^A-Za-z0-9._-]+", "_", nama)[:70] + ".csv"


def crawl_dalam(url_awal: str, sesi: requests.Session, args):
    """Telusuri link internal sampai kedalaman --dalam, ekstrak tiap halaman."""
    asal = urlparse(url_awal).netloc
    antre = [(url_awal, 0)]
    dilihat = set()
    semua = []
    batas = 50
    print(f"→ crawl sampai kedalaman {args.dalam} dari {url_awal}")
    while antre and len(dilihat) < batas:
        u, dalam = antre.pop(0)
        u = u.split("#")[0]
        if u in dilihat:
            continue
        dilihat.add(u)
        try:
            r = sesi.get(u, headers={"User-Agent": args.user_agent},
                         timeout=args.timeout, verify=not args.insecure)
        except requests.RequestException as e:
            print(f"  · {u[:70]} -> gagal ({e})")
            continue
        if r.status_code != 200:
            print(f"  · {u[:70]} -> HTTP {r.status_code}")
            continue
        r.encoding = args.encoding or r.apparent_encoding or "utf-8"
        print(f"  [{dalam}] {u[:74]}")

        rows = []
        try:
            payload = json.loads(r.text)
            ap_, _ = auto_items_path(payload)
            items = get_path(payload, ap_) if ap_ else payload
            if not isinstance(items, list):
                items = [items]
            rows = [flatten(x) if isinstance(x, (dict, list)) else {"value": x} for x in items]
        except Exception:
            try:
                from bs4 import BeautifulSoup
                try:
                    soup = BeautifulSoup(r.text, "lxml")
                except Exception:
                    soup = BeautifulSoup(r.text, "html.parser")
                sel, _ = auto_item_selector(soup)
                if sel:
                    fs = auto_html_fields(soup, sel)
                    args.item, args.field = sel, [f"{f.name}={f.loc}@{f.attr}" for f in fs]
                    rows = extract_html(soup, r.url, args)
                    args.item, args.field = None, []
                # kumpulkan link internal
                if dalam < args.dalam:
                    for a in soup.find_all("a", href=True):
                        nxt = urljoin(r.url, a["href"]).split("#")[0]
                        if urlparse(nxt).netloc == asal and nxt not in dilihat \
                                and not re.search(r"\.(jpg|jpeg|png|gif|svg|css|js|pdf|zip)$", nxt, re.I):
                            antre.append((nxt, dalam + 1))
            except ImportError:
                pass

        rows = [x for x in rows if any(str(v).strip() for v in x.values())]
        for row in rows:
            row["_url"], row["_depth"] = r.url, dalam
        print(f"      +{len(rows)} record")
        semua += rows
        time.sleep(args.delay)
    return semua


def pipeline_otomatis(url: str, sesi: requests.Session, args):
    """
    Ambil URL, putuskan sendiri cara scrape-nya:
      1. JSON langsung?          -> ekstrak
      2. HTML ada isinya?        -> ekstrak
      3. HTML kerangka (SPA)?    -> bedah bundle JS, cari endpoint API, uji satu-satu,
                                    pakai yang paling banyak datanya
    Kembalikan list[dict] record + info sumber.
    """
    print(f"→ {url}")
    try:
        r = sesi.get(url, headers={"User-Agent": args.user_agent}, timeout=args.timeout,
                     verify=not args.insecure)
    except requests.RequestException as e:
        print(f"✗ gak bisa hubungi: {e}")
        return [], None
    print(f"  HTTP {r.status_code} | {len(r.text):,} byte | {r.headers.get('Content-Type','-')}")
    if not (200 <= r.status_code < 300):
        print("✗ respons bukan 2xx.")
        return [], None
    r.encoding = args.encoding or r.apparent_encoding or "utf-8"

    # --- 1) JSON ---
    try:
        payload = json.loads(r.text)
        ap_, n_ = auto_items_path(payload)
        items = get_path(payload, ap_) if ap_ else payload
        if not isinstance(items, list):
            items = [items]
        if args.max_pages > 1:
            items, akhir = ikuti_paginasi(r.url, sesi, args, ap_, items, payload)
            if akhir != r.url:
                print(f"  → pagination diikuti sampai {akhir[:70]}")
        args.items = ap_
        rows = [flatten(x) if isinstance(x, (dict, list)) else {"value": x} for x in items]
        print(f"  ✓ JSON — items '{ap_ or '(root)'}' → {len(rows)} record")
        return rows, {"url": r.url, "items": ap_, "label": nama_file_otomatis(r.url)}
    except Exception:
        pass

    # --- 2) HTML ---
    try:
        from bs4 import BeautifulSoup
        try:
            soup = BeautifulSoup(r.text, "lxml")
        except Exception:
            soup = BeautifulSoup(r.text, "html.parser")
    except ImportError:
        print("✗ butuh bs4:  pip install beautifulsoup4 lxml")
        return [], None

    sel, n_ = auto_item_selector(soup)
    if sel:
        args.item = sel
        fs = auto_html_fields(soup, sel)
        args.field = [f"{f.name}={f.loc}@{f.attr}" for f in fs]
        rows = extract_html(soup, r.url, args)
        rows = [x for x in rows if any(str(v).strip() for v in x.values())]
        if rows:
            print(f"  ✓ HTML — pakai item '{sel}' ({n_} elemen) → {len(rows)} record")
            return rows, {"url": r.url, "item": sel}

    # --- 3) SPA: bedah bundle, cari API ---
    print("  ⚠ HTML-nya kerangka kosong (SPA) — datanya diambil lewat API.")
    print("  → membedah bundle JavaScript buat cari endpoint...")
    info = analisis_situs(r.url, r.text, sesi, args, cetak=None)
    print(f"    {info['js_bytes']:,} byte JS dianalisis")
    daftar = kandidat_api(info, r.url)
    if not daftar:
        print("  ✗ gak ketemu kandidat endpoint API di bundle-nya.")
        return [], info

    print(f"  → {len(daftar[:args.coba])} kandidat endpoint, diuji satu-satu:")
    hidup = []
    for u in daftar[: args.coba]:
        hasil = uji_kandidat(u, sesi, args)
        if hasil:
            # ikuti pagination kalau diminta
            if args.max_pages > 1:
                items, akhir = ikuti_paginasi(u, sesi, args, hasil["items"],
                                              get_path(hasil["payload"], hasil["items"]),
                                              hasil["payload"])
                hasil["n"] = len(items)
                hasil["_items"] = items
                hasil["_pag"] = (akhir != u)
            print(f"    ✓ {hasil['n']:>4} record  {u[:78]}"
                  + ("   ← pagination diikuti" if hasil.get("_pag") else ""))
            hidup.append(hasil)
        else:
            print(f"    · lewat        {u[:78]}")
        time.sleep(0.2)

    if not hidup:
        print("  ✗ gak ada endpoint yang balikin data list.")
        return [], info

    if not args.all:
        hidup.sort(key=lambda x: -x["n"])
        print(f"  ★ paling banyak: {hidup[0]['url']}")
        print(f"    (mau SEMUA endpoint? tambah  --all )")
        t = hidup[0]
        items = t.get("_items") or get_path(t["payload"], t["items"])
        rows = [flatten(x) if isinstance(x, (dict, list)) else {"value": x} for x in items]
        return rows, {"url": t["url"], "items": t["items"], "label": nama_file_otomatis(t["url"])}

    # --all: kembalikan semuanya, main() yang pisah-pisahin file
    print(f"  ★ --all: {len(hidup)} endpoint dipakai semua")
    gabungan = []
    for t in hidup:
        items = t.get("_items") or get_path(t["payload"], t["items"])
        rows = [flatten(x) if isinstance(x, (dict, list)) else {"value": x} for x in items]
        for row in rows:
            row["_endpoint"] = t["url"]
        gabungan.append((nama_file_otomatis(t["url"]), rows, t["url"]))
    return gabungan, info


def inspect(resp: dict, args):
    print(f"\n=== INSPEKSI ===")
    print(f"URL final : {resp['url']}")
    print(f"Status    : {resp['status']}   ({resp['elapsed']:.2f}s)")
    print(f"Content   : {resp['ctype']}  |  {len(resp['text']):,} byte")

    body = None
    try:
        body = json.loads(resp["text"])
    except Exception:
        pass

    if body is not None:
        print("\n-> Respons JSON.")
        print("   Struktur (dot-path yang bisa dipakai buat --items / --field):")
        for k, v in list(flatten(body).items())[:80]:
            vs = str(v)
            print(f"     {k}  =  {vs[:60]}{'…' if len(vs) > 60 else ''}")
        # tebak daftar item
        for path in ("", "data", "result", "results", "items", "records", "rows", "hits",
                     "data.items", "data.results", "data.data", "data.list", "content", "feed"):
            node = get_path(body, path) if path else body
            if isinstance(node, list) and node and isinstance(node[0], dict):
                label = path or "(root)"
                print(f"\n   Kandidat --items: '{label}'  ({len(node)} objek)")
                print(f"   Contoh field: " + ", ".join(list(node[0].keys())[:12]))
                break
        # tebak pagination
        for key in ("next", "next_page", "nextPage", "pagination.next", "meta.next",
                    "data.next", "links.next", "next_cursor", "cursor"):
            v = get_path(body, key)
            if v:
                print(f"   Kandidat --next  : '{key}' = {str(v)[:70]}")
                break
        print("\n   Coba:  --as json --items '<kandidat>' --field 'nama=<path>'")
        return

    print("\n-> Respons HTML/teks (bukan JSON).")
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp["text"], "lxml")
    except ImportError:
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(resp["text"], "html.parser")
        except ImportError:
            print("   (bs4 belum dipasang: pip install beautifulsoup4 lxml)")
            return
    except Exception:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(resp["text"], "html.parser")

    judul = soup.title.get_text(strip=True) if soup.title else "-"
    print(f"   <title>: {judul}")

    hitung = {}
    for el in soup.find_all(True):
        key = el.name
        cls = el.get("class")
        if cls:
            key += "." + ".".join(cls[:2])
        if el.get("id"):
            key += "#" + el["id"]
        hitung[key] = hitung.get(key, 0) + 1

    # kandidat per kelas (paling kepake)
    per_kelas: dict = {}
    for el in soup.find_all(class_=True):
        for c in el.get("class"):
            key = f"{el.name}.{c}"
            per_kelas.setdefault(key, []).append(el)
    print("   Selector yang muncul berulang (kandidat --item):")
    ada = False
    for k, els in sorted(per_kelas.items(), key=lambda x: -len(x[1]))[:15]:
        if len(els) >= 2:
            ada = True
            anak = ""
            teks = els[0].get_text(" ", strip=True)[:50]
            if teks:
                anak = f'   -> teks contoh: "{teks}"'
            print(f"     {len(els):>4}x  {k}{anak}")
    if not ada:
        for k, n in sorted(hitung.items(), key=lambda x: -x[1])[:15]:
            if n >= 2 and re.search(r"\.|#", k):
                ada = True
                print(f"     {n:>4}x  {k}")
    if not ada:
        print("     (gak ketemu; coba --item 'tbody tr' atau lihat struktur manual)")

    # JSON tersembunyi di dalam HTML — ini sering jadi jalan pintas terbaik
    pola = [
        (r'<script[^>]+id="__NEXT_DATA__"[^>]*>', "Next.js (__NEXT_DATA__)"),
        (r'<script[^>]+type="application/ld\+json"[^>]*>', "JSON-LD (schema.org)"),
        (r'window\.__INITIAL_STATE__\s*=', "window.__INITIAL_STATE__"),
        (r'window\.__NUXT__\s*=', "window.__NUXT__"),
        (r'<script[^>]+data-fragment-type', "fragment JSON"),
    ]
    for regex, label in pola:
        if re.search(regex, resp["text"]):
            print(f"\n   ★ Ada data JSON tersembunyi di HTML: {label}")
            print("     Biasanya lebih rapi diambil daripada parse DOM. Ambil dulu mentahnya:")
            print("       --raw-response -o page.html  lalu ekstrak blok JSON-nya.")
            break

    tabel = soup.find_all("table")
    if tabel:
        print(f"\n   Ada {len(tabel)} <table>. Contoh cepat:")
        print("     --field 'k1=td:nth-of-type(1)@text' --field 'k2=td:nth-of-type(2)@text' --item 'tbody tr'")

    if soup.select_one("a.next, li.next a, .pagination a[rel='next']"):
        print("   Kandidat --next (HTML): 'li.next a' / 'a.next'")
    print("\n   Coba:  --as html --item '<selector berulang>' --field 'nama=<selector anak>@text'")


# --------------------------------------------------------------------------- #
# output
# --------------------------------------------------------------------------- #

def write_output(records: list[dict], pages: list[dict], args):
    kolom = []
    for r in records:
        for k in r.keys():
            if k not in kolom:
                kolom.append(k)

    if args.out in ("-", None) and args.format == "raw":
        sys.stdout.write("\n".join(p["text"] for p in pages))
        return

    if args.out and args.out != "-":
        f = open(args.out, "w", encoding="utf-8", newline="")
    else:
        f = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", newline="")

    try:
        if args.format == "csv":
            if not records:
                log("Gak ada record buat ditulis.")
                return
            w = csv.DictWriter(f, fieldnames=kolom, extrasaction="ignore")
            w.writeheader()
            w.writerows(records)
        elif args.format == "json":
            # --raw-response + 1 halaman = tulis respons itu apa adanya (bukan dibungkus list)
            if args.raw_response and len(records) == 1:
                payload = records[0]
            elif args.wrap:
                payload = {"count": len(records), "data": records}
            else:
                payload = records
            json.dump(payload, f, ensure_ascii=False, indent=2)
            f.write("\n")
        elif args.format == "ndjson":
            for r in records:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")
        elif args.format == "raw":
            f.write("\n\n".join(p["text"] for p in pages))
        elif args.format == "html":
            f.write("\n\n<!-- ==== PAGE BREAK ==== -->\n\n".join(p["text"] for p in pages))
    finally:
        if args.out and args.out != "-":
            f.close()


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #

def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="scrape.py",
        description="Scraper generik: base-url + endpoint -> CSV/JSON/NDJSON.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
CONTOH
  # 1) Inspeksi dulu (nemu path/selector otomatis)
  python scrape.py --base-url https://api.contoh.com --endpoint /v1/products --inspect

  # 2) JSON API + pagination + pilih field -> CSV
  python scrape.py --base-url https://api.contoh.com --endpoint /v1/products \\
      --param page=1 --items data.products --pages 5 --next-param page \\
      --field id=id --field nama=name --field harga=price.value --out produk.csv

  # 3) Simpan respons JSON mentah apa adanya
  python scrape.py -b https://api.contoh.com -e /v1/products -o mentah.json --format json --raw-response

  # 4) HTML: ambil tabel
  python scrape.py -b https://situs.com -e /daftar --as html \\
      --item "tbody tr" --field "no=td:nth-of-type(1)@text" --field "nama=td:nth-of-type(2)@text"

  # 5) HTML: kartu produk + pagination lewat tombol next
  python scrape.py -b https://situs.com -e /kategori/sepatu --as html \\
      --item "div.product-card" --field "judul=h3 a@title" --field "harga=.price@text" \\
      --field "link=h3 a@href" --next "li.next a" --pages 10 --delay 2

  # 6) Login dulu (simpan cookie), baru scrape endpoint privat
  python scrape.py -b https://situs.com -e /api/me --login-endpoint /login \\
      --login-data "username=saya" --login-data "password=rahasia" \\
      --cookies sesi.json --format json

  # 7) Banyak endpoint sekaligus (mis. shard per halaman)
  python scrape.py -b https://situs.com -e /page/1 -e /page/2 -e /page/3 --as html --item "..." ...
""")
    p.add_argument("--version", action="version", version=f"scrape.py {__version__}")
    p.add_argument("-c", "--config", help="file JSON berisi default argumen (CLI tetap menang)")
    p.add_argument("-n", "--dry-run", action="store_true",
                   help="cuma cetak URL yang bakal di-request, gak kirim apa-apa")
    p.add_argument("target", nargs="*",
                   help="URL / endpoint langsung, tanpa perlu -b/-e. Contoh: python scrape.py https://situs.com")
    p.add_argument("-b", "--base-url", help="contoh: https://api.contoh.com")
    p.add_argument("-e", "--endpoint", action="append", default=[],
                   help="path/endpoint; boleh dipakai berulang. Boleh URL absolut.")
    p.add_argument("-X", "--method", default="GET", help="GET/POST/PUT/... (default GET)")
    p.add_argument("-P", "--param", action="append", default=[], help="query param: nama=nilai (boleh berulang)")
    p.add_argument("-H", "--header", action="append", default=[], help="header: 'Nama: Nilai' (boleh berulang)")
    p.add_argument("-d", "--data", action="append", default=[], help="body form: nama=nilai (boleh berulang)")
    p.add_argument("--json", dest="json_body", help="body JSON mentah (string) atau @file.json")
    p.add_argument("--auth", help="Basic auth: user:password")
    p.add_argument("--proxy", help="http://user:pass@host:port")
    p.add_argument("--cookies", help="file cookie JSON (baca kalau ada, simpan kalau --save-cookies)")
    p.add_argument("--save-cookies", action="store_true", help="simpan cookie setelah selesai")
    p.add_argument("--login-endpoint", help="endpoint login (dipanggil duluan, sebelum scrape)")
    p.add_argument("--login-data", action="append", default=[], help="data login: nama=nilai (boleh berulang)")
    p.add_argument("--login-json", help="body login JSON mentah / @file.json")

    g = p.add_argument_group("parsing")
    g.add_argument("--as", dest="as_mode", choices=["auto", "json", "html"], default="auto",
                   help="paksa mode parse (default auto dari Content-Type)")
    g.add_argument("--items", help="JSON: dot-path ke list item, mis. data.products")
    g.add_argument("--item", help="HTML: selector CSS item berulang, mis. 'div.product'")
    g.add_argument("-f", "--field", action="append", default=[],
                   help="kolom output: nama=path (JSON) atau nama=selector@attr (HTML). Boleh berulang.")
    g.add_argument("--sep", default=" ", help="pemisah get_text() di HTML (default spasi)")

    g2 = p.add_argument_group("pagination")
    g2.add_argument("--pages", type=int, default=1, help="jumlah halaman per endpoint (default 1)")
    g2.add_argument("--next", help="JSON: dot-path ke URL/kursor berikutnya; HTML: selector link next")
    g2.add_argument("--next-param", help="nama param halaman yg dinaikkan otomatis: page, p, offset...")
    g2.add_argument("--start-page", type=int, default=1, help="nomor halaman awal (default 1)")
    g2.add_argument("--limit", type=int, help="berhenti setelah N record terkumpul")
    g2.add_argument("--stop-on-empty", action="store_true", default=True,
                    help="berhenti kalau satu halaman gak menghasilkan record (default aktif)")
    g2.add_argument("--no-stop-on-empty", dest="stop_on_empty", action="store_false")

    g3 = p.add_argument_group("output")
    g3.add_argument("-o", "--out", default="-", help="file output; '-' = stdout (default)")
    g3.add_argument("--format", choices=["csv", "json", "ndjson", "raw", "html"], default="csv")
    g3.add_argument("--raw-response", action="store_true",
                    help="jangan ekstrak field — simpan respons mentah (auto-set --format sesuai mode)")
    g3.add_argument("--wrap", action="store_true", help="JSON: bungkus jadi {count, data}")
    g3.add_argument("--meta", action="store_true", help="tambahkan kolom _url & _status per record")
    g3.add_argument("--encoding", help="paksa encoding respons (mis. utf-8)")

    g4 = p.add_argument_group("perilaku")
    g4.add_argument("--delay", type=float, default=1.0, help="jeda antar request, detik (default 1)")
    g4.add_argument("--jitter", type=float, default=0.5, help="tambahan acak 0..jitter (default 0.5)")
    g4.add_argument("--timeout", type=float, default=20.0)
    g4.add_argument("--retry", type=int, default=3)
    g4.add_argument("-A", "--user-agent",
                   default=f"scrape.py/{__version__} (+script pribadi; hormati robots.txt)")
    g4.add_argument("--insecure", "-k", action="store_true", help="abaikan error sertifikat SSL")
    g4.add_argument("--no-redirect", action="store_true")
    g4.add_argument("--allow-status", action="store_true", help="tetap proses walau HTTP != 2xx")
    g4.add_argument("--discover", action="store_true",
                    help="lacak endpoint API otomatis: scan HTML + semua file JS, "
                         "cari URL API, tabel Supabase/Firebase, path /api/, graphql")
    g4.add_argument("--all", action="store_true",
                    help="mode otomatis: scrape SEMUA endpoint yang ada datanya, "
                         "masing-masing jadi file CSV sendiri (bukan cuma yang terbaik)")
    g4.add_argument("--max-pages", type=int, default=10,
                    help="mode otomatis: ikuti pagination tiap endpoint sampai habis, maks N halaman (default 10)")
    g4.add_argument("--dalam", type=int, default=0,
                    help="crawl link internal sampai kedalaman N (default 0 = halaman itu saja)")
    g4.add_argument("--coba", type=int, default=12,
                    help="mode otomatis: jumlah kandidat endpoint yang diuji (default 12)")
    g4.add_argument("--max-js", type=int, default=25, help="--discover: maks file JS yang diunduh (default 25)")
    g4.add_argument("-i", "--inspect", action="store_true",
                    help="mode inspeksi: tampilkan struktur respons + saran --items/--field, lalu keluar")
    g4.add_argument("-v", "--verbose", action="store_true")
    g4.add_argument("-q", "--quiet", action="store_true", help="diamkan log progres")
    return p


def load_config(argv):
    """--config x.json : isi default argumen dari file. Argumen CLI tetap menang."""
    argv = list(sys.argv[1:] if argv is None else argv)
    path = None
    for i, a in enumerate(argv):
        if a == "-c" or a == "--config":
            if i + 1 < len(argv):
                path = argv[i + 1]
        elif a.startswith("--config="):
            path = a.split("=", 1)[1]
    if not path:
        return argv
    with open(path, encoding="utf-8") as f:
        cfg = json.load(f)
    pre = []
    for k, v in cfg.items():
        k = str(k).strip()
        if k.startswith("_") or k.startswith("#"):
            continue                      # key diawali _ atau # = komentar, diabaikan
        flag = "--" + k.replace("_", "-")
        if isinstance(v, bool):
            if v:
                pre.append(flag)
        elif isinstance(v, (list, tuple)):
            for item in v:
                pre += [flag, str(item)]
        else:
            pre += [flag, str(v)]
    return pre + argv          # argv asli di belakang => menang


def main(argv=None) -> int:
    global ARGS
    args = build_parser().parse_args(load_config(argv))
    ARGS = args

    # positional target: python scrape.py https://situs.com  ==> base + endpoint otomatis
    for t in args.target:
        t = t.strip()
        if not t:
            continue
        if t.startswith("http://") or t.startswith("https://"):
            if not args.base_url:
                pr = urlparse(t)
                args.base_url = f"{pr.scheme}://{pr.netloc}"
                ep = pr.path or "/"
                if pr.query:
                    ep += "?" + pr.query
                args.endpoint.append(ep)
            else:
                args.endpoint.append(t)      # sudah ada base -> anggap endpoint/URL lengkap
        else:
            args.endpoint.append(t)

    if not args.endpoint:
        if sys.stdin.isatty() and not (args.inspect or args.discover or args.dry_run):
            try:
                jawab = input("URL yang mau di-scrape: ").strip().strip("'\"")
            except (EOFError, KeyboardInterrupt):
                print()
                sys.exit(1)
            if not jawab:
                sys.exit("Gak ada URL yang dikasih. Keluar.")
            if not jawab.startswith("http"):
                jawab = "https://" + jawab
            pr = urlparse(jawab)
            args.base_url = f"{pr.scheme}://{pr.netloc}"
            args.endpoint.append((pr.path or "/") + (("?" + pr.query) if pr.query else ""))
            args._interaktif = True
        else:
            sys.exit("Butuh target. Contoh:  python scrape https://situs.com\n"
                     "          atau:  python scrape -b https://api.situs.com -e /v1/items\n"
                     "          atau jalankan tanpa argumen buat mode tanya-jawab.")
    if args.inspect and not args.base_url:
        sys.exit("--inspect butuh --base-url")

    args.headers = parse_headers(args.header)
    args.params = parse_params(args.param)
    args.data = parse_params(args.data) if args.data else None
    args.login_data = parse_params(args.login_data) if args.login_data else None

    jb = args.json_body
    if jb and jb.startswith("@"):
        with open(jb[1:], encoding="utf-8") as f:
            jb = f.read()
    args.json_body_parsed = json.loads(jb) if jb else None

    ljb = args.login_json
    if ljb and ljb.startswith("@"):
        with open(ljb[1:], encoding="utf-8") as f:
            ljb = f.read()
    args.login_json_parsed = json.loads(ljb) if ljb else None

    if args.raw_response:
        if args.as_mode == "html":
            args.format = "html"
        elif args.format == "csv":
            args.format = "json"
        if args.out == "-" and args.format == "html":
            args.format = "raw"

    sesi = build_session(args)

    # --- MODE OTOMATIS PENUH ---
    # user cuma kasih URL, gak ngasih --items/--field/--item sama sekali
    mode_auto = not (args.field or args.items or args.item or args.raw_response
                     or args.inspect or args.discover or args.dry_run
                     or args.next or args.next_param or args.pages > 1)
    if mode_auto:
        sesi_a = build_session(args)
        if args.login_endpoint:
            lurl = join_url(args.base_url, args.login_endpoint)
            log(f"login -> {lurl}")
            if fetch(lurl, sesi_a, args, method="POST", data=args.login_data,
                     json_body=args.login_json_parsed, percobaan=2) is None:
                log("login gagal — lanjut tanpa sesi.")
        def tulis_csv(nama, rows):
            kolom = []
            for row in rows:
                for k in row:
                    if k not in kolom:
                        kolom.append(k)
            with open(nama, "w", encoding="utf-8", newline="") as fh:
                w = csv.DictWriter(fh, fieldnames=kolom, extrasaction="ignore")
                w.writeheader()
                w.writerows(rows)
            return len(kolom)

        berkas = []          # [(nama_file, jumlah_record, jumlah_kolom, sumber)]

        for ep in args.endpoint:
            full = join_url(args.base_url, ep)

            # --- crawl link internal ---
            if args.dalam > 0:
                rows = crawl_dalam(full, sesi_a, args)
                if rows:
                    nama = args.out if args.out != "-" else nama_file_otomatis(full).replace(".csv", "_crawl.csv")
                    n = tulis_csv(nama, rows)
                    berkas.append((nama, len(rows), n, full))
                continue

            hasil, info = pipeline_otomatis(full, sesi_a, args)
            args.items, args.field, args.item = "", [], None

            if not hasil:
                continue

            # --all: hasil berupa list (nama, rows, url)
            if isinstance(hasil, list) and hasil and isinstance(hasil[0], tuple):
                for nama, rows, src in hasil:
                    if not rows:
                        continue
                    tujuan = nama if args.out == "-" else f"{args.out}-{nama}"
                    n = tulis_csv(tujuan, rows)
                    berkas.append((tujuan, len(rows), n, src))
            else:
                rows = hasil
                src = (info or {}).get("url", full)
                tujuan = args.out if args.out != "-" else (info or {}).get("label") or nama_file_otomatis(src)
                n = tulis_csv(tujuan, rows)
                berkas.append((tujuan, len(rows), n, src))

        if args.save_cookies and args.cookies:
            save_cookies(sesi_a, args.cookies)
        if not berkas:
            print("\n✗ Gak dapat data. Coba jalankan dengan --discover buat lihat detailnya.",
                  file=sys.stderr)
            return 1

        total = sum(b[1] for b in berkas)
        print(f"\n{'=' * 60}")
        for nama, jr, nk, src in berkas:
            print(f"✔ {jr:>5} record × {nk:>2} kolom  →  {nama}")
        print(f"{'=' * 60}")
        print(f"  total {total} record dari {len(berkas)} sumber")
        if len(berkas) == 1:
            print(f"  sumber: {berkas[0][3]}")
        return 0

    # --- discover: lacak endpoint API ---
    if args.discover:
        target = args.endpoint[0]
        full = join_url(args.base_url, target)
        sesi_d = build_session(args)
        return discover(full, sesi_d, args)

    # --- dry-run: cetak rencana request, jangan kirim apa-apa ---
    if args.dry_run:
        if args.login_endpoint:
            lurl = join_url(args.base_url, args.login_endpoint)
            print(f"POST {lurl}")
            if args.login_data:
                print(f"       form: {args.login_data}")
            if args.login_json_parsed is not None:
                print(f"       json: {json.dumps(args.login_json_parsed, ensure_ascii=False)}")
        for ep in args.endpoint:
            base = join_url(args.base_url, ep)
            for halaman in range(args.pages):
                params = dict(args.params)
                if args.next_param and halaman > 0:
                    params[args.next_param] = args.start_page + halaman
                print(f"{args.method} {merge_url(base, params)}")
        if args.data:
            print(f"       body form: {args.data}")
        if args.json_body_parsed is not None:
            print(f"       body json: {json.dumps(args.json_body_parsed, ensure_ascii=False)}")
        for k, v in {**{"User-Agent": args.user_agent}, **(args.headers or {})}.items():
            print(f"       header   : {k}: {v}")
        print(f"\n(total maks. {len(args.endpoint) * args.pages} request; jeda {args.delay}s + jitter {args.jitter}s)")
        return 0

    # --- login dulu kalau diminta ---
    if args.login_endpoint:
        if not args.base_url:
            sys.exit("--login-endpoint butuh --base-url")
        lurl = join_url(args.base_url, args.login_endpoint)
        log(f"login -> {lurl}")
        lr = fetch(lurl, sesi, args, method="POST", data=args.login_data,
                   json_body=args.login_json_parsed, percobaan=2)
        if lr is None:
            log("login gagal — lanjut scrape tanpa sesi login.")
        else:
            log(f"login OK (HTTP {lr['status']})")

    records: list[dict] = []
    pages: list[dict] = []
    total_req = 0

    for ep in args.endpoint:
        ep = ep.strip()
        url = join_url(args.base_url, ep)

        log(f"\n▶ endpoint: {url}")
        cur = url
        for halaman in range(args.pages):
            if args.limit and len(records) >= args.limit:
                log(f"limit {args.limit} tercapai, berhenti.")
                break
            # param halaman otomatis
            params = dict(args.params)
            if args.next_param and halaman > 0 and not (args.next and args.as_mode == "html"):
                params[args.next_param] = args.start_page + halaman

            url_req = merge_url(cur, params)
            log(f"[{halaman + 1}/{args.pages}] {url_req}")
            resp = fetch(cur, sesi, args, params=params if params else None,
                         data=args.data, json_body=args.json_body_parsed)
            total_req += 1
            if resp is None:
                log("  berhenti: request gagal.")
                break
            pages.append(resp)

            if args.inspect:
                inspect(resp, args)
                if args.save_cookies and args.cookies:
                    save_cookies(sesi, args.cookies)
                return 0

            # tentukan mode
            mode = args.as_mode
            if mode == "auto":
                try:
                    json.loads(resp["text"])
                    mode = "json"
                except Exception:
                    mode = "html" if ("html" in resp["ctype"].lower() or resp["text"].lstrip()[:1] == "<") else "json"

            if mode == "json":
                try:
                    payload = json.loads(resp["text"])
                except Exception as e:
                    log(f"  bukan JSON valid: {e}")
                    break
                if args.raw_response:
                    records.append(payload)          # respons mentah apa adanya
                    got = 1
                else:
                    if not args.items and not args.field:
                        ap_, n_ = auto_items_path(payload)
                        if ap_:
                            args.items = ap_
                            log(f"  auto: pakai --items '{ap_}' ({n_} objek)")
                        else:
                            log("  auto: gak ada list di respons -> diratakan apa adanya")
                    got_rows = extract_json(payload, args)
                    for row in got_rows:
                        if args.meta:
                            row["_url"], row["_status"] = resp["url"], resp["status"]
                        records.append(row)
                    got = len(got_rows)
                nxt = find_next(payload, resp["url"], args)
            else:
                try:
                    from bs4 import BeautifulSoup
                except ImportError:
                    sys.exit("Parse HTML butuh bs4:  pip install beautifulsoup4 lxml")
                try:
                    soup = BeautifulSoup(resp["text"], "lxml")
                except Exception:
                    soup = BeautifulSoup(resp["text"], "html.parser")
                if args.raw_response:
                    records.append(resp["text"])
                    got = 1
                else:
                    if not args.item and not args.field:
                        sel, n_ = auto_item_selector(soup)
                        if not sel and soup.select("a[href]"):
                            sel, n_ = "a[href]", len(soup.select("a[href]"))
                            args.field = ["teks=@text", "link=@href"]
                            log(f"  auto: gak ada elemen berulang -> ambil {n_} link halaman")
                        elif sel:
                            args.item = sel
                            fs = auto_html_fields(soup, sel)
                            args.field = [f"{f.name}={f.loc}@{f.attr}" for f in fs]
                            log(f"  auto: pakai --item '{sel}' ({n_} elemen), {len(fs)} kolom: "
                                + ", ".join(f.name for f in fs))
                        else:
                            log("  auto: halaman ini kosong / di-render JavaScript. "
                                "Coba:  --discover")
                    if not args.field:
                        log("  gak ada yang bisa diekstrak dari HTML ini.\n"
                            "    -> jalankan dengan --discover buat nyari endpoint API-nya,\n"
                            "    -> atau tentukan manual: --item 'div.kartu' -f 'judul=h3@text'")
                        break
                    got_rows = extract_html(soup, resp["url"], args)
                    for row in got_rows:
                        if args.meta:
                            row["_url"], row["_status"] = resp["url"], resp["status"]
                        records.append(row)
                    got = len(got_rows)
                nxt = find_next(soup, resp["url"], args)

            log(f"  HTTP {resp['status']} | {len(resp['text']):,} byte | +{got} record (total {len(records)})")

            if args.limit and len(records) >= args.limit:
                records = records[: args.limit]
                break
            if got == 0 and args.stop_on_empty and halaman > 0:
                log("  halaman kosong — berhenti.")
                break
            if halaman == args.pages - 1:
                break

            # URL halaman berikutnya
            if nxt:
                cur = urljoin(resp["url"], nxt)
            elif args.next_param:
                base, _, qs = resp["url"].partition("?")
                nomor = args.start_page + halaman + 1
                others = [kv for kv in qs.split("&") if kv and not kv.startswith(args.next_param + "=")]
                cur = base + "?" + "&".join([f"{args.next_param}={nomor}"] + others)
            else:
                if args.next:
                    log(f"  link next ('{args.next}') gak ketemu di halaman ini — berhenti.")
                else:
                    log("  gak ada aturan halaman berikutnya (--next / --next-param) — berhenti.")
                break

            time.sleep(max(0.0, args.delay) + random.uniform(0, max(0.0, args.jitter)))

    if args.save_cookies and args.cookies:
        save_cookies(sesi, args.cookies)

    if not pages:
        log("\nGak ada respons sukses. Cek URL / header / auth.")
        return 1

    write_output(records, pages, args)
    lokasi = args.out if args.out != "-" else "stdout"
    log(f"\n✔ {total_req} request | {len(pages)} halaman | {len(records)} record -> {lokasi}")
    return 0


ARGS = None
if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\ndibatalkan.", file=sys.stderr)
        sys.exit(130)
