"""
Kocaeli yerel haber siteleri - İSU haber takip ve bildirim sistemi.

siteler.txt içindeki siteleri tarar, anahtar_kelimeler.txt içindeki kelimelerden
biri geçen YENİ haberleri Telegram (ve/veya ntfy) ile telefona gönderir.
Daha önce gönderilen haberler gorulenler.json içinde tutulur.
"""

import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
import urllib3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
from bs4 import BeautifulSoup

KLASOR = Path(__file__).resolve().parent
SITELER_DOSYASI = KLASOR / "siteler.txt"
KELIMELER_DOSYASI = KLASOR / "anahtar_kelimeler.txt"
GORULENLER_DOSYASI = KLASOR / "gorulenler.json"
RSS_ONBELLEK_DOSYASI = KLASOR / "rss_adresleri.json"
BASLANGIC_DOSYASI = KLASOR / "baslangic_bildirimi.txt"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
NTFY_KONU = os.environ.get("NTFY_TOPIC", "").strip()
WHATSAPP_TEL = os.environ.get("WHATSAPP_PHONE", "").strip()
WHATSAPP_KEY = os.environ.get("WHATSAPP_APIKEY", "").strip()
GOOGLE_HABERLER = os.environ.get("GOOGLE_HABERLER", "1").strip() != "0"

ZAMAN_ASIMI = 20
BASLIK = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "tr-TR,tr;q=0.9",
}
RSS_YOLLARI = [
    "/rss", "/rss.xml", "/feed", "/feed/", "/rss/anasayfa", "/rss/sondakika",
    "/rss/son-dakika", "/rss/news", "/sitemap-news.xml", "/index.rss",
]
GORULEN_SINIRI = 20000
HATALAR: dict[str, str] = {}


# ---------------------------------------------------------------- yardımcılar

def dosya_satirlari(yol: Path) -> list[str]:
    if not yol.exists():
        return []
    satirlar = []
    for satir in yol.read_text(encoding="utf-8").splitlines():
        satir = satir.strip()
        if satir and not satir.startswith("#"):
            satirlar.append(satir)
    return satirlar


def normalize(metin: str) -> str:
    """Türkçe karakterleri sadeleştirip küçük harfe çevirir (İSU = isu = Isu)."""
    metin = metin.replace("İ", "i").replace("I", "ı").lower()
    tablo = str.maketrans("ıçğöşüâîû", "icgosuaiu")
    metin = metin.translate(tablo)
    metin = unicodedata.normalize("NFKD", metin)
    metin = "".join(c for c in metin if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", metin)


def kelime_desenleri(kelimeler: list[str]):
    desenler = []
    for k in kelimeler:
        n = normalize(k)
        # Kelimenin başında sınır şart; sonunda Türkçe ekler serbest (İSU'nun, İSU'dan)
        # Kısa kelimelerde (İSU gibi) tam kelime şart: "Isuzu" eşleşmesin, "İSU'nun" eşleşsin
        son = r"(?![a-z0-9])" if len(n) <= 4 else ""
        desenler.append((k, re.compile(r"(?<![a-z0-9])" + re.escape(n) + son)))
    return desenler


def eslesen_kelimeler(metin: str, desenler) -> list[str]:
    n = normalize(metin)
    return [orijinal for orijinal, d in desenler if d.search(n)]


def anahtar(link: str) -> str:
    p = urlparse(link)
    sade = (p.netloc.replace("www.", "") + p.path).rstrip("/").lower()
    return hashlib.sha1(sade.encode()).hexdigest()[:16]


def site_adresi(site: str) -> str:
    if not site.startswith("http"):
        site = "https://" + site
    return site.rstrip("/")


def getir(url: str) -> requests.Response | None:
    r = _getir(url)
    if r is None:
        p = urlparse(url)
        alternatifler = []
        if p.netloc.startswith("www."):
            alternatifler.append(url.replace("://www.", "://", 1))
        if p.scheme == "https":
            alternatifler.append(url.replace("https://", "http://", 1))
        for alt in alternatifler:
            r = _getir(alt)
            if r is not None:
                break
    return r


def _getir(url: str) -> requests.Response | None:
    try:
        try:
            r = requests.get(url, headers=BASLIK, timeout=ZAMAN_ASIMI)
        except requests.exceptions.SSLError:
            # Sertifikası hatalı haber siteleri: yalnızca herkese açık haber okunur
            r = requests.get(url, headers=BASLIK, timeout=ZAMAN_ASIMI, verify=False)
        if r.status_code == 200 and r.content:
            return r
        HATALAR[urlparse(url).netloc] = f"HTTP {r.status_code}"
    except requests.RequestException as e:
        HATALAR[urlparse(url).netloc] = type(e).__name__
    return None


# ---------------------------------------------------------------- toplama

def rss_bul(site: str) -> list[str]:
    """Sitenin RSS adres(ler)ini bulur."""
    bulunan = []
    ana = getir(site)
    if ana is not None:
        soup = BeautifulSoup(ana.text, "html.parser")
        for l in soup.find_all("link", attrs={"type": re.compile("rss|atom", re.I)}):
            if l.get("href"):
                bulunan.append(urljoin(site + "/", l["href"]))
    if bulunan:
        return bulunan[:3]
    for yol in RSS_YOLLARI:
        r = getir(site + yol)
        if r is not None and re.search(rb"<(rss|feed|urlset)[\s>]", r.content[:2000]):
            return [site + yol]
    return []


def rss_haberleri(rss_url: str) -> list[dict]:
    r = getir(rss_url)
    if r is None:
        return []
    akis = feedparser.parse(r.content)
    haberler = []
    for e in akis.entries:
        link = e.get("link") or ""
        baslik = e.get("title") or ""
        ozet = BeautifulSoup(e.get("summary", "") or "", "html.parser").get_text(" ")
        if link and baslik:
            haberler.append({"baslik": baslik.strip(), "link": link, "metin": f"{baslik} {ozet}"})
    return haberler


def sayfa_haberleri(site: str) -> list[dict]:
    """RSS yoksa ana sayfadaki haber linklerini toplar."""
    r = getir(site)
    if r is None:
        return []
    soup = BeautifulSoup(r.text, "html.parser")
    alan = urlparse(site).netloc.replace("www.", "")
    haberler, gorulen = [], set()
    for a in soup.find_all("a", href=True):
        link = urljoin(site + "/", a["href"]).split("#")[0]
        if alan not in urlparse(link).netloc:
            continue
        baslik = (a.get("title") or a.get_text(" ")).strip()
        if len(baslik) < 15 or link in gorulen:
            continue
        gorulen.add(link)
        haberler.append({"baslik": re.sub(r"\s+", " ", baslik), "link": link, "metin": baslik})
    return haberler


def google_haberleri(kelimeler: list[str]) -> list[dict]:
    """Google Haberler'de 'kelime + Kocaeli' aramasıyla ek kaynak tarar."""
    haberler = []
    for k in kelimeler:
        sorgu = requests.utils.quote(f'"{k}" Kocaeli')
        url = f"https://news.google.com/rss/search?q={sorgu}+when:1d&hl=tr&gl=TR&ceid=TR:tr"
        haberler.extend(rss_haberleri(url))
    return haberler


# ---------------------------------------------------------------- bildirim

def telegram_gonder(mesaj: str) -> bool:
    if not (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID):
        return False
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": mesaj, "parse_mode": "HTML",
                  "disable_web_page_preview": "false"},
            timeout=ZAMAN_ASIMI,
        )
        if r.status_code == 429:
            time.sleep(int(r.json().get("parameters", {}).get("retry_after", 5)) + 1)
            return telegram_gonder(mesaj)
        return r.ok
    except requests.RequestException:
        return False


def ntfy_gonder(baslik: str, link: str, kaynak: str) -> bool:
    if not NTFY_KONU:
        return False
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_KONU}",
            data=f"{baslik}\n{kaynak}".encode("utf-8"),
            headers={"Title": "ISU Haber", "Click": link, "Tags": "newspaper"},
            timeout=ZAMAN_ASIMI,
        )
        return r.ok
    except requests.RequestException:
        return False


def whatsapp_gonder(mesaj: str) -> bool:
    """CallMeBot ücretsiz WhatsApp servisiyle mesaj gönderir."""
    if not (WHATSAPP_TEL and WHATSAPP_KEY):
        return False
    try:
        r = requests.get(
            "https://api.callmebot.com/whatsapp.php",
            params={"phone": WHATSAPP_TEL, "text": mesaj, "apikey": WHATSAPP_KEY},
            timeout=ZAMAN_ASIMI,
        )
        time.sleep(3)  # servis art arda mesajlarda aralık ister
        return r.ok
    except requests.RequestException:
        return False


def html_kacis(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def bildir(haber: dict, kelimeler: list[str]) -> bool:
    kaynak = urlparse(haber["link"]).netloc.replace("www.", "")
    if haber.get("kaynak"):
        kaynak = haber["kaynak"]
    mesaj = (
        f"📰 <b>{html_kacis(haber['baslik'])}</b>\n\n"
        f"🌐 {html_kacis(kaynak)}\n"
        f"🔑 {html_kacis(', '.join(kelimeler))}\n\n"
        f"{haber['link']}"
    )
    wa_mesaj = (f"📰 *{haber['baslik']}*\n\n🌐 {kaynak}\n🔑 {', '.join(kelimeler)}\n\n{haber['link']}")
    a = telegram_gonder(mesaj)
    b = ntfy_gonder(haber["baslik"], haber["link"], kaynak)
    c = whatsapp_gonder(wa_mesaj)
    return a or b or c


# ---------------------------------------------------------------- ana akış

def main() -> int:
    siteler = [site_adresi(s) for s in dosya_satirlari(SITELER_DOSYASI)]
    kelimeler = dosya_satirlari(KELIMELER_DOSYASI)
    if not kelimeler:
        print("anahtar_kelimeler.txt boş.")
        return 1
    desenler = kelime_desenleri(kelimeler)

    ilk_calisma = not GORULENLER_DOSYASI.exists()
    gorulenler: list[str] = [] if ilk_calisma else json.loads(GORULENLER_DOSYASI.read_text())
    gorulen_set = set(gorulenler)

    onbellek: dict = {}
    if RSS_ONBELLEK_DOSYASI.exists():
        onbellek = json.loads(RSS_ONBELLEK_DOSYASI.read_text())

    def site_tara(site: str):
        # RSS adresi daha önce bulunduysa onu kullan; değilse (günde bir) yeniden ara
        kayit = onbellek.get(site)
        if kayit is None or (not kayit["rss"] and time.time() - kayit["zaman"] > 86400):
            kayit = {"rss": rss_bul(site), "zaman": time.time()}
        haberler = []
        for rss in kayit["rss"]:
            haberler.extend(rss_haberleri(rss))
        yontem = "RSS"
        if not haberler:
            if kayit["rss"]:
                kayit = {"rss": [], "zaman": 0}  # RSS bozulmuş; sonraki turda tekrar ara
            haberler = sayfa_haberleri(site)
            yontem = "sayfa"
        if not haberler:
            # Site doğrudan okunamıyorsa Google Haberler'deki son haberlerine bak
            alan = urlparse(site).netloc.replace("www.", "")
            sorgu = requests.utils.quote(f"site:{alan}")
            haberler = rss_haberleri(
                f"https://news.google.com/rss/search?q={sorgu}+when:2d&hl=tr&gl=TR&ceid=TR:tr")
            for h in haberler:
                if " - " in h["baslik"]:
                    h["baslik"], h["kaynak"] = h["baslik"].rsplit(" - ", 1)
            yontem = "Google site:"
        return site, kayit, haberler, yontem

    tum_haberler: list[dict] = []
    with ThreadPoolExecutor(max_workers=16) as havuz:
        for site, kayit, haberler, yontem in havuz.map(site_tara, siteler):
            onbellek[site] = kayit
            durum = "" if haberler else f"  <-- ULAŞILAMADI ({HATALAR.get(urlparse(site).netloc, 'haber linki bulunamadı')})"
            print(f"{site}: {len(haberler)} haber ({yontem}){durum}")
            tum_haberler.extend(haberler)
    RSS_ONBELLEK_DOSYASI.write_text(json.dumps(onbellek, ensure_ascii=False, indent=1))

    if GOOGLE_HABERLER:
        g = google_haberleri(kelimeler)
        for h in g:
            # Google başlıkları "Başlık - Kaynak" biçimindedir
            if " - " in h["baslik"]:
                h["baslik"], h["kaynak"] = h["baslik"].rsplit(" - ", 1)
        print(f"Google Haberler: {len(g)} haber")
        tum_haberler.extend(g)

    gonderilen = 0
    baslik_anahtarlari = set()
    for h in tum_haberler:
        k = anahtar(h["link"])
        bk = "b:" + hashlib.sha1(normalize(h["baslik"]).encode()).hexdigest()[:16]
        if k in gorulen_set or bk in gorulen_set or bk in baslik_anahtarlari:
            continue
        eslesme = eslesen_kelimeler(h["metin"], desenler)
        baslik_anahtarlari.add(bk)
        if eslesme and not ilk_calisma:
            if bildir(h, eslesme):
                gonderilen += 1
                print(f"BİLDİRİM: {h['baslik']} -> {h['link']}")
            else:
                # Gönderilemeyen haber "görüldü" sayılmaz; sonraki taramada tekrar denenir
                print(f"Bildirim gönderilemedi (sonra tekrar denenecek): {h['baslik']}")
                continue
        # Eşleşmeyen veya gönderilen haberleri "görüldü" say; tekrar işlenmesin
        gorulen_set.update([k, bk])
        gorulenler.extend([k, bk])

    if ilk_calisma:
        print("İlk çalışma: mevcut haberler kaydedildi, bildirim gönderilmedi.")
    # Bildirim kanalı ilk kez tanımlandığında bir kez "sistem çalışıyor" mesajı gönder
    kanal_var = bool(NTFY_KONU or (TELEGRAM_TOKEN and TELEGRAM_CHAT_ID) or (WHATSAPP_TEL and WHATSAPP_KEY))
    if kanal_var and not BASLANGIC_DOSYASI.exists():
        BASLANGIC_DOSYASI.write_text(time.strftime("%Y-%m-%d %H:%M"))
        telegram_gonder("✅ İSU haber takip sistemi çalışmaya başladı. "
                        f"{len(siteler)} site ve {len(kelimeler)} anahtar kelime izleniyor.")
        ntfy_gonder("İSU haber takip sistemi çalışmaya başladı. "
                    f"{len(siteler)} site ve {len(kelimeler)} anahtar kelime izleniyor.",
                    "https://github.com/adnansolmaz0-maker/isu-haber--takip", "sistem")
        whatsapp_gonder("✅ İSU haber takip sistemi çalışmaya başladı. "
                        f"{len(siteler)} site ve {len(kelimeler)} anahtar kelime izleniyor.")

    GORULENLER_DOSYASI.write_text(json.dumps(gorulenler[-GORULEN_SINIRI:]))
    print(f"Toplam {len(tum_haberler)} haber tarandı, {gonderilen} bildirim gönderildi.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
