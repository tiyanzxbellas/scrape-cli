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
        sys.exit("Mode HTML wajib punya minimal satu --field. Contoh: --field 'judul=h2 a@text'")

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

    if not args.endpoint:
        sys.exit("Wajib minimal satu --endpoint. Contoh: -e /api/v1/items   (lihat --help)")
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
