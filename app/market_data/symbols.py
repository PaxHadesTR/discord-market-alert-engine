import asyncio
import re
from typing import Dict, Iterable, List, Optional, Tuple

from app.market_data.symbol_catalog import (
    AUTOCOMPLETE_PRESETS,
    BIST_ALIASES,
    BIST_POPULAR,
    COMMODITY_MAP,
    CRYPTO_SHORTHAND,
    EQUITY_ALIASES,
    FOREX_PAIRS,
    INDEX_MAP,
    KNOWN_EQUITIES,
    YAHOO_CRYPTO_MAP,
)

__all__ = [
    "AUTOCOMPLETE_PRESETS",
    "BIST_ALIASES",
    "BIST_POPULAR",
    "COMMODITY_MAP",
    "CRYPTO_SHORTHAND",
    "EQUITY_ALIASES",
    "FOREX_PAIRS",
    "INDEX_MAP",
    "KNOWN_EQUITIES",
    "YAHOO_CRYPTO_MAP",
    "classify_and_normalize",
    "get_symbol_autocomplete",
    "get_symbol_autocomplete_async",
]

CRYPTO_QUOTE_CURRENCIES: Tuple[str, ...] = ("USDT", "FDUSD", "USDC", "BTC", "ETH", "BNB")

# Explicit exchange prefixes that override heuristic routing
_US_EQUITY_PREFIXES = {"NASDAQ", "NYSE", "AMEX", "ARCA", "US", "STOCK", "ETF", "BATS", "CBOE"}
_BIST_PREFIXES = {"BIST", "IST", "BORSA"}
_CRYPTO_PREFIXES = {"BINANCE", "BINANCEUS", "BYBIT", "OKX", "KUCOIN", "COINBASE", "KRAKEN", "CRYPTO"}
_FOREX_PREFIXES = {"FX", "FOREX", "OANDA", "FXCM"}
_EXCHANGE_SUFFIX_MAP: Dict[str, str] = {
    "XETR": ".DE",
    "FRA": ".DE",
    "EPA": ".PA",
    "EURONEXT": ".PA",
    "LSE": ".L",
    "TSE": ".T",
    "TYO": ".T",
    "HKEX": ".HK",
    "AMS": ".AS",
    "TSX": ".TO",
}

_TURKISH_TRANSLATION = str.maketrans({
    "Ç": "C", "ç": "C",
    "Ğ": "G", "ğ": "G",
    "İ": "I", "ı": "I",
    "Ö": "O", "ö": "O",
    "Ş": "S", "ş": "S",
    "Ü": "U", "ü": "U",
})

_WORD_SPLIT_RE = re.compile(r"[^A-Z0-9]+")


def _normalize_search_text(text: str) -> str:
    """Uppercase and fold Turkish diacritics for forgiving autocomplete matching."""
    return text.strip().translate(_TURKISH_TRANSLATION).upper()


def _truncate_choice(label: str, value: str) -> Tuple[str, str]:
    """Enforce Discord's 100-character limit for autocomplete Choice name and value."""
    return label[:100], value[:100]


def classify_and_normalize(raw_symbol: str) -> Tuple[str, str]:
    """Normalize input symbol and determine whether it routes to Binance WS or Yahoo Finance.

    Supports:
    - Explicit exchange prefixes (e.g., BIST:LINK, NASDAQ:DASH, NYSE:AR, BINANCE:BTC, FX:EURUSD)
    - Slash notation (e.g., BTC/USDT, USD/TRY, EUR/USD, XAU/USD)
    - Indices, futures, bond yields, commodities (EN + TR aliases)
    - Binance Spot cryptos + non-Binance major cryptos via Yahoo Finance
    - Borsa Istanbul (560+ stocks + legacy rebranding aliases)
    - US & Global equities + ETFs (including dot share classes like BRK.B -> BRK-B)

    :param raw_symbol: User inputted symbol (e.g., btcusdt, btc, MU, SPCX, NASDAQ, XU100, xau, THYAO, USDTRY)
    :return: (normalized_symbol, asset_type) where asset_type is 'CRYPTO' or 'EQUITY_FOREX'
    """
    clean = raw_symbol.strip().upper()
    if not clean:
        return "", "EQUITY_FOREX"

    # 0. Handle explicit exchange prefixes (preserves user disambiguation!)
    if ":" in clean:
        prefix, body = clean.split(":", 1)
        prefix = prefix.strip()
        body = body.strip()

        if prefix in _BIST_PREFIXES:
            if body in INDEX_MAP:
                return INDEX_MAP[body], "EQUITY_FOREX"
            if body in BIST_ALIASES:
                return BIST_ALIASES[body], "EQUITY_FOREX"
            if body.endswith(".IS"):
                return body, "EQUITY_FOREX"
            return f"{body}.IS", "EQUITY_FOREX"

        if prefix in _US_EQUITY_PREFIXES:
            mapped = EQUITY_ALIASES.get(body, body)
            return mapped, "EQUITY_FOREX"

        if prefix in _CRYPTO_PREFIXES:
            body_clean = body.replace("/", "")
            if body_clean in CRYPTO_SHORTHAND:
                return CRYPTO_SHORTHAND[body_clean], "CRYPTO"
            for quote in CRYPTO_QUOTE_CURRENCIES:
                if body_clean.endswith(quote) and len(body_clean) > len(quote):
                    return body_clean, "CRYPTO"
            return f"{body_clean}USDT", "CRYPTO"

        if prefix in _FOREX_PREFIXES:
            fx_clean = body.replace("/", "")
            if not fx_clean.endswith("=X"):
                fx_clean = f"{fx_clean}=X"
            return fx_clean, "EQUITY_FOREX"

        if prefix in _EXCHANGE_SUFFIX_MAP:
            suffix = _EXCHANGE_SUFFIX_MAP[prefix]
            if not body.endswith(suffix):
                body = f"{body}{suffix}"
            return body, "EQUITY_FOREX"

        # Unknown prefix (e.g., TV:BTCUSDT) -> strip and continue standard classification
        clean = body

    # Normalize slash notation (e.g., BTC/USDT -> BTCUSDT, USD/TRY -> USDTRY, XAU/USD -> XAUUSD)
    if "/" in clean:
        clean = clean.replace("/", "")

    # 1. Check Major Indices & Treasury Yields/Futures (NASDAQ, XU100, BIST100, SP500, SOX, DXY, etc.)
    if clean in INDEX_MAP:
        return INDEX_MAP[clean], "EQUITY_FOREX"

    # 2. Check Commodity aliases (GOLD, ALTIN, XAU, XAUUSD, SILVER, XAG, BRENT, etc.)
    if clean in COMMODITY_MAP:
        return COMMODITY_MAP[clean], "EQUITY_FOREX"

    # 3. Check Active Binance Crypto Shorthand (e.g. BTC -> BTCUSDT, HYPE -> HYPEUSDT)
    if clean in CRYPTO_SHORTHAND:
        return CRYPTO_SHORTHAND[clean], "CRYPTO"

    # 4. Check Non-Binance Major Cryptos routed to Yahoo Finance (TON, KAS, XMR, CRO, OKB, MNT, BGB)
    if clean in YAHOO_CRYPTO_MAP:
        return YAHOO_CRYPTO_MAP[clean], "EQUITY_FOREX"

    # 5. Check Known US/Global Equities, ETFs & Equity Name Aliases (protects FBTC, GBTC, FETH, MU, SPCX)
    if clean in EQUITY_ALIASES:
        return EQUITY_ALIASES[clean], "EQUITY_FOREX"
    if clean in KNOWN_EQUITIES:
        return clean, "EQUITY_FOREX"

    # 6. Check Forex pairs
    if clean in FOREX_PAIRS:
        return f"{clean}=X", "EQUITY_FOREX"
    if clean.endswith("=X"):
        return clean, "EQUITY_FOREX"

    # 7. Check BIST aliases & BIST symbols (known BIST indices are already handled in INDEX_MAP)
    if clean in BIST_ALIASES:
        return BIST_ALIASES[clean], "EQUITY_FOREX"
    if clean.endswith(".IS"):
        return clean, "EQUITY_FOREX"
    if clean in BIST_POPULAR:
        return f"{clean}.IS", "EQUITY_FOREX"

    # 8. Check Binance Crypto quote patterns (e.g. BTCUSDT, ETHFDUSD, SOLUSDC, ETHBTC, DOGEBTC)
    # Stablecoin quote pairs (4+ char suffix): safe to match any non-empty base
    for quote in ("USDT", "FDUSD", "USDC"):
        if clean.endswith(quote) and len(clean) > len(quote):
            return clean, "CRYPTO"

    # 3-letter crypto quote pairs (BTC, ETH, BNB): require base length >= 2 (avoids 4-letter US ETFs/equities like GBTC, FBTC, FETH, ABNB)
    for quote in ("BTC", "ETH", "BNB"):
        if clean.endswith(quote) and len(clean) >= len(quote) + 2:
            base = clean[: -len(quote)]
            if base in CRYPTO_SHORTHAND or len(base) >= 3:
                return clean, "CRYPTO"

    # 9. Check Futures (=F), Indices (^), or Yahoo Crypto-USD (-USD)
    if clean.endswith("=F") or clean.startswith("^") or clean.endswith("-USD"):
        return clean, "EQUITY_FOREX"

    # Default fallback: Treat as EQUITY_FOREX (Yahoo Finance), supporting US/Global stocks and BIST fallback
    return clean, "EQUITY_FOREX"


# Precompute normalized lookup metadata for sub-millisecond ranked autocomplete
_PresetEntry = Tuple[str, str, str, str, str, str, Tuple[str, ...]]
_INDEXED_PRESETS: List[_PresetEntry] = []

for _label, _val in AUTOCOMPLETE_PRESETS:
    _norm_val = _normalize_search_text(_val)
    # Also compute bare ticker without exchange prefix or USDT suffix for exact-match detection
    _bare_val = _norm_val.split(":", 1)[1] if ":" in _norm_val else _norm_val
    if _bare_val.endswith("USDT") and len(_bare_val) > 4:
        _short_base = _bare_val[:-4]
    else:
        _short_base = _bare_val
    _canonical_target, _ = classify_and_normalize(_val)
    _norm_target = _normalize_search_text(_canonical_target)
    _norm_label = _normalize_search_text(_label)
    _label_words = tuple(w for w in _WORD_SPLIT_RE.split(_norm_label) if w)
    _INDEXED_PRESETS.append((_label, _val, _norm_val, _short_base, _norm_target, _norm_label, _label_words))


def _resolve_alias_target(clean_norm: str) -> Optional[str]:
    """If the user typed a known alias (e.g. SPACEX, ALTIN, KOZAL, US30, FTM), return its canonical target."""
    for mapping in (EQUITY_ALIASES, INDEX_MAP, COMMODITY_MAP, CRYPTO_SHORTHAND, YAHOO_CRYPTO_MAP, BIST_ALIASES):
        if clean_norm in mapping:
            return _normalize_search_text(mapping[clean_norm])
    return None


def _rank_local_presets(
    clean_norm: str,
    live_crypto_symbols: Optional[Iterable[str]] = None,
) -> Tuple[List[Tuple[str, str]], bool]:
    """Return ranked (label, value) matches and whether an exact match was found."""
    alias_norm = _resolve_alias_target(clean_norm)

    tier0: List[Tuple[str, str]] = []  # Exact ticker / exact alias / exact base match
    tier1: List[Tuple[str, str]] = []  # Ticker prefix match
    tier2: List[Tuple[str, str]] = []  # Label word-prefix match (e.g. MICRON, SPACEX, ALTIN)
    tier3: List[Tuple[str, str]] = []  # General substring match

    seen_vals = set()
    has_exact = False

    for label, val, norm_val, short_base, norm_target, norm_label, label_words in _INDEXED_PRESETS:
        bare_val = norm_val.split(":", 1)[1] if ":" in norm_val else norm_val

        if (
            clean_norm == norm_val
            or clean_norm == bare_val
            or clean_norm == short_base
            or (alias_norm is not None and (alias_norm == norm_target or alias_norm == norm_val or alias_norm == bare_val))
        ):
            tier0.append(_truncate_choice(label, val))
            seen_vals.add(val.upper())
            has_exact = True
        elif norm_val.startswith(clean_norm) or bare_val.startswith(clean_norm) or short_base.startswith(clean_norm):
            tier1.append(_truncate_choice(label, val))
            seen_vals.add(val.upper())
        elif any(w.startswith(clean_norm) for w in label_words):
            tier2.append(_truncate_choice(label, val))
            seen_vals.add(val.upper())
        elif clean_norm in norm_val or clean_norm in norm_label:
            tier3.append(_truncate_choice(label, val))
            seen_vals.add(val.upper())

    # Dynamically include any live Binance symbols discovered via WebSocket stream
    if live_crypto_symbols:
        live_exact: List[Tuple[str, str]] = []
        live_prefix: List[Tuple[str, str]] = []
        for raw_sym in live_crypto_symbols:
            sym = raw_sym.upper().strip()
            if not sym or sym in seen_vals:
                continue
            if not sym.endswith(("USDT", "FDUSD", "USDC")):
                continue
            base = sym[:-4] if sym.endswith(("USDT", "USDC")) else sym[:-5]
            if clean_norm == sym or clean_norm == base:
                live_exact.append(_truncate_choice(f"{sym} (Binance Spot Live)", sym))
                seen_vals.add(sym)
                has_exact = True
            elif sym.startswith(clean_norm) or base.startswith(clean_norm):
                live_prefix.append(_truncate_choice(f"{sym} (Binance Spot Live)", sym))
                seen_vals.add(sym)
        tier0.extend(live_exact[:5])
        tier1.extend(sorted(live_prefix)[:10])

    ranked = tier0 + tier1 + tier2 + tier3
    return ranked, has_exact


def get_symbol_autocomplete(
    current: str,
    live_crypto_symbols: Optional[Iterable[str]] = None,
) -> List[Tuple[str, str]]:
    """Return ranked autocomplete choices matching user input (max 25 choices for Discord)."""
    clean_raw = current.strip().upper()
    if not clean_raw:
        return [_truncate_choice(lbl, val) for lbl, val in AUTOCOMPLETE_PRESETS[:25]]

    clean_norm = _normalize_search_text(clean_raw)
    matches, has_exact = _rank_local_presets(clean_norm, live_crypto_symbols=live_crypto_symbols)

    # If user typed a custom ticker not matched as an exact preset, keep custom option at top
    if not has_exact and not any(val.upper() == clean_raw for _, val in matches):
        matches.insert(0, _truncate_choice(f"Özel Sembol: {clean_raw}", clean_raw))

    return matches[:25]


async def get_symbol_autocomplete_async(
    current: str,
    yahoo_feed=None,
    live_crypto_symbols: Optional[Iterable[str]] = None,
    yahoo_timeout: float = 1.2,
) -> List[Tuple[str, str]]:
    """Hybrid autocomplete combining instant local ranked catalog, live Binance WS tickers,
    and an optional fast Yahoo Finance search fallback for any global ticker on Earth.
    """
    clean_raw = current.strip().upper()
    if not clean_raw:
        return [_truncate_choice(lbl, val) for lbl, val in AUTOCOMPLETE_PRESETS[:25]]

    clean_norm = _normalize_search_text(clean_raw)
    local_matches, has_exact = _rank_local_presets(clean_norm, live_crypto_symbols=live_crypto_symbols)

    # Trigger live Yahoo Finance search only when query >= 2 chars and not already an exact curated hit
    # (or very few local suggestions exist)
    yahoo_matches: List[Tuple[str, str]] = []
    if yahoo_feed is not None and len(clean_raw) >= 2 and (not has_exact or len(local_matches) < 5):
        try:
            yahoo_matches = await asyncio.wait_for(
                yahoo_feed.search_symbols(clean_raw, limit=8),
                timeout=yahoo_timeout,
            )
        except Exception:
            yahoo_matches = []

    merged: List[Tuple[str, str]] = []
    seen_vals = set()

    # Exact Yahoo matches first if local didn't have an exact match
    yahoo_exact: List[Tuple[str, str]] = []
    yahoo_rest: List[Tuple[str, str]] = []
    for lbl, val in yahoo_matches:
        val_upper = val.upper()
        bare = val_upper[:-3] if val_upper.endswith(".IS") else val_upper
        if not has_exact and (val_upper == clean_raw or bare == clean_raw):
            yahoo_exact.append(_truncate_choice(lbl, val))
        else:
            yahoo_rest.append(_truncate_choice(lbl, val))

    for group in (yahoo_exact, local_matches, yahoo_rest):
        for lbl, val in group:
            key = val.upper()
            if key not in seen_vals:
                seen_vals.add(key)
                merged.append((lbl, val))

    if not has_exact and not any(
        val.upper() == clean_raw or (val.upper().endswith(".IS") and val.upper()[:-3] == clean_raw)
        for _, val in merged
    ):
        merged.insert(0, _truncate_choice(f"Özel Sembol: {clean_raw}", clean_raw))

    return merged[:25]
