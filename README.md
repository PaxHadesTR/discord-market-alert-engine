# Autonomous Market Price Alert & Discord Bot Engine

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.115+-009688.svg)](https://fastapi.tiangolo.com/)
[![Tests](https://img.shields.io/badge/tests-115%20passed-brightgreen.svg)](tests/)

A high-performance, self-hosted, autonomous price monitoring and notification service designed to bypass TradingView free-tier alert limits and provide institutional-grade alerts on your own infrastructure.

Run your own alert service 24/7 on **Coolify** or Docker. Set alerts for **Crypto (Binance)**, **Borsa Istanbul (BIST)**, **US Equities**, **Forex**, and **Commodities (Gold/Silver/Oil)** directly from **Discord** via modern Slash Commands, or route your **TradingView Webhooks** directly into Discord.

---

## 🇹🇷 Türkiye İçin Kurulum & Discord Erişim Engeli Çözümü (SpoofDPI + Cloudflare WARP Çift Proxy)

Türkiye'de BTK ve mahkeme kararıyla **Discord'a erişim engeli** uygulandığından, Türkiye lokasyonlu sunuculardan (veya yerel WSL/Docker kurulumlarından) `discord.com`'a bağlanmaya çalışıldığında internet servis sağlayıcıların (Superonline, TTNET vb.) DPI engellemesi nedeniyle `SSLCertVerificationError` hatası alınır.

Bu projede Türkiye'deki kullanıcıların tek başına WARP tünel kopmaları yaşamadan, ek hiçbir ücret ödemeden ve VPN kurmadan 7/24 kesintisiz çalışabilmesi için **Çift Proxy Failover (`SpoofDPI` birincil + `Cloudflare WARP` yedek + `Autoheal`)** mimarisi entegre edilmiştir:
- **Birincil (`spoof-dpi`):** VPN/UDP tüneli kurmaz (~10 MB RAM, düşük ping, tünel kopması sıfır). Yalnızca TLS `ClientHello` paketini parçalayarak BTK SNI/DPI engelini doğrudan geçer.
- **Yedek (`warp` + `autoheal`):** ISS kuralları sıkılaşıp `spoof-dpi` bağlantı hatası verirse bot **5 saniye içinde otomatik `http://warp:1080` yedeğine** geçer; `autoheal` donan `warp` konteynerini otomatik yeniden başlatır.

### Nasıl Uygulanır?

#### 1. Coolify ile Kullanıyorsanız (Önerilen Çift Proxy Kurulumu)
Sunucunuzun terminalinde (veya WSL içinde) `spoof-dpi`, sertleştirilmiş `warp` ve `autoheal` konteynerlerini `coolify` ağında başlatın:
```bash
# 1) Birincil: SpoofDPI (Kopmasız HTTP DPI Bypass Proxy)
docker rm -f spoof-dpi
docker run -d \
  --name spoof-dpi \
  --restart always \
  --network coolify \
  --dns 1.1.1.1 \
  --dns 8.8.8.8 \
  ghcr.io/unmedius/spoof-dpi:latest

# 2) Yedek: Sertleştirilmiş Cloudflare WARP Proxy (autoheal etiketli)
docker rm -f warp
docker run -d \
  --name warp \
  --restart always \
  --network coolify \
  --label autoheal=true \
  --device-cgroup-rule='c 10:200 rwm' \
  --cap-add NET_ADMIN \
  --cap-add MKNOD \
  --cap-add AUDIT_WRITE \
  --sysctl net.ipv6.conf.all.disable_ipv6=0 \
  --sysctl net.ipv4.conf.all.src_valid_mark=1 \
  --sysctl net.ipv4.ip_forward=1 \
  --sysctl net.ipv6.conf.all.forwarding=1 \
  --dns 1.1.1.1 \
  --dns 8.8.8.8 \
  -e WARP_SLEEP=8 \
  -e WARP_ENABLE_NAT=1 \
  -v warp_data:/var/lib/cloudflare-warp \
  --health-cmd="curl -fsS --max-time 5 -x http://127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace | grep -Eq 'warp=(on|plus)'" \
  --health-interval=30s \
  --health-timeout=10s \
  --health-retries=3 \
  --health-start-period=20s \
  caomingjun/warp:latest

# 3) Otomatik Kurtarıcı: unhealthy olan warp konteynerini anında restart eder
docker rm -f autoheal
docker run -d \
  --name autoheal \
  --restart always \
  -e AUTOHEAL_CONTAINER_LABEL=autoheal \
  -e AUTOHEAL_INTERVAL=15 \
  -v /var/run/docker.sock:/var/run/docker.sock \
  willfarrell/autoheal:latest
```
*(İsteğe bağlı — Türkiye ISS'lerinde WARP yedeğini MASQUE protokolüne almak için 10sn sonra: `docker exec warp warp-cli tunnel protocol set MASQUE && docker exec warp warp-cli disconnect && docker exec warp warp-cli connect`)*

Ardından Coolify'da **Environment Variables** kısmına şunu ekleyin:
```env
DISCORD_PROXY=http://spoof-dpi:8080,http://warp:1080
DISCORD_PROXY_FALLBACK_DIRECT=false
DISCORD_PROXY_FAILOVER_THRESHOLD=2
```
Konteyneri **Restart** ettiğinizde bot birincil olarak `spoof-dpi` üzerinden bağlanır; herhangi bir kopma/reset durumunda 5 saniyede otomatik `warp` yedeğine geçer.

> **💡 İpucu (Proxy Test & `503 Service Unavailable` Kontrolü):**
> - SpoofDPI testi (`HTTP/2 200` dönmeli): `docker exec warp curl -sI --max-time 8 -x http://spoof-dpi:8080 https://discord.com/api/v10/gateway | head -n 1`
> - WARP tünel testi (`warp=on` veya `warp=plus` yazmalı): `docker exec warp curl -s --max-time 8 -x http://127.0.0.1:1080 https://cloudflare.com/cdn-cgi/trace | grep warp=`
> - WARP tamamen kilitlenirse sıfırlama: `docker rm -f warp && docker volume rm warp_data` ve yukarıdaki `warp` komutuyla yeniden başlatın.

#### 2. Standart Docker Compose ile Kullanıyorsanız
Proje kökündeki `docker-compose.yml` dosyasına `spoof-dpi`, `warp` ve `autoheal` servislerinin tamamı önceden tanımlanmıştır:
```bash
docker compose up -d --build
```
Konteynerler ayağa kalktığında bot otomatik olarak çift proxy failover (`http://spoof-dpi:8080,http://warp:1080`) üzerinden Discord'a bağlanır.

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

### 2. Coolify Ortam Değişkenleri (.env) — Tek Seferlik Tam Yapılandırma
Uygulamanın çalışması için **zorunlu olan tek değişken `DISCORD_BOT_TOKEN`**'dır. Aşağıdaki bloğu Coolify **Environment Variables** (veya `.env`) içerisine tek seferde yapıştırıp tokeninizi girmeniz yeterlidir; sonradan ek bir ayar değişikliği yapmanız gerekmez:

```env
# === ZORUNLU ALAN (Sadece bunu kendi bot tokeninizle değiştirin) ===
DISCORD_BOT_TOKEN=buraya_bot_tokeniniz

# === PROXY & BAĞLANTI DAYANIKLILIĞI (Türkiye / BTK Engeli İçin Önerilen Çift Proxy) ===
# Türkiye dışı (engelsiz Avrupa/ABD) VPS kullanıyorsanız DISCORD_PROXY satırını boş bırakabilirsiniz.
# Birincil SpoofDPI (tünelsiz/kopmasız) + Yedek Cloudflare WARP otomatik failover:
DISCORD_PROXY=http://spoof-dpi:8080,http://warp:1080
# Türkiye sunucularında 'false' önerilir (boşuna engelli direkt SSL denemesi yapmaz).
# Engelsiz VPS'lerde proxy çökünce direkt bağlantıya geçmesi için 'true' yapabilirsiniz.
DISCORD_PROXY_FALLBACK_DIRECT=false
DISCORD_PROXY_FAILOVER_THRESHOLD=2

# === SİSTEM, KOTA VE PERFORMANS (Varsayılan Hazır Değerler) ===
MAX_ACTIVE_ALERTS=100
MAX_USER_ACTIVE_ALERTS=25
ALERT_EXPIRY_DAYS=180
YAHOO_POLL_INTERVAL_SECONDS=20.0
CACHE_TTL_SECONDS=20.0
RATE_LIMIT_PER_SECOND=2.0
MAX_QUEUE_CAPACITY=500
HTTP_TIMEOUT_SECONDS=10.0
SQLITE_DB_PATH=/app/data/alerts.db
LOG_LEVEL=INFO

# === İSTEĞE BAĞLI ALANLAR (Kullanmıyorsanız hiç eklemenize gerek yok) ===
# Tüm alarmları tek sabit kanala yönlendirmek isterseniz ekleyin; boşsa alarm kurulan kanala düşer:
# DISCORD_ALERT_CHANNEL_ID=alarmlarin_duyurulacagi_kanal_id
# Sadece TradingView Webhook (/webhook/...) kullanıyorsanız güçlü bir anahtar tanımlayın;
# boş bırakıldığında bot sorunsuz çalışır, webhook uç noktası güvenlik için kapalı (fail-closed) kalır:
# WEBHOOK_SECRET=buraya_guclu_bir_sifre_veya_token
# DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/id/token
```

| Değişken | Zorunluluk | Ne İşe Yarar? | Önerilen Değer |
| :--- | :--- | :--- | :--- |
| `DISCORD_BOT_TOKEN` | **Zorunlu** | Discord Bot giriş anahtarı | *(Bot tokeniniz)* |
| `DISCORD_PROXY` | TR için önerilen | Çıkış proxy adresi (virgülle yedekli liste destekler) | `http://spoof-dpi:8080,http://warp:1080` *(TR)* / boş *(EU/US)* |
| `DISCORD_PROXY_FALLBACK_DIRECT` | İsteğe bağlı | Proxy 503 verince direkt bağlantı denesin mi? | `false` *(TR)* / `true` *(EU/US)* |
| `DISCORD_PROXY_FAILOVER_THRESHOLD` | İsteğe bağlı | Yedek/fallback öncesi ardışık proxy hata eşiği | `2` |
| `MAX_ACTIVE_ALERTS` / `MAX_USER_ACTIVE_ALERTS` | İsteğe bağlı | Sistem geneli ve kullanıcı başı aktif alarm kotası | `100` / `25` |
| `ALERT_EXPIRY_DAYS` | İsteğe bağlı | Tetiklenmeyen eski alarmların otomatik temizlenme süresi | `180` |
| `YAHOO_POLL_INTERVAL_SECONDS` / `CACHE_TTL_SECONDS` | İsteğe bağlı | Hisse/Emtia/Döviz tarama ve önbellek tazelik süresi (sn) | `20.0` / `20.0` |
| `SQLITE_DB_PATH` | İsteğe bağlı | Kalıcı veritabanı yolu (`/app/data` volume'u ile eşleşmeli) | `/app/data/alerts.db` |
| `DISCORD_ALERT_CHANNEL_ID` | **İsteğe bağlı** | Sabit bildirim kanalı (boşsa `/alert set` yazılan kanala gönderir) | *(Boş bırakılabilir)* |
| `WEBHOOK_SECRET` | **İsteğe bağlı** | Sadece TradingView Webhook kullanıyorsanız gerekli (boşsa webhook kilitli kalır, bot normal çalışır) | *(Webhook kullanmıyorsanız gerekmez)* |
| `DISCORD_WEBHOOK_URL` | **İsteğe bağlı** | Sadece TradingView Webhook'u doğrudan Discord Webhook URL'sine iletmek için | *(Boş bırakılabilir)* |

---

## 🧪 Otomatik Testler

Tüm sistem birim ve entegrasyon testleri:

```bash
python -m pytest tests -v
```

```text
======================== 115 passed ========================
```
Authentication, dual parser, token bucket rate limiter, SQLite işlemleri, watchlist, DM bildirimleri, yüzdesel alarmlar, cache tazeliği, genişletilmiş sembol kataloğu, hibrit canlı arama, borsa öneki ayrıştırması, SpoofDPI + WARP çift proxy failover/fallback koruması, piyasa beslemeleri (Binance/Yahoo), bot komutları ve tetikleyici motoru dahil **115 testin tamamı %100 yeşildir**.

---

## 📄 Lisans (License)

Bu proje [MIT Lisansı](LICENSE) altında açık kaynak olarak lisanslanmıştır. Detaylar için [`LICENSE`](LICENSE) dosyasına göz atabilirsiniz.
