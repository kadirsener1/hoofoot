#!/usr/bin/env python3
"""
HooFoot.ru IPTV M3U Playlist Generator
Tüm kanalları tarar, HLS linklerini bulur ve M3U dosyasına yazar.
"""

import requests
import re
import json
import time
import os
import logging
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import urljoin

# Logging ayarları
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ============================================================
# YAPILANDIRMA
# ============================================================
BASE_URL = "https://hoofoot.ru"
API_URL = f"{BASE_URL}/api/channels"
CHANNEL_PAGE_URL = f"{BASE_URL}/iptv/channel?id="
OUTPUT_FILE = "hoofoot_playlist.m3u"
MAX_WORKERS = 10          # Eşzamanlı istek sayısı
REQUEST_TIMEOUT = 15      # İstek zaman aşımı (saniye)
DELAY_BETWEEN_REQUESTS = 0.3  # İstekler arası bekleme
RETRY_COUNT = 3           # Başarısız istekler için tekrar deneme

# Tarayıcı gibi görünmek için headers
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                  "AppleWebKit/537.36 (KHTML, like Gecko) "
                  "Chrome/125.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;"
              "q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "tr-TR,tr;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Referer": BASE_URL + "/",
}


# ============================================================
# SESSION OLUŞTUR
# ============================================================
def create_session():
    """İstekler için oturum oluşturur."""
    session = requests.Session()
    session.headers.update(HEADERS)
    # Retry adapter
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    retry_strategy = Retry(
        total=RETRY_COUNT,
        backoff_factor=1,
        status_forcelist=[429, 500, 502, 503, 504],
    )
    adapter = HTTPAdapter(max_retries=retry_strategy)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


# ============================================================
# KANAL LİSTESİNİ API'DEN ÇEK
# ============================================================
def fetch_channel_list(session):
    """
    /api/channels endpoint'inden tüm kanal listesini çeker.
    Farklı API yanıt formatlarını destekler.
    """
    logger.info(f"Kanal listesi çekiliyor: {API_URL}")

    try:
        resp = session.get(API_URL, timeout=REQUEST_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"API isteği başarısız: {e}")
        return []
    except json.JSONDecodeError as e:
        logger.error(f"JSON parse hatası: {e}")
        logger.debug(f"Yanıt içeriği: {resp.text[:500]}")
        return []

    channels = []

    # --- Format 1: Doğrudan liste ---
    if isinstance(data, list):
        channels = data

    # --- Format 2: {"channels": [...]} ---
    elif isinstance(data, dict):
        if "channels" in data:
            channels = data["channels"]
        elif "data" in data:
            channels = data["data"]
        elif "items" in data:
            channels = data["items"]
        elif "result" in data:
            channels = data["result"]
        else:
            # Belki dict'in value'ları kanal gruplarıdır
            for key, value in data.items():
                if isinstance(value, list):
                    channels.extend(value)

    logger.info(f"Toplam {len(channels)} kanal bulundu.")

    # Debug: İlk kanalın yapısını logla
    if channels:
        logger.debug(f"Örnek kanal verisi: {json.dumps(channels[0], ensure_ascii=False, indent=2)}")

    return channels


# ============================================================
# KANAL BİLGİLERİNİ PARSE ET
# ============================================================
def parse_channel_info(channel_data):
    """
    API'den gelen kanal verisini standart formata dönüştürür.
    Farklı alan adlarını destekler.
    """
    info = {
        "id": None,
        "name": "Bilinmeyen Kanal",
        "logo": "",
        "group": "Diğer",
        "country": "",
        "language": "",
        "url": "",
        "epg_id": "",
    }

    if isinstance(channel_data, dict):
        # ID
        info["id"] = (
            channel_data.get("id") or
            channel_data.get("channel_id") or
            channel_data.get("channelId") or
            channel_data.get("ID")
        )

        # İsim
        info["name"] = (
            channel_data.get("name") or
            channel_data.get("title") or
            channel_data.get("channel_name") or
            channel_data.get("channelName") or
            channel_data.get("display_name") or
            "Bilinmeyen Kanal"
        )

        # Logo
        logo = (
            channel_data.get("logo") or
            channel_data.get("image") or
            channel_data.get("icon") or
            channel_data.get("thumbnail") or
            channel_data.get("logo_url") or
            channel_data.get("img") or
            ""
        )
        if logo and not logo.startswith("http"):
            logo = urljoin(BASE_URL, logo)
        info["logo"] = logo

        # Grup / Kategori
        info["group"] = (
            channel_data.get("group") or
            channel_data.get("category") or
            channel_data.get("genre") or
            channel_data.get("group_title") or
            channel_data.get("type") or
            "Diğer"
        )

        # Ülke
        info["country"] = (
            channel_data.get("country") or
            channel_data.get("nation") or
            channel_data.get("region") or
            channel_data.get("country_code") or
            ""
        )

        # Dil
        info["language"] = (
            channel_data.get("language") or
            channel_data.get("lang") or
            ""
        )

        # EPG
        info["epg_id"] = (
            channel_data.get("epg_id") or
            channel_data.get("tvg_id") or
            channel_data.get("xmltv_id") or
            ""
        )

    return info


# ============================================================
# HLS LİNKLERİNİ BUL
# ============================================================

# HLS link pattern'leri
HLS_PATTERNS = [
    # .m3u8 uzantılı URL'ler (en yaygın)
    r'(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
    # Tek tırnak içindeki m3u8
    r"'(https?://[^']+\.m3u8[^']*)'",
    # Çift tırnak içindeki m3u8
    r'"(https?://[^"]+\.m3u8[^"]*)"',
    # JavaScript değişkenlerinde
    r'(?:source|src|url|file|stream|video|hls|manifest)\s*[:=]\s*["\']?(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
    # JSON içindeki URL'ler
    r'"(?:source|src|url|file|stream|video|hls|manifest)"\s*:\s*"(https?://[^"]+\.m3u8[^"]*)"',
    # data attribute'ları
    r'data-(?:source|src|url|stream|video)\s*=\s*["\']?(https?://[^\s\'"<>]+\.m3u8[^\s\'"<>]*)',
]

# İkincil stream pattern'leri (m3u8 bulunamazsa)
SECONDARY_PATTERNS = [
    r'(https?://[^\s\'"<>]+/live/[^\s\'"<>]+)',
    r'(https?://[^\s\'"<>]+/stream/[^\s\'"<>]+)',
    r'(https?://[^\s\'"<>]+/hls/[^\s\'"<>]+)',
    r'(https?://[^\s\'"<>]+/playlist[^\s\'"<>]*)',
    r'(https?://[^\s\'"<>]+\.ts[^\s\'"<>]*)',
    r'(https?://[^\s\'"<>]+\.mpd[^\s\'"<>]*)',
]


def extract_hls_from_page(html_content):
    """Sayfa HTML'inden HLS linklerini çıkarır."""
    found_urls = set()

    # Ana HLS pattern'lerini dene
    for pattern in HLS_PATTERNS:
        matches = re.findall(pattern, html_content, re.IGNORECASE)
        for match in matches:
            url = match.strip().rstrip('\\').rstrip('"').rstrip("'")
            # Geçersiz URL'leri filtrele
            if is_valid_stream_url(url):
                found_urls.add(url)

    # m3u8 bulunamadıysa ikincil pattern'leri dene
    if not found_urls:
        for pattern in SECONDARY_PATTERNS:
            matches = re.findall(pattern, html_content, re.IGNORECASE)
            for match in matches:
                url = match.strip().rstrip('\\').rstrip('"').rstrip("'")
                if is_valid_stream_url(url):
                    found_urls.add(url)

    return list(found_urls)


def extract_hls_from_scripts(html_content, session):
    """
    Sayfadaki harici JS dosyalarından da HLS linki arar.
    Bazı siteler linkleri harici JS'de saklıyor.
    """
    found_urls = set()

    # Sayfadaki script src'lerini bul
    script_srcs = re.findall(
        r'<script[^>]+src=["\']([^"\']+)["\']',
        html_content, re.IGNORECASE
    )

    for src in script_srcs:
        # Sadece site ile ilgili JS dosyalarını kontrol et
        if "hoofoot" in src or not src.startswith("http"):
            full_url = urljoin(BASE_URL, src)
            try:
                js_resp = session.get(full_url, timeout=REQUEST_TIMEOUT)
                if js_resp.status_code == 200:
                    js_content = js_resp.text
                    for pattern in HLS_PATTERNS:
                        matches = re.findall(pattern, js_content, re.IGNORECASE)
                        for match in matches:
                            url = match.strip()
                            if is_valid_stream_url(url):
                                found_urls.add(url)
            except Exception:
                pass

    return list(found_urls)


def extract_hls_from_iframes(html_content, session):
    """
    Sayfadaki iframe'lerin içindeki HLS linklerini bulur.
    Birçok IPTV sitesi embed player kullanır.
    """
    found_urls = set()

    iframe_srcs = re.findall(
        r'<iframe[^>]+src=["\']([^"\']+)["\']',
        html_content, re.IGNORECASE
    )

    for src in iframe_srcs:
        full_url = urljoin(BASE_URL, src)
        try:
            iframe_resp = session.get(
                full_url,
                timeout=REQUEST_TIMEOUT,
                headers={**HEADERS, "Referer": BASE_URL + "/"}
            )
            if iframe_resp.status_code == 200:
                iframe_html = iframe_resp.text
                urls = extract_hls_from_page(iframe_html)
                found_urls.update(urls)

                # İframe içindeki JS'leri de kontrol et
                js_urls = extract_hls_from_scripts(iframe_html, session)
                found_urls.update(js_urls)
        except Exception:
            pass

    return list(found_urls)


def is_valid_stream_url(url):
    """Stream URL'sinin geçerli olup olmadığını kontrol eder."""
    if not url or len(url) < 10:
        return False
    if not url.startswith("http"):
        return False
    # Statik dosyaları hariç tut
    exclude_extensions = ['.js', '.css', '.png', '.jpg', '.jpeg', '.gif', '.svg', '.ico', '.woff']
    for ext in exclude_extensions:
        if url.lower().endswith(ext):
            return False
    # Bilinen CDN/analytics URL'lerini hariç tut
    exclude_domains = ['google', 'facebook', 'analytics', 'doubleclick', 'adsense']
    for domain in exclude_domains:
        if domain in url.lower():
            return False
    return True


# ============================================================
# TEK BİR KANALI İŞLE
# ============================================================
def process_channel(channel_data, session):
    """
    Tek bir kanalı işler:
    1. Kanal bilgilerini parse et
    2. Kanal sayfasını aç
    3. HLS linkini bul
    """
    info = parse_channel_info(channel_data)

    if not info["id"]:
        logger.warning(f"Kanal ID'si bulunamadı: {channel_data}")
        return None

    channel_url = f"{CHANNEL_PAGE_URL}{info['id']}"
    logger.info(f"İşleniyor: {info['name']} (ID: {info['id']})")

    try:
        time.sleep(DELAY_BETWEEN_REQUESTS)

        resp = session.get(
            channel_url,
            timeout=REQUEST_TIMEOUT,
            headers={**HEADERS, "Referer": BASE_URL + "/iptv/"}
        )

        if resp.status_code != 200:
            logger.warning(f"Sayfa yüklenemedi ({resp.status_code}): {info['name']}")
            return None

        html = resp.text

        # 1. Doğrudan sayfadan HLS ara
        hls_urls = extract_hls_from_page(html)

        # 2. Bulunamadıysa script dosyalarını kontrol et
        if not hls_urls:
            hls_urls = extract_hls_from_scripts(html, session)

        # 3. Hâlâ bulunamadıysa iframe'leri kontrol et
        if not hls_urls:
            hls_urls = extract_hls_from_iframes(html, session)

        # 4. API yanıtında doğrudan URL varsa onu kullan
        if not hls_urls:
            direct_url = (
                channel_data.get("url") or
                channel_data.get("stream_url") or
                channel_data.get("streamUrl") or
                channel_data.get("source") or
                channel_data.get("link") or
                ""
            )
            if direct_url and is_valid_stream_url(direct_url):
                hls_urls = [direct_url]

        if hls_urls:
            # En iyi URL'yi seç (m3u8 öncelikli)
            best_url = select_best_url(hls_urls)
            info["url"] = best_url
            logger.info(f"✅ Bulundu: {info['name']} -> {best_url[:80]}...")
            return info
        else:
            logger.warning(f"❌ HLS bulunamadı: {info['name']}")
            return None

    except requests.exceptions.Timeout:
        logger.warning(f"⏱️ Zaman aşımı: {info['name']}")
        return None
    except requests.exceptions.RequestException as e:
        logger.error(f"🔥 İstek hatası ({info['name']}): {e}")
        return None
    except Exception as e:
        logger.error(f"🔥 Beklenmeyen hata ({info['name']}): {e}")
        return None


def select_best_url(urls):
    """Birden fazla URL varsa en iyisini seçer."""
    # m3u8 uzantılı olanı tercih et
    m3u8_urls = [u for u in urls if '.m3u8' in u.lower()]
    if m3u8_urls:
        # "master" veya "index" içereni tercih et
        for preferred in ['master', 'index', 'playlist', 'live']:
            for url in m3u8_urls:
                if preferred in url.lower():
                    return url
        return m3u8_urls[0]

    # mpd (DASH) tercih et
    mpd_urls = [u for u in urls if '.mpd' in u.lower()]
    if mpd_urls:
        return mpd_urls[0]

    return urls[0]


# ============================================================
# M3U DOSYASINI OLUŞTUR
# ============================================================
def generate_m3u(channels, output_file):
    """Bulunan kanallardan M3U playlist dosyası oluşturur."""
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(output_file, "w", encoding="utf-8") as f:
        # M3U Header
        f.write('#EXTM3U x-tvg-url="" '
                f'catchup="default" '
                f'catchup-source="" '
                f'catchup-days="7"\n')
        f.write(f'# HooFoot.ru IPTV Playlist\n')
        f.write(f'# Oluşturulma: {now}\n')
        f.write(f'# Toplam Kanal: {len(channels)}\n\n')

        # Kanalları gruba göre sırala
        channels_sorted = sorted(channels, key=lambda x: (x.get("group", ""), x.get("name", "")))

        for ch in channels_sorted:
            # EXTINF satırı
            tvg_id = ch.get("epg_id", "") or ""
            tvg_name = ch.get("name", "").replace(",", " ")
            tvg_logo = ch.get("logo", "") or ""
            group = ch.get("group", "Diğer") or "Diğer"
            country = ch.get("country", "") or ""
            language = ch.get("language", "") or ""

            extinf_parts = [
                f'#EXTINF:-1',
                f'tvg-id="{tvg_id}"',
                f'tvg-name="{tvg_name}"',
                f'tvg-logo="{tvg_logo}"',
                f'tvg-country="{country}"',
                f'tvg-language="{language}"',
                f'group-title="{group}"',
            ]

            extinf_line = " ".join(extinf_parts) + f",{tvg_name}"
            f.write(extinf_line + "\n")
            f.write(ch["url"] + "\n\n")

    logger.info(f"✅ M3U dosyası oluşturuldu: {output_file}")
    logger.info(f"   Toplam kanal sayısı: {len(channels)}")


# ============================================================
# İSTATİSTİK RAPORU
# ============================================================
def print_statistics(all_channels, found_channels):
    """Tarama istatistiklerini yazdırır."""
    total = len(all_channels)
    found = len(found_channels)
    failed = total - found
    success_rate = (found / total * 100) if total > 0 else 0

    print("\n" + "=" * 60)
    print("📊 TARAMA İSTATİSTİKLERİ")
    print("=" * 60)
    print(f"  📺 Toplam kanal     : {total}")
    print(f"  ✅ Başarılı         : {found}")
    print(f"  ❌ Başarısız        : {failed}")
    print(f"  📈 Başarı oranı    : {success_rate:.1f}%")
    print("=" * 60)

    # Grup bazlı istatistik
    if found_channels:
        groups = {}
        for ch in found_channels:
            g = ch.get("group", "Diğer")
            groups[g] = groups.get(g, 0) + 1

        print("\n📁 GRUP BAZLI DAĞILIM:")
        print("-" * 40)
        for group_name, count in sorted(groups.items(), key=lambda x: -x[1]):
            print(f"  {group_name}: {count} kanal")

    # Ülke bazlı istatistik
    if found_channels:
        countries = {}
        for ch in found_channels:
            c = ch.get("country", "Bilinmiyor") or "Bilinmiyor"
            countries[c] = countries.get(c, 0) + 1

        if any(c != "Bilinmiyor" for c in countries):
            print("\n🌍 ÜLKE BAZLI DAĞILIM:")
            print("-" * 40)
            for country_name, count in sorted(countries.items(), key=lambda x: -x[1]):
                print(f"  {country_name}: {count} kanal")

    print()


# ============================================================
# ANA FONKSİYON
# ============================================================
def main():
    """Ana çalıştırma fonksiyonu."""
    print("=" * 60)
    print("🔴 HooFoot.ru IPTV M3U Playlist Generator")
    print("=" * 60)

    start_time = time.time()
    session = create_session()

    # 1. Kanal listesini çek
    all_channels = fetch_channel_list(session)

    if not all_channels:
        logger.error("Kanal listesi boş! API yanıtını kontrol edin.")

        # Alternatif: Sayfayı doğrudan parse et
        logger.info("Alternatif yöntem deneniyor: Ana sayfadan kanal listesi çekiliyor...")
        all_channels = scrape_channel_list_from_page(session)

        if not all_channels:
            logger.error("Hiç kanal bulunamadı. Çıkılıyor.")
            return

    # 2. Tüm kanalları işle (paralel)
    found_channels = []

    logger.info(f"\n🔄 {len(all_channels)} kanal taranıyor ({MAX_WORKERS} paralel işlem)...\n")

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_channel, ch, session): ch
            for ch in all_channels
        }

        completed = 0
        for future in as_completed(futures):
            completed += 1
            progress = completed / len(all_channels) * 100

            try:
                result = future.result()
                if result and result.get("url"):
                    found_channels.append(result)
            except Exception as e:
                logger.error(f"Thread hatası: {e}")

            # İlerleme göstergesi
            if completed % 10 == 0 or completed == len(all_channels):
                print(f"  İlerleme: {completed}/{len(all_channels)} "
                      f"({progress:.1f}%) - Bulunan: {len(found_channels)}")

    # 3. M3U dosyasını oluştur
    if found_channels:
        generate_m3u(found_channels, OUTPUT_FILE)

        # JSON backup da kaydet
        json_file = OUTPUT_FILE.replace('.m3u', '.json')
        with open(json_file, 'w', encoding='utf-8') as f:
            json.dump(found_channels, f, ensure_ascii=False, indent=2)
        logger.info(f"📋 JSON backup: {json_file}")
    else:
        logger.warning("Hiç kanal bulunamadı! M3U dosyası oluşturulmadı.")

    # 4. İstatistikler
    elapsed = time.time() - start_time
    print_statistics(all_channels, found_channels)
    print(f"⏱️ Toplam süre: {elapsed:.1f} saniye")


# ============================================================
# ALTERNATİF: SAYFADAN KANAL LİSTESİ ÇEKME
# ============================================================
def scrape_channel_list_from_page(session):
    """
    API çalışmazsa ana sayfadan kanal linklerini parse eder.
    """
    channels = []

    try:
        # Ana IPTV sayfasını çek
        resp = session.get(f"{BASE_URL}/iptv/", timeout=REQUEST_TIMEOUT)
        if resp.status_code != 200:
            return channels

        html = resp.text

        # channel?id= formatındaki linkleri bul
        pattern = r'channel\?id=(\d+)'
        ids = set(re.findall(pattern, html))

        # Kanal isimlerini de bulmaya çalış
        # <a href="...channel?id=123">Kanal Adı</a>
        link_pattern = r'<a[^>]*href=["\'][^"\']*channel\?id=(\d+)["\'][^>]*>([^<]+)</a>'
        named_channels = re.findall(link_pattern, html, re.IGNORECASE)

        named_dict = {cid: name.strip() for cid, name in named_channels}

        for cid in ids:
            channels.append({
                "id": int(cid),
                "name": named_dict.get(cid, f"Channel {cid}"),
                "group": "Diğer",
                "country": "",
                "logo": "",
            })

        logger.info(f"Sayfadan {len(channels)} kanal ID'si bulundu.")

    except Exception as e:
        logger.error(f"Sayfa parse hatası: {e}")

    return channels


# ============================================================
# ÇALIŞTIR
# ============================================================
if __name__ == "__main__":
    main()
