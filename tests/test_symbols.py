import asyncio
from unittest.mock import AsyncMock, MagicMock
import pytest

from app.market_data.symbols import (
    AUTOCOMPLETE_PRESETS,
    BIST_POPULAR,
    CRYPTO_SHORTHAND,
    FOREX_PAIRS,
    KNOWN_EQUITIES,
    classify_and_normalize,
    get_symbol_autocomplete,
    get_symbol_autocomplete_async,
)
from app.market_data.yahoo import YahooFinanceFeed


def test_classify_crypto():
    sym, asset_type = classify_and_normalize("btcusdt")
    assert sym == "BTCUSDT"
    assert asset_type == "CRYPTO"

    sym2, asset_type2 = classify_and_normalize("ETHFDUSD")
    assert sym2 == "ETHFDUSD"
    assert asset_type2 == "CRYPTO"

    sym3, asset_type3 = classify_and_normalize("BTC/USDT")
    assert sym3 == "BTCUSDT"
    assert asset_type3 == "CRYPTO"


def test_classify_crypto_shorthand():
    cases = [
        ("btc", "BTCUSDT"),
        ("eth", "ETHUSDT"),
        ("sol", "SOLUSDT"),
        ("sui", "SUIUSDT"),
        ("pepe", "PEPEUSDT"),
        ("hype", "HYPEUSDT"),
        ("hyperliquid", "HYPEUSDT"),
        ("pump", "PUMPUSDT"),
        ("paxg", "PAXGUSDT"),
        ("xaut", "XAUTUSDT"),
        ("shiba", "SHIBUSDT"),
        ("beam", "BEAMXUSDT"),
        ("sonic", "SUSDT"),
        ("ftm", "SUSDT"),
        ("mkr", "SKYUSDT"),
        ("sky", "SKYUSDT"),
        ("eos", "AUSDT"),
        ("berachain", "BERAUSDT"),
    ]
    for raw, expected in cases:
        sym, asset_type = classify_and_normalize(raw)
        assert sym == expected, f"Failed for {raw}"
        assert asset_type == "CRYPTO", f"Wrong asset_type for {raw}"


def test_classify_yahoo_crypto():
    for raw, expected in [
        ("ton", "TON11419-USD"),
        ("toncoin", "TON11419-USD"),
        ("kas", "KAS-USD"),
        ("kaspa", "KAS-USD"),
        ("xmr", "XMR-USD"),
        ("monero", "XMR-USD"),
        ("cro", "CRO-USD"),
        ("okb", "OKB-USD"),
        ("mnt", "MNT27075-USD"),
    ]:
        sym, asset_type = classify_and_normalize(raw)
        assert sym == expected
        assert asset_type == "EQUITY_FOREX"


def test_classify_indices():
    cases = [
        ("nasdaq", "^NDX"),
        ("nq", "NQ=F"),
        ("dji", "^DJI"),
        ("dow", "^DJI"),
        ("us30", "^DJI"),
        ("xu100", "XU100.IS"),
        ("bist100", "XU100.IS"),
        ("xu050", "XU050.IS"),
        ("xk100", "XK100.IS"),
        ("dxy", "DX-Y.NYB"),
        ("sp500", "^GSPC"),
        ("sox", "^SOX"),
        ("rty", "RTY=F"),
        ("zn", "ZN=F"),
        ("ibex", "^IBEX"),
        ("twii", "^TWII"),
    ]
    for raw, expected in cases:
        sym, asset_type = classify_and_normalize(raw)
        assert sym == expected, f"Failed for {raw}"
        assert asset_type == "EQUITY_FOREX"


def test_classify_bist_and_rebrand_aliases():
    cases = [
        ("THYAO", "THYAO.IS"),
        ("asels.is", "ASELS.IS"),
        ("mavi", "MAVI.IS"),
        ("enjsa", "ENJSA.IS"),
        ("doas", "DOAS.IS"),
        ("despc", "DESPC.IS"),
        ("dgate", "DGATE.IS"),
        ("klser", "KLSER.IS"),
        ("bera", "BERA.IS"),  # Bera Holding must NOT be hijacked by Berachain!
        # Rebranded BIST legacy aliases
        ("kozal", "TRALT.IS"),
        ("kozaa", "TRMET.IS"),
        ("ipeke", "TRENJ.IS"),
        ("qnbfb", "QNBTR.IS"),
        ("kervt", "BESLR.IS"),
        ("despec", "DESPC.IS"),
        ("datagate", "DGATE.IS"),
        ("thy", "THYAO.IS"),
    ]
    for raw, expected in cases:
        sym, asset_type = classify_and_normalize(raw)
        assert sym == expected, f"Failed for {raw}"
        assert asset_type == "EQUITY_FOREX"


def test_classify_forex():
    cases = [
        ("usdtry", "USDTRY=X"),
        ("USD/TRY", "USDTRY=X"),
        ("EURUSD=X", "EURUSD=X"),
        ("EUR/USD", "EURUSD=X"),
        ("gbptry", "GBPTRY=X"),
        ("eurpln", "EURPLN=X"),
        ("usdsar", "USDSAR=X"),
        ("aedtry", "AEDTRY=X"),
    ]
    for raw, expected in cases:
        sym, asset_type = classify_and_normalize(raw)
        assert sym == expected, f"Failed for {raw}"
        assert asset_type == "EQUITY_FOREX"


def test_classify_commodities():
    cases = [
        ("gold", "GC=F"),
        ("xau", "GC=F"),
        ("xauusd", "GC=F"),
        ("XAU/USD", "GC=F"),
        ("brent", "BZ=F"),
        ("wti", "CL=F"),
        ("silver", "SI=F"),
        ("lumber", "LBR=F"),
        ("orangejuice", "OJ=F"),
        ("mgc", "MGC=F"),
    ]
    for raw, expected in cases:
        sym, asset_type = classify_and_normalize(raw)
        assert sym == expected, f"Failed for {raw}"
        assert asset_type == "EQUITY_FOREX"


def test_classify_us_global_equities_and_etfs():
    cases = [
        ("aapl", "AAPL"),
        ("NVDA", "NVDA"),
        ("tsla", "TSLA"),
        ("mu", "MU"),
        ("micron", "MU"),
        ("spcx", "SPCX"),
        ("spacex", "SPCX"),
        ("brk.b", "BRK-B"),
        ("rklb", "RKLB"),
        ("asts", "ASTS"),
        ("oklo", "OKLO"),
        ("ionq", "IONQ"),
        ("smh", "SMH"),
        ("ibit", "IBIT"),
        # Adversarial: Crypto Spot ETFs ending in BTC/ETH must NOT route to Binance!
        ("fbtc", "FBTC"),
        ("gbtc", "GBTC"),
        ("feth", "FETH"),
        ("etha", "ETHA"),
        # Single-letter US stocks must NOT be hijacked by crypto
        ("s", "S"),
        ("c", "C"),
        ("f", "F"),
        ("ai", "AI"),
        # International tickers
        ("rhm.de", "RHM.DE"),
        ("rheinmetall", "RHM.DE"),
        ("mc.pa", "MC.PA"),
        ("lvmh", "MC.PA"),
        ("0700.hk", "0700.HK"),
        ("7203.t", "7203.T"),
    ]
    for raw, expected in cases:
        sym, asset_type = classify_and_normalize(raw)
        assert sym == expected, f"Failed for {raw}: got {sym}"
        assert asset_type == "EQUITY_FOREX", f"Wrong asset_type for {raw}: got {asset_type}"


def test_classify_explicit_exchange_prefixes():
    # Disambiguation for symbols that collide across exchanges
    assert classify_and_normalize("BIST:LINK") == ("LINK.IS", "EQUITY_FOREX")
    assert classify_and_normalize("BIST:THYAO.IS") == ("THYAO.IS", "EQUITY_FOREX")
    assert classify_and_normalize("BIST:KOZAL") == ("TRALT.IS", "EQUITY_FOREX")
    assert classify_and_normalize("NASDAQ:DASH") == ("DASH", "EQUITY_FOREX")
    assert classify_and_normalize("NASDAQ:MU") == ("MU", "EQUITY_FOREX")
    assert classify_and_normalize("NASDAQ:SPCX") == ("SPCX", "EQUITY_FOREX")
    assert classify_and_normalize("NYSE:AR") == ("AR", "EQUITY_FOREX")
    assert classify_and_normalize("NYSE:COMP") == ("COMP", "EQUITY_FOREX")
    assert classify_and_normalize("BINANCE:BTC") == ("BTCUSDT", "CRYPTO")
    assert classify_and_normalize("BINANCE:S") == ("SUSDT", "CRYPTO")
    assert classify_and_normalize("CRYPTO:ETH/USDT") == ("ETHUSDT", "CRYPTO")
    assert classify_and_normalize("FX:EUR/USD") == ("EURUSD=X", "EQUITY_FOREX")
    assert classify_and_normalize("XETR:RHM") == ("RHM.DE", "EQUITY_FOREX")
    assert classify_and_normalize("EPA:MC") == ("MC.PA", "EQUITY_FOREX")
    assert classify_and_normalize("LSE:HSBA") == ("HSBA.L", "EQUITY_FOREX")
    assert classify_and_normalize("TSE:7203") == ("7203.T", "EQUITY_FOREX")
    assert classify_and_normalize("HKEX:0700") == ("0700.HK", "EQUITY_FOREX")
    # Empty input adversarial guard
    assert classify_and_normalize("   ") == ("", "EQUITY_FOREX")


def test_autocomplete_ranking_and_turkish_search():
    # Empty query returns top presets (up to 25)
    default_res = get_symbol_autocomplete("")
    assert len(default_res) == 25

    # Exact matches MU and SPCX MUST appear at position 0 (not buried under GUMUS etc.)
    mu_res = get_symbol_autocomplete("mu")
    assert mu_res[0][1] == "MU"
    assert "Micron" in mu_res[0][0]

    spcx_res = get_symbol_autocomplete("spcx")
    assert spcx_res[0][1] == "SPCX"
    assert "SpaceX" in spcx_res[0][0]

    # Company name / alias search
    spacex_res = get_symbol_autocomplete("spacex")
    assert spacex_res[0][1] == "SPCX"

    micron_res = get_symbol_autocomplete("micron")
    assert micron_res[0][1] == "MU"

    # Turkish diacritic-insensitive search
    gumus_res = get_symbol_autocomplete("gümüş")
    assert any(val == "SILVER" for _, val in gumus_res)

    altin_res = get_symbol_autocomplete("altın")
    assert altin_res[0][1] == "GOLD"

    sise_res = get_symbol_autocomplete("şişe")
    assert any(val == "SISE" for _, val in sise_res)

    # Specific crypto, BIST, index queries
    btc_res = get_symbol_autocomplete("btc")
    assert btc_res[0][1] == "BTCUSDT"

    thyao_res = get_symbol_autocomplete("thyao")
    assert thyao_res[0][1] == "THYAO"

    nasdaq_res = get_symbol_autocomplete("nasdaq")
    assert any("NASDAQ" in val for _, val in nasdaq_res)

    # Dynamic live Binance WebSocket symbols are surfaced automatically
    live_res = get_symbol_autocomplete("NEWCOIN", live_crypto_symbols=["NEWCOINUSDT", "BTCUSDT"])
    assert live_res[0][1] == "NEWCOINUSDT"

    # Custom ticker not in presets returns Custom ticker as 1st option
    custom_res = get_symbol_autocomplete("CUSTOMTICKER123")
    assert custom_res[0][1] == "CUSTOMTICKER123"


def test_us_etfs_and_exchange_prefixes():
    """LOGIC-SYMBOLS-003: US ETFs/equities are not misclassified as crypto/BIST and exchange prefixes are respected."""
    assert classify_and_normalize("GBTC") == ("GBTC", "EQUITY_FOREX")
    assert classify_and_normalize("FBTC") == ("FBTC", "EQUITY_FOREX")
    assert classify_and_normalize("FETH") == ("FETH", "EQUITY_FOREX")
    assert classify_and_normalize("ABNB") == ("ABNB", "EQUITY_FOREX")
    assert classify_and_normalize("XBI") == ("XBI", "EQUITY_FOREX")
    assert classify_and_normalize("BIST:LINK") == ("LINK.IS", "EQUITY_FOREX")
    assert classify_and_normalize("BIST:THYAO.IS") == ("THYAO.IS", "EQUITY_FOREX")
    assert classify_and_normalize("BINANCE:BTC") == ("BTCUSDT", "CRYPTO")
    assert classify_and_normalize("ETHBTC") == ("ETHBTC", "CRYPTO")
    assert classify_and_normalize("DOGEBTC") == ("DOGEBTC", "CRYPTO")


@pytest.mark.asyncio
async def test_autocomplete_async_yahoo_fallback_and_resilience():
    # 1. Primary path: Unknown ticker triggers Yahoo search fallback and surfaces exact match at top
    mock_yahoo = MagicMock()
    mock_yahoo.search_symbols = AsyncMock(return_value=[("XYZCORP (XYZ Corporation • NASDAQ)", "XYZCORP")])

    results = await get_symbol_autocomplete_async("xyzcorp", yahoo_feed=mock_yahoo)
    assert results[0][1] == "XYZCORP"
    assert "XYZ Corporation" in results[0][0]
    mock_yahoo.search_symbols.assert_awaited_once()

    # 2. Fast-path: Exact curated match with >=5 local results skips network search completely
    mock_yahoo_fast = MagicMock()
    mock_yahoo_fast.search_symbols = AsyncMock(return_value=[])
    fast_results = await get_symbol_autocomplete_async("MU", yahoo_feed=mock_yahoo_fast)
    assert fast_results[0][1] == "MU"
    mock_yahoo_fast.search_symbols.assert_not_awaited()

    # 3. Adversarial path: Yahoo search hangs past timeout -> gracefully falls back without raising
    async def slow_search(*args, **kwargs):
        await asyncio.sleep(5)
        return []

    mock_yahoo_slow = MagicMock()
    mock_yahoo_slow.search_symbols = slow_search
    fallback_res = await get_symbol_autocomplete_async(
        "SLOWTICKER99",
        yahoo_feed=mock_yahoo_slow,
        yahoo_timeout=0.05,
    )
    assert fallback_res[0][1] == "SLOWTICKER99"


@pytest.mark.asyncio
async def test_yahoo_search_symbols_caching_and_bounds():
    feed = YahooFinanceFeed(search_cache_ttl_seconds=60.0, search_cache_max_size=2)

    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.json.return_value = {
        "quotes": [
            {"symbol": "SPCX", "shortname": "Space Exploration Technologies", "exchDisp": "NASDAQ", "quoteType": "EQUITY"},
            {"symbol": "IGNORED", "shortname": "News Item", "quoteType": "OPTION"},
        ]
    }
    mock_client = MagicMock()
    mock_client.is_closed = False
    mock_client.get = AsyncMock(return_value=mock_response)
    feed._client = mock_client

    # First call hits network and filters out OPTION
    res1 = await feed.search_symbols("spcx")
    assert len(res1) == 1
    assert res1[0][1] == "SPCX"
    assert mock_client.get.await_count == 1

    # Second call serves from cache (no second HTTP call)
    res2 = await feed.search_symbols("SPCX")
    assert res2 == res1
    assert mock_client.get.await_count == 1

    # Bounded LRU eviction check (max_size=2)
    await feed.search_symbols("AA")
    await feed.search_symbols("BB")
    assert len(feed._search_cache) == 2
    assert "SPCX" not in feed._search_cache

    # Adversarial network error returns [] cleanly
    mock_client.get = AsyncMock(side_effect=RuntimeError("network down"))
    err_res = await feed.search_symbols("FAILQUERY")
    assert err_res == []


def test_catalog_integrity_invariants():
    # 1. No duplicate values in AUTOCOMPLETE_PRESETS
    vals = [val.upper() for _, val in AUTOCOMPLETE_PRESETS]
    assert len(vals) == len(set(vals)), f"Duplicate preset values: {[v for v in vals if vals.count(v) > 1]}"

    # 2. Discord Choice 100-char limit strictly respected
    for label, val in AUTOCOMPLETE_PRESETS:
        assert 1 <= len(label) <= 100, f"Invalid label length: {label}"
        assert 1 <= len(val) <= 100, f"Invalid value length: {val}"

    # 3. No unintended collisions between BIST_POPULAR and KNOWN_EQUITIES
    assert (BIST_POPULAR & KNOWN_EQUITIES) == set()

    # 4. Only explicitly documented intentional collision between CRYPTO_SHORTHAND and BIST_POPULAR (LINK)
    bist_crypto_overlap = set(CRYPTO_SHORTHAND.keys()) & BIST_POPULAR
    assert bist_crypto_overlap == {"LINK"}, f"Unexpected BIST/Crypto overlap: {bist_crypto_overlap}"

    # 5. Zero collisions between CRYPTO_SHORTHAND and KNOWN_EQUITIES
    equity_crypto_overlap = set(CRYPTO_SHORTHAND.keys()) & KNOWN_EQUITIES
    assert equity_crypto_overlap == set(), f"Unexpected Equity/Crypto overlap: {equity_crypto_overlap}"

    # 6. All CRYPTO_SHORTHAND targets must end with USDT
    for k, target in CRYPTO_SHORTHAND.items():
        assert target.endswith("USDT"), f"CRYPTO_SHORTHAND[{k}] -> {target} does not end with USDT"

    # 7. Catalog scale sanity assertions
    assert len(BIST_POPULAR) >= 500
    assert len(KNOWN_EQUITIES) >= 350
    assert len(CRYPTO_SHORTHAND) >= 200
    assert len(FOREX_PAIRS) >= 80
    assert len(AUTOCOMPLETE_PRESETS) >= 500
