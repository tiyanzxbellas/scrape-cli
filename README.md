# scrape-cli

Scraper generik satu file: kasih `--base-url` + `--endpoint`, dia yang urus request,
pagination, ekstraksi field, dan output ke CSV/JSON/NDJSON. Jalan di **Termux (Android)**,
Linux, dan macOS. Zero config untuk mulai.

```bash
python scrape.py -b https://api.target.com -e /v1/produk \
    --items results --pages 3 --next next \
    -f nama=name -f harga=price.value \
    -o hasil.csv
```

## Cara paling gampang: kasih link aja

```bash
python scrape.py https://situs.com/api/produk -o hasil.csv
```
Selesai. Tool-nya otomatis:
- misahin **base-url** dan **endpoint** dari link itu
- nebak mode **JSON / HTML** dari Content-Type
- kalau JSON: nyari list terbesar di respons, jadiin itu baris-baris CSV
- kalau HTML: nyari elemen berulang (kartu/baris tabel) pakai heuristik keseragaman,
  terus bikin kolom dari anak-anaknya

```bash
$ python scrape.py https://quotes.toscrape.com -o kutipan.csv
  auto: pakai --item 'div.quote' (10 elemen), 6 kolom: a_tag, span_text, ...
✔ 1 request | 1 halaman | 10 record -> kutipan.csv
```

### Kalau hasilnya kosong → situsnya SPA

Situs React/Vue/Next mengirim HTML kerangka kosong (`<div id="root">`), datanya diambil
lewat API di sisi klien. Pakai **`--discover`** buat melacak endpoint-nya otomatis:

```bash
python scrape.py https://situs-spa.com --discover
```
Dia akan: deteksi bahwa ini SPA → unduh semua bundle JS (termasuk chunk lazy-load) →
grep URL API, path `/api/`·`/v1/`·`/graphql`, project Supabase/Firebase, nama tabel
(`.from('...')`), dan fungsi RPC → lalu **nyetak perintah siap pakai**.

```
★ Ini SPA (React/Vue/Next) — HTML-nya kerangka kosong, data diambil dari API.
[js] mengunduh 3 file bundle...
[js] +27 chunk lazy-load...
     terkumpul 728.461 byte JavaScript dari 30 file

[Path API]
  /api/v1/popular
  /api/v1/schedule
  /api/v1/genre

SARAN PERINTAH
python scrape.py -b https://situs-spa.com -e /api/v1/popular --inspect
```

Urutan yang benar: **`--discover` → `--inspect` → scrape**.

**Fitur**
- Target **JSON** (dot-path: `data.items[*].price`) dan **HTML** (CSS selector: `h3 a@title`)
- **Pagination**: ikut URL `next` dari respons, naikkan nomor halaman otomatis, atau cursor
- **Cukup kasih link**: `python scrape.py https://situs.com` — base-url, endpoint, mode, dan kolom ditebak otomatis
- **`--discover`**: lacak endpoint API situs SPA dengan membedah bundle JavaScript-nya
- **Inspeksi otomatis**: `--inspect` nyetak struktur respons + saran `--items`/`--field`/`--next`
- **Dry-run**: `--dry-run` cetak semua URL yang bakal di-request tanpa ngirim apa-apa
- **Login/sesi**: POST login, simpan-muat cookie, Basic auth, Bearer token, proxy
- **Config file**: satu `-c config.json` menggantikan baris perintah panjang
- **Sopan ke server**: retry + backoff eksponensial, hormati `Retry-After`, jeda + jitter acak
- **Output**: `csv`, `json`, `ndjson`, `raw`, `html` — ke file atau stdout (bisa di-pipe)

## Install

**Termux (Android)**
```bash
pkg update && pkg upgrade -y
pkg install -y python git
pip install requests beautifulsoup4 lxml

git clone https://github.com/tiyanzxbellas/scrape-cli.git
cd scrape-cli
python scrape.py --help
```
Kalau belum punya repo-nya, cukup salin `scrape.py` ke folder mana pun — script ini mandiri.

**Linux / macOS**
```bash
git clone https://github.com/tiyanzxbellas/scrape-cli.git
cd scrape-cli
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scrape.py --help
```

> `requests` saja sudah cukup untuk target JSON. `bs4`/`lxml` cuma dipakai kalau parse HTML.

## Coba dulu (semua contoh ini aman & legal untuk di-scrape)

```bash
# JSON API + pagination lewat URL 'next'
python scrape.py -c examples/config-pokeapi.json

# HTML + pagination lewat tombol next
python scrape.py -c examples/config-html-quotes.json

# JSON API + pagination nomor halaman, banyak endpoint sekaligus
python scrape.py -c examples/config-dummyjson.json

# Lihat rencana request tanpa mengirim apa pun
python scrape.py -c examples/config-pokeapi.json --dry-run
```

## Isi repo

```
scrape.py                          # tool-nya (satu file, tanpa package)
README.md                          # dokumentasi lengkap
docs/termux.md                     # panduan Termux: install, cron, wake-lock, batasan
examples/config-pokeapi.json       # contoh: JSON + pagination via 'next'
examples/config-html-quotes.json   # contoh: HTML + CSS selector
examples/config-dummyjson.json     # contoh: JSON + pagination nomor halaman
examples/config-login.json.example # template login (copy, isi, jangan commit)
requirements.txt
```

---

## Alur kerja (selalu mulai dari sini)

### Langkah 1 — inspeksi
```bash
python scrape.py -b https://api.target.com -e /v1/produk --inspect
```
Output-nya nyetak **dot-path** yang tersedia + **saran otomatis**:
```
   Kandidat --items: 'results'  (20 objek)
   Contoh field: name, url
   Kandidat --next  : 'next' = https://api.../pokemon?offset=20&limit=20
```
Untuk HTML, dia nyetak selector yang muncul berulang (`div.product-card 3x`) dan mendeteksi
**JSON tersembunyi** di dalam HTML (`__NEXT_DATA__`, `JSON-LD`, `__INITIAL_STATE__`) — itu
biasanya jalur paling rapi dibanding parse DOM.

### Langkah 2 — dry-run (cek URL tanpa ngirim request)
```bash
python scrape.py -b https://api.target.com -e /v1/produk --pages 3 --next-param page --dry-run
```
```
GET https://api.target.com/v1/produk
GET https://api.target.com/v1/produk?page=2
GET https://api.target.com/v1/produk?page=3
```

### Langkah 3 — jalanin beneran
```bash
python scrape.py -b https://api.target.com -e /v1/produk \
    --items results --pages 3 --next next \
    --field nama=name --field harga=price.value \
    -o hasil.csv
```

---

## Target JSON (paling gampang)

```bash
# field bertingkat pakai dot-path
python scrape.py -b https://dummyjson.com -e /products \
    --param limit=10 --items products --pages 5 --next-param page \
    -f id=id -f nama=title -f harga=price -f kategori=category -f stok=stock \
    -o produk.csv

# ambil respons mentah apa adanya (1 halaman -> objek, >1 -> array)
python scrape.py -b https://api.target.com -e /v1/detail/123 --raw-response -o detail.json

# pagination pakai URL/kursor dari respons
python scrape.py -b https://api.target.com -e /feed --items data --next "paging.next" --pages 20

# POST + body JSON + header custom (mis. Bearer token)
python scrape.py -b https://api.target.com -e /v1/search -X POST \
    --json '{"query":"sepatu","page":1}' \
    -H "Authorization: Bearer TOKEN_KAMU" \
    -H "Content-Type: application/json" \
    --items result.list -f nama=title -f harga=price -o cari.csv

# body dari file
python scrape.py -b https://api.target.com -e /v1/search -X POST --json @body.json --raw-response
```

## Target HTML

```bash
# kartu produk + pagination lewat tombol next
python scrape.py -b https://toko.com -e /kategori/sepatu --as html \
    --item "div.product-card" \
    -f "judul=h3 a@title" \
    -f "harga=.price@text" \
    -f "link=h3 a@href" \
    -f "img=img.product@src" \
    --next "li.next a" --pages 10 --delay 2 -o sepatu.csv

# tabel
python scrape.py -b https://situs.com -e /daftar --as html \
    --item "table.data tbody tr" \
    -f "kode=td:nth-of-type(1)@text" \
    -f "nama=td:nth-of-type(2)@text" \
    -f "harga=td:nth-of-type(3)@text" -o tabel.csv

# banyak endpoint sekaligus
python scrape.py -b https://situs.com -e /page/1 -e /page/2 -e /page/3 --as html --item "..." -f "..."
```

**Sintaks `--field` HTML:** `nama=SELECTOR@ATRIBUT`
| atribut | arti |
|---|---|
| `@text` | isi teks (default kalau `@` gak ditulis) |
| `@href`, `@src` | nilai atribut, **otomatis dijadikan URL absolut** |
| `@title`, `@data-id`, dll | nilai atribut apa pun |
| `@html` / `@outer` | HTML di dalamnya / HTML elemen utuh |
| (selector kosong, mis. `-f "nama=@text"`) | ambil dari node `--item` itu sendiri |

`SELECTOR` relatif terhadap `--item`. Kalau `--item` gak diisi, selector dicari dari root dokumen.

---

## Login / sesi privat

```bash
# login form-encoded, simpan cookie, langsung scrape endpoint privat
python scrape.py -b https://situs.com -e /api/dashboard \
    --login-endpoint /login \
    --login-data "username=saya" --login-data "password=rahasia" \
    --cookies sesi.json --save-cookies \
    --raw-response -o dash.json

# request berikutnya tinggal pakai cookie yang tersimpan (gak perlu login lagi)
python scrape.py -b https://situs.com -e /api/lain --cookies sesi.json --raw-response
```
Kalau situsnya pakai token: `-H "Authorization: Bearer xxx"`.
Kalau pakai Basic auth: `--auth user:pass`.

---

## Config file — biar gak ngetik panjang

`config.json`:
```json
{
  "base_url": "https://dummyjson.com",
  "endpoint": ["/products", "/products/category/beauty"],
  "items": "products",
  "field": ["id=id", "nama=title", "harga=price", "kategori=category", "stok=stock"],
  "pages": 2,
  "next_param": "page",
  "delay": 0.4,
  "out": "hasil.csv",
  "format": "csv"
}
```
```bash
python scrape.py -c config.json              # jalan
python scrape.py -c config.json --dry-run    # cek rencana
python scrape.py -c config.json --pages 10 -o lain.csv   # CLI menang atas config
```
Nama key = nama flag dengan `_` (mis. `--next-param` → `next_param`).

---

## Opsi penting lainnya

| Flag | Fungsi |
|---|---|
| `--limit N` | berhenti setelah N record (buat sampling/cek cepat) |
| `--delay 2 --jitter 1` | jeda 2–3 detik antar request. **Jangan di-set 0** di situs orang. |
| `--retry 3` | retry otomatis untuk 429/5xx/error jaringan (backoff eksponensial + hormati `Retry-After`) |
| `--timeout 20` | batas tunggu per request |
| `--format csv/json/ndjson/raw/html` | format output. `ndjson` enak buat data gede (streaming, 1 baris = 1 record) |
| `--meta` | tambah kolom `_url` & `_status` per record (buat tau datanya dari halaman mana) |
| `--wrap` | JSON: bungkus jadi `{count, data}` |
| `--encoding utf-8` | paksa encoding kalau hasilnya mojibake (`Â£`, `Ã©`) |
| `--proxy http://host:port` | lewat proxy |
| `-k / --insecure` |abaikan error sertifikat |
| `--allow-status` | tetap proses walau HTTP bukan 2xx |
| `-v` | cetak URL final + body tiap request |
| `-q` | diamkan log (output bersih ke stdout) |
| `-o -` | output ke stdout, bisa di-pipe: `... -o - \| head` |

---

## Catatan penting di Termux

- **Biar gak dimatiin Android** saat scrape panjang:
  ```bash
  pkg install termux-api
  termux-wake-lock
  python scrape.py -c config.json
  termux-wake-unlock
  ```
  Plus: Settings → Apps → Termux → Battery → *Unrestricted*.
- **Pindahin hasil ke memori HP:**
  ```bash
  termux-setup-storage          # sekali saja
  cp hasil.csv ~/storage/downloads/
  ```
- **Terjadwal:** `pkg install cronie && crond`, atau Tasker + plugin Termux:Tasker.
- **Situs yang butuh eksekusi JavaScript** (Shopee, Tokopedia, IG, TikTok, kebanyakan SPA):
  `requests` cuma dapat kerangka HTML kosong. Cek dulu pakai `--inspect` — kalau ada bintang
  ★ "data JSON tersembunyi", artinya datanya ada tapi butuh diambil dari blok `<script>`.
  Kalau gak ada, butuh browser headless (Playwright) → di Termux pakai
  `pkg install proot-distro && proot-distro install ubuntu`, atau jalanin di VPS.
- **Cari endpoint aslinya**: buka situs di browser laptop → DevTools → tab *Network* → filter
  *Fetch/XHR* → cari request yang balikin data yang kamu mau → klik kanan → *Copy as cURL*.
  Dari situ kelihatan base-url, endpoint, param, dan header yang dibutuhkan. Cara ini 10x
  lebih cepat daripada nebak-nebak selector HTML.

## Etika
Cek `robots.txt`, hormati ToS, pakai delay, jangan paralel brutal, jangan simpan data pribadi
orang tanpa dasar hukum (UU PDP No. 27/2022). Default `User-Agent` script ini sudah jujur;
kalau scraping serius, ganti dengan kontakmu: `-A "BotNama/1.0 (+https://situsmu.com; email@kamu.com)"`.

---

## Perilaku yang perlu dipahami

- **Selector yang cocok banyak elemen → diambil yang pertama saja.**
  Untuk ambil semua, jadikan dia `--item`, lalu field-nya relatif terhadap item itu.
  ```bash
  # SALAH: cuma dapat tag pertama dari seluruh halaman
  -f "tags=div.tags a.tag@text"

  # BENAR: tiap kutipan jadi satu baris
  --item "div.quote" -f "tags=div.tags a.tag@text"
  ```
- **`--items` wajib untuk JSON yang list-nya bersarang.** Tanpa `--items`, seluruh respons
  dianggap satu record.
- **Pagination berhenti otomatis** kalau: halaman kosong, link `next` sudah tidak ada,
  `--limit` tercapai, atau `--pages` habis.
- **HTTP 4xx tidak di-retry** (URL salah ya salah). Yang di-retry cuma 429, 5xx, dan error
  jaringan — dengan backoff eksponensial dan menghormati header `Retry-After`.
- **`--field` menentukan urutan kolom** di CSV. Kolom digabung dari semua record, jadi
  record yang field-nya beda tetap masuk (sel kosong).

## Troubleshooting

| Gejala | Penyebab & solusi |
|---|---|
| `--items` hasilnya bukan list | Path salah. Jalankan `--inspect` untuk lihat dot-path yang benar. |
| Hasil CSV cuma 1 baris padahal datanya banyak | Lupa `--items` (JSON) atau `--item` (HTML). |
| Kolom kosong semua | Selector/path salah, atau elemen di-render JavaScript (lihat bawah). |
| `Â£51.77`, `Ã©` (mojibake) | Tambahkan `--encoding utf-8`. |
| HTML baliknya kerangka kosong / `<div id="root"></div>` | Situs butuh eksekusi JS. `requests` tidak menjalankan JS — butuh Playwright, atau cari endpoint JSON-nya lewat DevTools. |
| HTTP 403 terus-menerus | Butuh header tambahan (`Referer`, `Origin`, `User-Agent` browser) atau Cloudflare. Coba `-A "Mozilla/5.0 ..."`. |
| HTTP 429 | Terlalu cepat. Naikkan `--delay`, jangan pakai `--jitter 0`. |
| `pip: command not found` di Termux | `pkg install python` dulu, bukan `apt`. |
| Proses mati sendiri saat scrape panjang di Android | `termux-wake-lock` + matikan optimasi baterai Termux. |

## Keamanan

- **Jangan pernah commit token, password, atau file cookie.** `.gitignore` di repo ini sudah
  memblokir `sesi*.json`, `cookies*.json`, `.env`, `config.local.json`, dan semua hasil scrape.
- Pakai `examples/config-login.json.example` sebagai template → copy jadi `config-login.json`
  (sudah ter-ignore) → isi di situ.
- Kalau token/password sudah terlanjur ketempel di chat, terminal yang di-share, atau commit:
  **cabut dan ganti sekarang juga**. Riwayat git tidak menghapus rahasia yang sudah bocor.
- Cek dulu sebelum commit:
  ```bash
  git status
  git diff --cached | grep -iE "token|password|secret|ghp_|cookie" && echo "⚠️ ADA KREDENSIAL!"
  ```

## Lisensi

MIT — lihat [LICENSE](LICENSE). Panduan khusus Termux ada di [docs/termux.md](docs/termux.md).
