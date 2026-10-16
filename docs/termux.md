# Scraping lewat Termux — panduan cepat

## 1. Instal Termux
- Ambil dari **F-Droid** atau **GitHub** (`termux-app` releases).
- Versi Play Store sudah lama tidak di-update, sering bermasalah dengan repo.
- Buka Termux, jalankan sekali:
  ```bash
  pkg update && pkg upgrade -y
  ```

## 2. Pasang Python
```bash
pkg install -y python git
pip install --upgrade pip
pip install requests beautifulsoup4 lxml
```

## 3. Simpan & jalankan script
```bash
mkdir -p ~/scrape && cd ~/scrape
# salin isi scraper_demo.py ke sini (nano scraper_demo.py -> paste -> Ctrl+O, Enter, Ctrl+X)
python scraper_demo.py --pages 5
```
Hasilnya `buku.csv`. Buka di HP:
```bash
termux-setup-storage      # sekali saja, izinkan akses penyimpanan
cp buku.csv ~/storage/downloads/
```
Atau lihat langsung: `column -s, -t buku.csv | less -S`

## 4. Kalau mau bikin scraper sendiri
Urutan yang dipakai hampir selalu sama:
1. **Cek `robots.txt`** → `https://situs.com/robots.txt` (ada larangan path tertentu?)
2. **Cek apakah datanya HTML atau JSON** → buka DevTools di laptop, tab *Network*, filter *Fetch/XHR*.
   Kalau ada request yang balikin JSON, **pakai itu** — jauh lebih gampang & ringan daripada parsing HTML.
3. **Request** dengan `requests.Session()` + header `User-Agent` sendiri.
4. **Parse** dengan `BeautifulSoup` (HTML) atau `r.json()` (JSON).
5. **Simpan** ke CSV/SQLite: `pkg install sqlite` lalu pakai modul `sqlite3` bawaan Python.
6. **Jeda** 1–3 detik antar request + jitter acak. Jangan hammer server.

## 5. Batas di Android/Termux yang perlu tahu
- Termux **tidak bisa jalan terus** kalau Android mematikan background app. Solusi:
  - `termux-wake-lock` (dari `pkg install termux-api` / paket `termux-services`),
  - atau matikan optimasi baterai untuk Termux di Settings → Apps.
- Untuk jalan terjadwal: `pkg install cronie` lalu `crond`, atau pakai **Tasker + Termux:Tasker**.
- Situs yang butuh login/JS berat (Shopee, Tokopedia, Instagram, TikTok) **tidak bisa** dengan `requests` saja.
  Butuh Playwright/Selenium → berat & sering tidak jalan mulus di Termux (perlu browser headless + root/proot).
  Alternatif: pakai `proot-distro install ubuntu`, atau jalankan scraper-nya di VPS/laptop saja.

## 6. Etika & legal (penting)
- Hormati `robots.txt`, ToS situs, dan hak cipta data.
- Jangan simpan/olah data pribadi orang lain tanpa dasar hukum (di Indonesia ada **UU PDP No. 27/2022**).
- Volume rendah, jeda antar request, pakai API resmi kalau ada (banyak situs punya API gratis).
- Scraping untuk latihan: pakai `books.toscrape.com`, `quotes.toscrape.com`, `scrapethissite.com`.

## 7. Naik level
```bash
pip install httpx pandas scrapy playwright
```
- `httpx` → async, lebih cepat untuk banyak request.
- `pandas` → olah hasil jadi tabel/analisis.
- `scrapy` → framework lengkap (antrian, pipeline, throttle otomatis). Jalan di Termux, tapi agak rewel install.
