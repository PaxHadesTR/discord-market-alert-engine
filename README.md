# Autonomous Market Price Alert & Discord Bot Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-108%20passed-brightgreen.svg)](tests/)

A high-performance, self-hosted, autonomous price monitoring and notification service designed to bypass TradingView free-tier alert limits and provide institutional-grade alerts on your own infrastructure.

Run your own alert service 24/7 on **Coolify** or Docker. Set alerts for **Crypto (Binance)**, **Borsa Istanbul (BIST)**, **US Equities**, **Forex**, and **Commodities (Gold/Silver/Oil)** directly from **Discord** via modern Slash Commands, or route your **TradingView Webhooks** directly into Discord.

---

## 🇹🇷 Türkiye İçin Kurulum & Discord Erişim Engeli Çözümü (Cloudflare WARP)

Türkiye'de BTK ve mahkeme kararıyla **Discord'a erişim engeli** uygulandığından, Türkiye lokasyonlu sunuculardan (veya yerel WSL/Docker kurulumlarından) `discord.com`'a bağlanmaya çalışıldığında internet servis sağlayıcıların (Superonline, TTNET vb.) DPI engellemesi nedeniyle `SSLCertVerificationError` hatası alınır.

Bu projede Türkiye'deki kullanıcıların ek hiçbir ücret ödemeden ve VPN kurmak zorunda kalmadan Discord botunu çalıştırabilmesi için **Cloudflare WARP Sidecar / Proxy** desteği entegre edilmiştir.

### Nasıl Uygulanır?

#### 1. Coolify ile Kullanıyorsanız (Önerilen Sağlam Kurulum)
Sunucunuzun terminalinde (veya WSL içinde) Cloudflare WARP konteynerini gerekli çekirdek ağ yetkileri ve kalıcı oturum hacmiyle başlatın:
```bash
docker rm -f warp
docker run -d \
  --name warp \
  --restart always \
  --network coolify \
  --cap-add NET_ADMIN \
  --cap-add MKNOD \
  --cap-add AUDIT_WRITE \
  --sysctl net.ipv6.conf.all.disable_ipv6=0 \
  --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  -e WARP_SLEEP=5 \
  -v warp_data:/var/lib/cloudflare-warp \
  caomingjun/warp:latest
```
Ardından Coolify'da **Environment Variables** kısmına şunu ekleyin:
```env
DISCORD_PROXY=http://warp:1080
```
Konteyneri **Restart** ettiğinizde bot engeli tamamen aşarak Discord'a bağlanacaktır.

> **💡 İpucu (Proxy Durum Kontrolü):**
> Bağlantı durumunu kontrol etmek için: `docker exec warp warp-cli status` (Connected yazmalıdır).
> Eğer nadiren bağlantı koparsa: `docker exec warp warp-cli connect` komutuyla hemen yeniden bağlayabilirsiniz.

#### 2. Standart Docker Compose ile Kullanıyorsanız
Proje kökündeki `docker-compose.yml` dosyasına `warp` servisi zaten eklenmiştir:
```bash
docker compose up -d --build
```
Konteynerler ayağa kalktığında bot otomatik olarak `warp` proxy'si üzerinden Discord'a bağlanır.

---

## 🌟 Öne Çıkan Özellikler (Key Features)

1. **TradingView Plan Limitlerine Son**:
   - Ücretsiz TradingView hesaplarındaki 1 alarm sınırına takılmadan, kendi sunucunuzda sınırsız sayıda alarm kurun ve takip edin.
2. **Hibrit Gerçek Zamanlı Piyasa Beslemesi (Hybrid Feeds)**:
   - **Binance WebSocket Stream (`!miniTicker@arr`)**: Tüm kripto çiftleri için milisaniyelik canlı fiyat akışı (API key gerektirmez).
   - **Yahoo Finance Async Poller**: Kripto dışı varlıklar (BIST `.IS`, ABD Hisseleri, Döviz `=X`, Altın/Petrol Vadeli `GC=F`, `BZ=F`, Endeksler) için 20 saniyelik periyodik canlı tarama.
   - **In-Memory Freshness Cache (20s TTL)**: Fiyat sorgularında eski/bayat fiyat gösterimini önleyen akıllı önbellek mekanizması.
3. **Gelişmiş Discord Slash Komutları & Kullanıcı Deneyimi**:
   - `/alert set`: Hedef fiyata veya **yüzdesel değişime** (`+5%`, `-3%`, `150,5`, `70k`) göre alarm kurma. Otomatik yön tayini (`ABOVE` / `BELOW`).
   - `/alert list`: Canlı fiyatlar, hedefe kalan yüzde mesafesi ve tek tıkla silme sağlayan açılır menü (Dropdown).
   - `/alert clear`: Tetiklenmiş veya tüm alarmları tek komutla temizleme.
   - `/alert delete`: ID ile alarm iptali.
   - `/price`: Herhangi bir varlığın anlık piyasa değerini sorgulama.
   - `/watchlist show`: Kişisel portföyünüzün anlık değerlerini tek ekranda görme.
   - `/watchlist add` & `/watchlist remove`: Portföy yönetimi.
   - `/help`: Kategori bazlı interaktif yardım menüsü (Genel Rehber, Komutlar, Semboller, TradingView Kurulumu).
   - `/stats`: Bot gecikmesi (ping), çalışma süresi (uptime), aktif alarmlar ve piyasa besleme sağlığı.
4. **İnteraktif Butonlar & Modallar**:
   - Alarm tetiklendiğinde mesaj altında **"🔄 Yeniden Kur"** ve **"🗑 Kapat"** butonları belirir.
   - "Yeniden Kur" butonuna tıklandığında açılan Discord Modal penceresi üzerinden yeni hedef fiyat veya yüzde girilerek alarm anında güncellenebilir.
5. **Kişisel & Sistem Kota Koruması**:
   - Sunucu kaynaklarını korumak için sistem genelinde (`MAX_ACTIVE_ALERTS=100`) ve kullanıcı başına (`MAX_USER_ACTIVE_ALERTS=25`) kota koruması.
   - Unutulan eski alarmlar için otomatik son kullanma temizleyicisi (`ALERT_EXPIRY_DAYS=180`).
6. **Esnek TradingView Webhook Köprüsü**:
   - TradingView Pine Script veya alarm mesajlarını (`/webhook/{token}`) üzerinden karşılar.
   - Eğer `DISCORD_WEBHOOK_URL` tanımlanmamışsa, TradingView sinyallerini otomatik olarak aktif Discord Botu üzerinden hedeflenen kanala iletir!
7. **Dayanıklı SQLite Veritabanı**:
   - `aiosqlite` ile WAL modunda çalışan (`alerts.db`), konteyner güncellemelerinde veri kaybını önleyen kalıcı depolama.

---

## 🎮 Discord Bot Komut Tablosu

| Komut | Açıklama | Örnek Kullanım |
| :--- | :--- | :--- |
| `/alert set <symbol> <target> [note] [dm]` | Yeni fiyat alarmı kurar (fiyat veya yüzde kabul eder) | `/alert set symbol:BTC target_price:+5% note:Direnç dm:True` |
| `/alert list [status]` | Alarmlarınızı canlı fiyat ve kalan fark % ile listeler | `/alert list status:Aktif Alarmlar` |
| `/alert clear [scope]` | Tetiklenmiş veya tüm alarmlarınızı topluca temizler | `/alert clear scope:Sadece Tetiklenmiş Alarmları Temizle` |
| `/alert delete <alert_id>` | Belirli bir alarmı ID ile iptal eder | `/alert delete alert_id:4` |
| `/price <symbol>` | Anlık fiyat sorgular (Autocomplete destekli) | `/price symbol:GOLD` veya `/price symbol:THYAO` |
| `/watchlist show` | Kişisel takip listenizdeki tüm varlıkların anlık fiyatlarını gösterir | `/watchlist show` |
| `/watchlist add <symbol>` | Takip listenize yeni bir sembol ekler | `/watchlist add symbol:SOL` |
| `/watchlist remove <symbol>`| Takip listenizden bir sembolü kaldırır | `/watchlist remove symbol:SOL` |
| `/help` | İnteraktif açılır menülü detaylı yardım rehberi | `/help` |
| `/stats` | Sistem gecikmesi (ping), uptime ve alarm istatistikleri | `/stats` |

---

## 📈 Desteklenen Semboller ve Yazım Formatları

Sistem kullanıcı dostu akıllı sembol ayrıştırıcısına (`symbols.py` & `symbol_catalog.py`) ve **775+ hazır öneri + canlı Binance/Yahoo Finance araması** sunan hibrit Discord otomatik tamamlama motoruna sahiptir:

* **🪙 Kripto Paralar (Binance Spot + Yahoo Finance):** `BTC`, `ETH`, `SOL`, `HYPE`, `PEPE`, `SUI`, `AVAX`, `DOGE`, `XRP`, `RENDER`, `TAO`, `NEAR`, `PAXG`, `XAUT`, `PUMP`, `ARB`, `OP`, `TIA`, `INJ`, `TON`, `KAS`, `XMR` vb. (320+ canlı doğrulanmış kısaltma ve tüm 500+ Binance USDT paritesi canlı desteklenir).
* **🇺🇸 ABD & Global Hisseler + ETF'ler (390+ Doğrulanmış Varlık):**
  * **Teknoloji, Yarı İletken, Yapay Zeka & Uzay:** `NVDA`, `MU` (Micron), `SPCX` (SpaceX), `AAPL`, `TSLA`, `MSFT`, `AMZN`, `GOOGL`, `META`, `AVGO`, `AMD`, `INTC`, `TSM`, `ASML`, `ARM`, `PLTR`, `ORCL`, `CRWD`, `SNDK`, `NBIS`, `CRWV`, `RKLB`, `ASTS`, `OKLO`, `IONQ`, `QBTS`, `RGTI`, `COIN`, `MSTR`, `CRCL`, `HOOD`
  * **ETF'ler:** `SPY`, `VOO`, `QQQ`, `TQQQ`, `SQQQ`, `SMH`, `SOXX`, `SOXL`, `SOXS`, `SPCH`, `SSPC`, `MUU`, `IBIT`, `FBTC`, `ETHA`, `GLD`, `SLV`, `TLT`, `SHLD`, `ITA`, `ARKX`, `URA`, `TUR`
  * **Avrupa & Asya Devleri:** `RHM.DE`, `SIE.DE`, `MC.PA`, `AIR.PA`, `ASML.AS`, `NOVO-B.CO`, `HSBA.L`, `7203.T`, `0700.HK`, `9988.HK`, `BABA`, `PDD`, `NIO`, `BYDDY`
* **🇹🇷 Borsa İstanbul (570+ Doğrulanmış BIST Hissesi):** `THYAO`, `ASELS`, `GARAN`, `EREGL`, `SISE`, `KCHOL`, `TUPRS`, `DOAS`, `ASTOR`, `TRALT` (`KOZAL`), `TRMET` (`KOZAA`), `QNBTR` (`QNBFB`), `REEDR`, `MIATK`, `EUPWR`, `CCOLA`, `MAVI`, `BIMAS`, `SDTTR`, `ALTNY` vb. (Sonuna `.IS` yazmanız gerekmez; eski hisse kodları otomatik yeni kodlara yönlendirilir).
* **🏆 Emtialar (75+ Türkçe/İngilizce Eşleme):**
  * `GOLD`/`ALTIN` (`GC=F`), `MGC`, `SILVER`/`GUMUS` (`SI=F`), `PLATINUM`/`PLATIN` (`PL=F`), `PALLADIUM` (`PA=F`), `COPPER`/`BAKIR` (`HG=F`), `ALUMINUM` (`ALI=F`)
  * `BRENT` (`BZ=F`), `OIL`/`WTI` (`CL=F`), `NATGAS`/`DOGALGAZ` (`NG=F`), `GASOLINE`/`BENZIN` (`RB=F`), `HEATINGOIL` (`HO=F`)
  * `WHEAT`/`BUGDAY`, `CORN`/`MISIR`, `SOYBEAN`, `COFFEE`/`KAHVE`, `SUGAR`/`SEKER`, `COTTON`/`PAMUK`, `COCOA`/`KAKAO`, `LUMBER`/`KERESTE`, `ORANGEJUICE`/`PORTAKAL`, `OATS`, `RICE`, `LIVECATTLE`
* **🌐 Dünya & BIST Endeksleri + Tahvil Faizleri/Vadelileri (150+ Eşleme):**
  * `NASDAQ` (`^NDX`), `NQ`/`MNQ`, `SP500`/`SPX` (`^GSPC`), `ES`/`MES`, `DJI`/`US30` (`^DJI`), `YM`, `RUSSELL` (`^RUT`), `RTY`, `SOX` (`^SOX` Yarı İletken Endeksi)
  * `DAX`/`GER40`, `FTSE`/`UK100`, `CAC40`, `EUROSTOXX`, `IBEX`, `FTSEMIB`, `AEX`, `SMI`, `NIKKEI`, `HANGSENG`, `KOSPI`, `TWII`, `NIFTY`, `ASX200`, `BOVESPA`
  * `DXY` (Dolar Endeksi), `VIX` (Korku Endeksi), `US10Y`, `US30Y`, `US05Y`, `US02Y`, `ZN`, `ZB`
  * `XU100`, `XU050`, `XU030`, `XUTUM`, `XBANK`, `XUSIN`, `XUMAL`, `XUHIZ`, `XULAS`, `XK100`, `XK030`, `XUTEK`, `XBLSM`
* **💱 Döviz & Forex Kurları (84+ Doğrulanmış Çift):** `USDTRY`, `EURTRY`, `GBPTRY`, `CHFTRY`, `AEDTRY`, `EURUSD`, `GBPUSD`, `USDJPY`, `EURPLN`, `USDSAR` vb. (`USD/TRY` gibi eğik çizgili yazımlar da desteklenir).
* **🔀 Borsa Öneki & Canlı Global Arama:** Çakışan sembollerde borsa öneki kullanabilirsiniz (`BIST:LINK`, `NASDAQ:DASH`, `NYSE:AR`, `BINANCE:S`, `XETR:RHM`). Hazır listede olmayan herhangi bir hisse/ETF yazdığınızda Discord otomatik tamamlama anlık olarak Yahoo Finance üzerinden canlı arama yapar.

---

## 🔔 TradingView Webhook Kurulumu

TradingView grafik veya Pine Script stratejilerinizden botunuza sinyal göndermek için:

1. **Webhook URL:**
   ```text
   https://sizin-coolify-domaininiz.com/webhook/GIZLI_TOKENINIZ
   ```
2. **Alarm Mesajı (JSON Formatı):**
   ```json
   {
     "ticker": "{{ticker}}",
     "action": "{{strategy.order.action}}",
     "price": {{close}},
     "strategy": "Trend Takip 15m",
     "timeframe": "{{interval}}",
     "message": "Kesişim sinyali onaylandı!"
   }
   ```
3. **Düz Metin Formatı:**
   Dilerseniz TradingView mesajına doğrudan `BTCUSDT BUY 68500 Direnç Kırılımı` da yazabilirsiniz.

---

## 🚀 Coolify & Docker Dağıtımı

### 1. Discord Bot Token Alma
1. [Discord Developer Portal](https://discord.com/developers/applications) adresine gidin.
2. **New Application** -> **Bot** sekmesine geçin *(Bot yalnızca Slash Komutları ve `discord.Intents.default()` ile çalıştığından herhangi bir Privileged Gateway Intent açmanız gerekmez)*.
3. **Reset Token** butonuna basarak bot tokeninizi kopyalayın (`DISCORD_BOT_TOKEN`).
4. **OAuth2 -> URL Generator** kısmında `bot` ve `applications.commands` seçin. İzin olarak `Send Messages`, `Embed Links`, `Attach Files`, `Read Message History` verin.
5. Üretilen davet linkiyle botu sunucunuza ekleyin.

### 2. Coolify Ortam Değişkenleri (.env)
Coolify üzerinde aşağıdaki değişkenleri tanımlayın:

```env
# Bot ve Kanal Ayarları
DISCORD_BOT_TOKEN=buraya_bot_tokeniniz
DISCORD_ALERT_CHANNEL_ID=alarmlarin_duyurulacagi_kanal_id
DISCORD_PROXY=http://warp:1080

# Sistem ve Kullanıcı Kotaları
MAX_ACTIVE_ALERTS=100
MAX_USER_ACTIVE_ALERTS=25
ALERT_EXPIRY_DAYS=180
CACHE_TTL_SECONDS=20.0
SQLITE_DB_PATH=/app/data/alerts.db

# TradingView Webhook Ayarları (Webhook uç noktası için güçlü bir değer zorunludur — fail-closed)
WEBHOOK_SECRET=buraya_guclu_bir_sifre_veya_token
# DISCORD_WEBHOOK_URL boş bırakılırsa, TradingView sinyalleri bot üzerinden DISCORD_ALERT_CHANNEL_ID'ye gönderilir!
```

---

## 🧪 Otomatik Testler

Tüm sistem birim ve entegrasyon testleri:

```bash
python -m pytest tests -v
```

```text
======================== 108 passed ========================
```
Authentication, dual parser, token bucket rate limiter, SQLite işlemleri, watchlist, DM bildirimleri, yüzdesel alarmlar, cache tazeliği, genişletilmiş sembol kataloğu, hibrit canlı arama, borsa öneki ayrıştırması, piyasa beslemeleri (Binance/Yahoo), bot komutları ve tetikleyici motoru dahil **108 testin tamamı %100 yeşildir**.

---

## 📄 Lisans (License)

Bu proje [MIT Lisansı](LICENSE) altında açık kaynak olarak lisanslanmıştır. Detaylar için [`LICENSE`](LICENSE) dosyasına göz atabilirsiniz.
