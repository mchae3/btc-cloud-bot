"""
BTC Quant Lab - Paper Trading Engine (Model C: 40/40 Hybrid + 200 SMA Regime Switch)
100% Pure Python Standard Library (Python 3.9+ compatible)
Strategy: 일봉 40/40 SMA + 200일선 체제전환 + 거래량(강세 1.2x·20일 / 약세 1.8x·5일) 필터
"""
import json
import math
import os
import ssl
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

TORONTO_TZ = ZoneInfo("America/Toronto")
BAR_5M_MS = 300_000

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "telegram_config.json"
STATE_PATH = BASE_DIR / "paper_trading_state.json"

DEFAULT_CONFIG = {
    "bot_token": "",
    "chat_id": "",
    "strategy_name": "daily_sma40_hybrid",
    "bull_sma_len": 40,
    "bear_sma_len": 40,
    "regime_sma_len": 200,
    "bull_vol_mult": 1.2,
    "bull_vol_ma": 20,
    "bear_vol_mult": 1.8,
    "bear_vol_ma": 5,
    "exit_confirm_days": 1,       # 청산 이탈 확인 일수 (1=기존 즉시, 2=2일 연속 이탈 확인)
    "trend_kernel": "sma",        # 추세 커널 ('sma'=단일 SMA, 'sma_donchian'=SMA+돈치안 중앙값 50:50)
    "vol_target_pct": 0.0,        # 20일 실현 변동성 목표 연율 % (0=비활성 고정100%, 75=v3 에어백)
    "vol_lookback": 20,           # 실현 변동성 계산 일수
    "rebal_deadband_pct": 15.0,   # 비중 리밸런싱 데드밴드 (%)
    # Legacy fallback keys (used only if strategy_name is explicitly non-hybrid)
    "strategy_sma": 40,
    "volume_multiplier": 1.2,
    "volume_ma_len": 20,
    "initial_seed_usdt": 10000.0,
    "fee_rate": 0.0015,           # 0.15% spot taker fee
    "slippage_rate": 0.0005,      # 0.05% QIS-7 adverse slippage
    "daily_check_hour_utc": 0,    # UTC 00:00 (Korea 09:00 AM / Toronto 19:00 EST)
    "enable_daily_report": True,
    "touch_alert_enabled": True,  # 40일선/200일선 터치 감지 알림
    "touch_threshold_pct": 0.5,   # 오차 0.5% 이내 근접 시 터치 판정
    "touch_cooldown_sec": 7200,   # 같은 선 터치 알림 쿨다운 (2시간)
    "dip_alert_enabled": True,    # 현금대기 중 급락 반등 알림
    "dip_alert_cash_only": True,  # 모델 C 현금(CASH) 대기 상태일 때만 급락 알림 발송
    "dip_alert_bear_only": False, # True면 200일선 이하(약세장) 현금 구간에서만 단타 진입
    "dip_alert_lookback_bars": 24,  # 24 × 5분 = 2시간 (v2/v3: 288 = 24시간)
    "dip_alert_drop_pct": 4.5,    # 급락 기준 % (v2/v3: 10.0)
    "dip_alert_vol_mult": 2.5,    # 직전 14일 평균 대비 투매 거래량 배수 필터 (1.0=미사용, v2/v3: 1.5)
    "dip_alert_hold_bars": 24,    # 보유 봉 수 (v2/v3: 288 = 24시간)
    "dip_alert_cooldown_sec": 7200  # 급락 알림 중복 방지 쿨다운 (v2/v3: 86400 = 24시간)
}

STRATEGY_PRESETS = {
    "v3": {
        "label": "👑 v3 메인 추천 (듀얼커널 SMA40+Donch40 · Vol75% · 2일확인 + 24h -10% · 1.5x · 약세전용)",
        "trend_kernel": "sma_donchian",
        "vol_target_pct": 75.0,
        "vol_lookback": 20,
        "rebal_deadband_pct": 15.0,
        "exit_confirm_days": 2,
        "dip_alert_bear_only": True,
        "dip_alert_lookback_bars": 288,
        "dip_alert_drop_pct": 10.0,
        "dip_alert_vol_mult": 1.5,
        "dip_alert_hold_bars": 288,
        "dip_alert_cooldown_sec": 86400,
    },
    "v3_airbag": {
        "label": "🛡️ v3 에어백 (모델C SMA40 · Vol75% · 2일확인 + 24h -10% · 1.5x · 약세전용)",
        "trend_kernel": "sma",
        "vol_target_pct": 75.0,
        "vol_lookback": 20,
        "rebal_deadband_pct": 15.0,
        "exit_confirm_days": 2,
        "dip_alert_bear_only": True,
        "dip_alert_lookback_bars": 288,
        "dip_alert_drop_pct": 10.0,
        "dip_alert_vol_mult": 1.5,
        "dip_alert_hold_bars": 288,
        "dip_alert_cooldown_sec": 86400,
    },
    "v2": {
        "label": "🚀 v2 메인 추천 (고정100% · 모델C 2일확인 + 24h -10% · 1.5x · 24h · 약세전용)",
        "trend_kernel": "sma",
        "vol_target_pct": 0.0,
        "exit_confirm_days": 2,
        "dip_alert_bear_only": True,
        "dip_alert_lookback_bars": 288,
        "dip_alert_drop_pct": 10.0,
        "dip_alert_vol_mult": 1.5,
        "dip_alert_hold_bars": 288,
        "dip_alert_cooldown_sec": 86400,
    },
    "v2_2h": {
        "label": "⚡ v2 고수익 2h (모델C 2일확인 + 2h -4.5% · 2.5x · 2h · 약세전용)",
        "trend_kernel": "sma",
        "vol_target_pct": 0.0,
        "exit_confirm_days": 2,
        "dip_alert_bear_only": True,
        "dip_alert_lookback_bars": 24,
        "dip_alert_drop_pct": 4.5,
        "dip_alert_vol_mult": 2.5,
        "dip_alert_hold_bars": 24,
        "dip_alert_cooldown_sec": 7200,
    },
    "v1": {
        "label": "📌 v1 기존 원본 (모델C 1일즉시 + 2h -4.5% · 2.5x · 2h · 전체)",
        "trend_kernel": "sma",
        "vol_target_pct": 0.0,
        "exit_confirm_days": 1,
        "dip_alert_bear_only": False,
        "dip_alert_lookback_bars": 24,
        "dip_alert_drop_pct": 4.5,
        "dip_alert_vol_mult": 2.5,
        "dip_alert_hold_bars": 24,
        "dip_alert_cooldown_sec": 7200,
    },
}


def apply_strategy_preset(config, preset_name):
    """Apply a verified strategy preset (v3, v3_airbag, v2, v2_2h, v1) onto config dict without touching credentials."""
    key = str(preset_name or "").strip().lower()
    if key not in STRATEGY_PRESETS:
        raise ValueError(f"지원하지 않는 프리셋입니다: {preset_name}")
    preset = STRATEGY_PRESETS[key]
    for k, v in preset.items():
        if k != "label":
            config[k] = v
    return preset["label"]


def _required_1h_limit(lookback_bars, scan_bars=1, days=14):
    need = int(days) * 24 + math.ceil((max(1, int(lookback_bars)) + max(1, int(scan_bars))) / 12.0) + 24
    return min(1000, max(360, need))


def _get_ssl_context():
    cafile = ssl.get_default_verify_paths().cafile
    if cafile and Path(cafile).is_file():
        return ssl.create_default_context()
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    mac_ca = Path("/etc/ssl/cert.pem")
    if mac_ca.is_file():
        return ssl.create_default_context(cafile=str(mac_ca))
    return ssl.create_default_context()


def _http_get_json(url, timeout=10, retries=3):
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (BTC Quant Lab)"})
    ctx = _get_ssl_context()
    last_err = None
    for attempt in range(max(1, int(retries))):
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            last_err = e
            if e.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as e:
            last_err = e
        if attempt + 1 < retries:
            time.sleep(0.4 * (2 ** attempt))
    raise last_err


def _safe_int(value, default=0):
    if value is None or value == "" or isinstance(value, bool):
        return int(default)
    try:
        v = float(value)
        return int(v) if math.isfinite(v) else int(default)
    except (TypeError, ValueError):
        return int(default)


def _safe_float(value, default=0.0):
    if value is None or value == "" or isinstance(value, bool):
        return float(default)
    try:
        v = float(value)
        return v if math.isfinite(v) else float(default)
    except (TypeError, ValueError):
        return float(default)


def _pick_positive_price(*candidates, fallback=0.0):
    for c in candidates:
        v = _safe_float(c, 0.0)
        if v > 0.0:
            return v
    return _safe_float(fallback, 0.0)


def _required_daily_limit(config, min_limit=250):
    cfg = config if isinstance(config, dict) else {}
    lens = [
        _safe_int(cfg.get("bull_sma_len", 40), 40),
        _safe_int(cfg.get("bear_sma_len", 40), 40),
        _safe_int(cfg.get("strategy_sma", 44), 44),
        _safe_int(cfg.get("regime_sma_len", 200), 200),
        _safe_int(cfg.get("bull_vol_ma", 20), 20) + 1,
        _safe_int(cfg.get("bear_vol_ma", 5), 5) + 1,
        _safe_int(cfg.get("volume_ma_len", 20), 20) + 1,
    ]
    return min(1000, max(int(min_limit), max(lens) + 50))


def _atomic_write_json(path, data):
    raw_path = Path(path)
    target = Path(os.path.realpath(raw_path)) if os.path.islink(raw_path) else raw_path
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.with_name(f".{target.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, target)


def default_state(config):
    seed = float(config.get("initial_seed_usdt", 10000.0))
    return {
        "initial_seed": seed,
        "cash": seed,
        "btc_qty": 0.0,
        "cost_basis": 0.0,
        "position": "CASH",  # 'BTC' or 'CASH'
        "entry_price": 0.0,
        "entry_time_utc": "",
        "last_evaluated_candle_ms": 0,
        "last_morning_report_date": "",
        "trades": [],
        "last_signal": "NONE",
        "last_regime_is_bull": None,
        "last_touch_alerts": {},
        "last_dip_alert_ts": 0.0,
        "last_dip_alert_candle_ms": 0,
        "dip_initial_seed": seed,
        "dip_cash": seed,
        "open_dip": None,
        "dip_trades": [],
        "oos_start_utc": "",
        "oos_start_btc_price": 0.0,
        "oos_start_equity": seed,
        "oos_equity_history": [],
    }


def load_config(config_path=CONFIG_PATH):
    path = Path(config_path)
    if not path.is_file():
        _atomic_write_json(path, dict(DEFAULT_CONFIG))
        return dict(DEFAULT_CONFIG)
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
        merged = dict(DEFAULT_CONFIG)
        if isinstance(loaded, dict):
            merged.update(loaded)
        return merged
    except Exception as e:
        print(f"Config load error: {e}")
        return dict(DEFAULT_CONFIG)


def load_state(config, state_path=STATE_PATH):
    path = Path(state_path)
    base = default_state(config)
    if not path.is_file():
        _atomic_write_json(path, base)
        return base
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            raise ValueError("state JSON is not an object")
        for k, v in base.items():
            if k not in data:
                data[k] = v
        if not isinstance(data.get("trades"), list):
            data["trades"] = []
        if not isinstance(data.get("last_touch_alerts"), dict):
            data["last_touch_alerts"] = {}
        if not isinstance(data.get("dip_trades"), list):
            data["dip_trades"] = []
        if not isinstance(data.get("oos_equity_history"), list):
            data["oos_equity_history"] = []
        if data.get("open_dip") is not None and not isinstance(data.get("open_dip"), dict):
            data["open_dip"] = None
        if _safe_float(data.get("dip_initial_seed", 0), 0.0) <= 0:
            data["dip_initial_seed"] = float(base["dip_initial_seed"])
        if data.get("dip_cash") is None:
            data["dip_cash"] = float(data["dip_initial_seed"])
        return data
    except Exception as e:
        print(f"State load error (backing up corrupt file): {e}")
        try:
            target = Path(os.path.realpath(path)) if os.path.islink(path) else path
            corrupt = target.with_name(f"{target.name}.corrupt.{int(time.time())}")
            os.replace(target, corrupt)
        except Exception:
            pass
        _atomic_write_json(path, base)
        return base


def save_state(state, state_path=STATE_PATH):
    _atomic_write_json(state_path, state)


CLOUD_STATE_RAW_URL = "https://raw.githubusercontent.com/mchae3/btc-cloud-bot/main/paper_trading_state.json"


def sync_state_from_cloud(state, state_path=STATE_PATH, url=CLOUD_STATE_RAW_URL, timeout=4):
    """Merge newer state fields from the GitHub Actions 24/7 cloud bot to prevent split-brain/duplicate alerts."""
    if not url or not isinstance(state, dict):
        return False
    try:
        sep = "&" if "?" in url else "?"
        req = urllib.request.Request(
            f"{url}{sep}_ts={int(time.time())}",
            headers={
                "User-Agent": "BTCQuantLab-StateSync/1.0",
                "Cache-Control": "no-cache, no-store",
                "Pragma": "no-cache",
            },
        )
        with urllib.request.urlopen(req, timeout=timeout, context=_get_ssl_context()) as resp:
            remote = json.loads(resp.read().decode("utf-8"))
        if not isinstance(remote, dict):
            return False
    except Exception:
        return False

    dirty = False
    r_eval = int(remote.get("last_evaluated_candle_ms", 0) or 0)
    l_eval = int(state.get("last_evaluated_candle_ms", 0) or 0)
    r_trades = remote.get("trades") if isinstance(remote.get("trades"), list) else []
    l_trades = state.get("trades") if isinstance(state.get("trades"), list) else []
    if r_eval > l_eval or len(r_trades) > len(l_trades):
        for k in ("last_evaluated_candle_ms", "position", "cash", "btc_qty", "cost_basis", "entry_price", "entry_time_utc", "trades", "last_signal"):
            if k in remote and state.get(k) != remote[k]:
                state[k] = remote[k]
                dirty = True

    r_morn = str(remote.get("last_morning_report_date", "") or "")
    l_morn = str(state.get("last_morning_report_date", "") or "")
    if r_morn > l_morn:
        state["last_morning_report_date"] = r_morn
        dirty = True

    r_dip_ms = int(remote.get("last_dip_alert_candle_ms", 0) or 0)
    l_dip_ms = int(state.get("last_dip_alert_candle_ms", 0) or 0)
    r_dip_trades = remote.get("dip_trades") if isinstance(remote.get("dip_trades"), list) else []
    l_dip_trades = state.get("dip_trades") if isinstance(state.get("dip_trades"), list) else []
    r_open = remote.get("open_dip") if isinstance(remote.get("open_dip"), dict) else None
    l_open = state.get("open_dip") if isinstance(state.get("open_dip"), dict) else None
    l_settled_sigs = {int(t.get("signal_candle_ms", 0) or 0) for t in l_dip_trades if isinstance(t, dict)}
    r_open_sig = int(r_open.get("signal_candle_ms", 0) or 0) if r_open else 0
    remote_settled_local_open = (
        r_dip_ms == l_dip_ms
        and len(r_dip_trades) == len(l_dip_trades)
        and r_open is None
        and l_open is not None
    )
    remote_opened_new_unsettled = (
        r_dip_ms == l_dip_ms
        and r_dip_ms > 0
        and len(r_dip_trades) == len(l_dip_trades)
        and l_open is None
        and r_open is not None
        and (r_open_sig == 0 or r_open_sig not in l_settled_sigs)
    )
    if (
        r_dip_ms > l_dip_ms
        or len(r_dip_trades) > len(l_dip_trades)
        or remote_settled_local_open
        or remote_opened_new_unsettled
    ):
        for k in ("last_dip_alert_candle_ms", "open_dip", "dip_trades", "dip_cash", "dip_initial_seed"):
            if k in remote and state.get(k) != remote[k]:
                state[k] = remote[k]
                dirty = True
        r_dip_ts = float(remote.get("last_dip_alert_ts", 0.0) or 0.0)
        if r_dip_ts > _safe_float(state.get("last_dip_alert_ts", 0.0), 0.0):
            state["last_dip_alert_ts"] = r_dip_ts
            dirty = True
    else:
        if (
            isinstance(l_open, dict)
            and isinstance(r_open, dict)
            and _safe_int(l_open.get("signal_candle_ms", 0), 0) == _safe_int(r_open.get("signal_candle_ms", 0), 0)
            and l_open.get("entry_alert_sent") is False
            and r_open.get("entry_alert_sent", True) is True
        ):
            l_open["entry_alert_sent"] = True
            dirty = True
        if len(r_dip_trades) == len(l_dip_trades) and l_dip_trades:
            for lt, rt in zip(l_dip_trades, r_dip_trades):
                if (
                    isinstance(lt, dict)
                    and isinstance(rt, dict)
                    and lt.get("signal_candle_ms") == rt.get("signal_candle_ms")
                    and not lt.get("exit_alert_sent", True)
                    and rt.get("exit_alert_sent") is True
                ):
                    lt["exit_alert_sent"] = True
                    dirty = True

    r_touch = remote.get("last_touch_alerts")
    if isinstance(r_touch, dict):
        l_touch = state.setdefault("last_touch_alerts", {})
        for k, v in r_touch.items():
            try:
                fv = float(v)
                if fv > float(l_touch.get(k, 0.0) or 0.0):
                    l_touch[k] = fv
                    dirty = True
            except (TypeError, ValueError):
                pass

    if dirty and state_path:
        save_state(state, state_path=state_path)
    return dirty


def send_telegram_message(token, chat_id, text, parse_mode="HTML"):
    if not token or not chat_id:
        print(f"[Telegram Mock] (No token/chat_id configured):\n{text}")
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": str(chat_id),
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
    ctx = _get_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=10, context=ctx) as resp:
            return resp.status == 200
    except Exception as e:
        print(f"Telegram send error: {e}")
        return False


BINANCE_SPOT_BASES = (
    "https://api.binance.com",
    "https://data-api.binance.vision",
)


def _fetch_binance_public_json(path_and_query, timeout=10, retries=2):
    """Query Binance public market API with automatic fallback to data-api.binance.vision (e.g. US IP HTTP 451)."""
    last_err = None
    for base in BINANCE_SPOT_BASES:
        url = f"{base}{path_and_query}"
        try:
            return _http_get_json(url, timeout=timeout, retries=retries)
        except Exception as e:
            last_err = e
    raise last_err


def _fetch_binance_klines(interval, limit, start_time_ms=None):
    """Fetch spot klines from Binance public API in chronological order."""
    path = f"/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={int(limit)}"
    if start_time_ms is not None:
        path += f"&startTime={int(start_time_ms)}"
    try:
        raw = _fetch_binance_public_json(path, timeout=10, retries=2)
        candles = []
        for item in raw:
            candles.append({
                "open_time_ms": int(item[0]),
                "open": float(item[1]),
                "high": float(item[2]),
                "low": float(item[3]),
                "close": float(item[4]),
                "volume": float(item[5]),
                "close_time_ms": int(item[6])
            })
        candles.sort(key=lambda c: c["open_time_ms"])
        return candles
    except Exception as e:
        print(f"Binance API ({interval}) fetch error: {e}")
        return []


def fetch_binance_daily_candles(limit=250):
    """Fetch daily candles from Binance Spot public API in chronological order."""
    return _fetch_binance_klines("1d", limit)


def fetch_binance_5m_candles(limit=40, start_time_ms=None):
    """Fetch 5-minute candles from Binance Spot public API in chronological order."""
    return _fetch_binance_klines("5m", limit, start_time_ms=start_time_ms)


_BINANCE_1H_CACHE = {"ts": 0.0, "hour_bucket": -1, "limit": 0, "candles": None}


def clear_binance_1h_cache():
    """Reset the in-memory 1h klines cache (useful for tests or manual refresh)."""
    _BINANCE_1H_CACHE.update({"ts": 0.0, "hour_bucket": -1, "limit": 0, "candles": None})


def fetch_binance_1h_candles(limit=360):
    """Fetch 1-hour candles from Binance Spot public API in chronological order (14d baseline volume)."""
    return _fetch_binance_klines("1h", limit)


def get_cached_binance_1h_candles(limit=360, ttl_sec=900, now_ts=None):
    """Fetch 1h candles using an in-memory TTL cache that invalidates at each UTC hour rollover."""
    ts = float(now_ts) if now_ts is not None else time.time()
    hour_bucket = int(ts // 3600)
    cached = _BINANCE_1H_CACHE
    if (
        cached["candles"]
        and cached["limit"] >= int(limit)
        and cached["hour_bucket"] == hour_bucket
        and (ts - cached["ts"]) < float(ttl_sec)
    ):
        return cached["candles"]
    candles = fetch_binance_1h_candles(limit=limit)
    if candles:
        cached.update({"ts": ts, "hour_bucket": hour_bucket, "limit": int(limit), "candles": candles})
    return candles


def fetch_binance_ticker_price():
    path = "/api/v3/ticker/price?symbol=BTCUSDT"
    try:
        data = _fetch_binance_public_json(path, timeout=5, retries=2)
        p = float(data.get("price", 0))
        return p if math.isfinite(p) and p > 0 else None
    except Exception:
        return None


def fetch_binance_24h_ticker():
    path = "/api/v3/ticker/24hr?symbol=BTCUSDT"
    try:
        data = _fetch_binance_public_json(path, timeout=8, retries=2)
        return {
            "price": float(data.get("lastPrice", 0)),
            "change_24h_pct": float(data.get("priceChangePercent", 0)),
            "high_24h": float(data.get("highPrice", 0)),
            "low_24h": float(data.get("lowPrice", 0)),
            "volume_24h": float(data.get("volume", 0))
        }
    except Exception as e:
        print(f"Binance 24h ticker error: {e}")
        return None


def partition_candles(candles, now_ms=None, clock_skew_ms=2000):
    """Split candles strictly into closed valid bars vs forming open bar (QIS-7 Closed Candle Rule)."""
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000) - max(0, int(clock_skew_ms))
    closed = []
    forming = []
    for c in sorted((candles or []), key=lambda x: _safe_int(x.get("open_time_ms", 0), 0)):
        o = _safe_float(c.get("open", 0.0), 0.0)
        h = _safe_float(c.get("high", 0.0), 0.0)
        l = _safe_float(c.get("low", 0.0), 0.0)
        cl = _safe_float(c.get("close", 0.0), 0.0)
        vol = _safe_float(c.get("volume", -1.0), -1.0)
        close_ms = _safe_int(c.get("close_time_ms", 0), 0)
        valid_ohlc = (
            l > 0.0 and o > 0.0 and cl > 0.0 and h > 0.0 and vol >= 0.0
        )
        if not valid_ohlc:
            continue
        if close_ms < now_ms:
            if vol > 0.0:
                closed.append(c)
        else:
            forming.append(c)
    return closed, forming


def partition_daily_candles(candles, now_ms=None, clock_skew_ms=2000):
    """Split daily candles strictly into closed valid bars vs forming open bar (QIS-7 Closed Candle Rule)."""
    return partition_candles(candles, now_ms=now_ms, clock_skew_ms=clock_skew_ms)


def compute_live_signal_snapshot(closed_candles, config, prev_pos="CASH", prev_state=None):
    """Exact Model C / v3 Dual-Kernel + Vol-Target snapshot on closed daily bars."""
    strategy = config.get("strategy_name", "daily_sma40_hybrid")
    is_hybrid = strategy in ("daily_sma40_hybrid", "daily_sma40_volume_c", "daily_sma_hybrid", "model_c")
    trend_kernel = str(config.get("trend_kernel", "sma") or "sma").strip().lower()
    if trend_kernel not in ("sma", "sma_donchian"):
        trend_kernel = "sma"
    vol_target_pct = max(0.0, _safe_float(config.get("vol_target_pct", 0.0), 0.0))
    vol_lookback = max(2, _safe_int(config.get("vol_lookback", 20), 20))
    rebal_deadband_pct = max(0.0, min(100.0, _safe_float(config.get("rebal_deadband_pct", 15.0), 15.0)))

    if is_hybrid:
        bull_sma_len = max(1, _safe_int(config.get("bull_sma_len", 40), 40))
        bear_sma_len = max(1, _safe_int(config.get("bear_sma_len", 40), 40))
        regime_sma_len = max(2, _safe_int(config.get("regime_sma_len", 200), 200))
        bull_vol_mult = max(0.0, _safe_float(config.get("bull_vol_mult", 1.2), 1.2))
        bull_vol_ma = max(1, _safe_int(config.get("bull_vol_ma", 20), 20))
        bear_vol_mult = max(0.0, _safe_float(config.get("bear_vol_mult", 1.8), 1.8))
        bear_vol_ma = max(1, _safe_int(config.get("bear_vol_ma", 5), 5))
    else:
        sma_len = max(1, _safe_int(config.get("strategy_sma", 44), 44))
        bull_sma_len = bear_sma_len = sma_len
        regime_sma_len = max(2, _safe_int(config.get("regime_sma_len", 200), 200))
        bull_vol_mult = bear_vol_mult = max(0.0, _safe_float(config.get("volume_multiplier", 1.2), 1.2))
        bull_vol_ma = bear_vol_ma = max(1, _safe_int(config.get("volume_ma_len", 20), 20))

    min_needed = max(bull_sma_len, bear_sma_len, bull_vol_ma + 1, bear_vol_ma + 1)
    if len(closed_candles) < min_needed:
        raise ValueError(f"완료 일봉 부족: 최소 {min_needed}일 필요, 현재 {len(closed_candles)}일")

    completed_candle = closed_candles[-1]
    closes = [float(c["close"]) for c in closed_candles]
    highs = [float(c.get("high", c["close"])) for c in closed_candles]
    lows = [float(c.get("low", c["close"])) for c in closed_candles]
    vols = [float(c["volume"]) for c in closed_candles]
    last_close = closes[-1]
    last_vol = vols[-1]

    regime_sma = None
    if len(closes) >= regime_sma_len:
        regime_sma = sum(closes[-regime_sma_len:]) / float(regime_sma_len)

    if is_hybrid:
        is_bull = (regime_sma is not None) and (last_close > regime_sma)
        active_sma_len = bull_sma_len if is_bull else bear_sma_len
        vol_mult = bull_vol_mult if is_bull else bear_vol_mult
        vol_ma_len = bull_vol_ma if is_bull else bear_vol_ma
    else:
        is_bull = (regime_sma is not None) and (last_close > regime_sma)
        active_sma_len = bull_sma_len
        vol_mult = bull_vol_mult
        vol_ma_len = bull_vol_ma

    active_sma = sum(closes[-active_sma_len:]) / float(active_sma_len)
    donchian_mid = 0.5 * (max(highs[-active_sma_len:]) + min(lows[-active_sma_len:]))
    prior_vols = vols[-(vol_ma_len + 1):-1]
    prior_vol_avg = sum(prior_vols) / float(vol_ma_len)
    vol_ratio = (last_vol / prior_vol_avg) if prior_vol_avg > 0 else 0.0
    vol_ok = (len(prior_vols) == vol_ma_len) and (prior_vol_avg > 0) and (last_vol >= vol_mult * prior_vol_avg)

    # Strictly causal realized annualized volatility (matching backtester._build_model_d_target_weights)
    realized_vol_ann = None
    if len(closes) > vol_lookback:
        log_rets = []
        for idx_r in range(len(closes) - vol_lookback, len(closes)):
            if closes[idx_r - 1] > 0.0 and closes[idx_r] > 0.0:
                log_rets.append(math.log(closes[idx_r] / closes[idx_r - 1]))
        if len(log_rets) == vol_lookback:
            mean_r = sum(log_rets) / float(vol_lookback)
            var_r = sum((x - mean_r) ** 2 for x in log_rets) / float(vol_lookback - 1)
            realized_vol_ann = math.sqrt(var_r) * math.sqrt(365.25) * 100.0

    is_above = last_close > active_sma
    is_below = last_close < active_sma
    exit_confirm_days = max(1, min(10, _safe_int(config.get("exit_confirm_days", 1), 1)))
    below_streak = 0
    if is_below:
        if exit_confirm_days == 1:
            below_streak = 1
        else:
            # Causal backward streak matching backtester._model_c_hybrid_signals()
            n_c = len(closes)
            for idx_back in range(n_c - 1, -1, -1):
                if is_hybrid:
                    reg_b = (
                        sum(closes[idx_back - regime_sma_len + 1:idx_back + 1]) / float(regime_sma_len)
                        if idx_back >= regime_sma_len - 1 else None
                    )
                    bull_b = (reg_b is not None) and (closes[idx_back] > reg_b)
                    sma_len_b = bull_sma_len if bull_b else bear_sma_len
                else:
                    sma_len_b = bull_sma_len
                if idx_back < sma_len_b - 1:
                    break
                sma_b = sum(closes[idx_back - sma_len_b + 1:idx_back + 1]) / float(sma_len_b)
                if closes[idx_back] < sma_b:
                    below_streak += 1
                    if below_streak >= exit_confirm_days:
                        break
                else:
                    break

    donch_below_streak = 0
    if last_close < donchian_mid:
        if exit_confirm_days == 1:
            donch_below_streak = 1
        else:
            n_c = len(closes)
            for idx_back in range(n_c - 1, -1, -1):
                if idx_back < active_sma_len - 1:
                    break
                dm_b = 0.5 * (
                    max(highs[idx_back - active_sma_len + 1:idx_back + 1])
                    + min(lows[idx_back - active_sma_len + 1:idx_back + 1])
                )
                if closes[idx_back] < dm_b:
                    donch_below_streak += 1
                    if donch_below_streak >= exit_confirm_days:
                        break
                else:
                    break

    use_fractional = (trend_kernel == "sma_donchian") or (vol_target_pct > 0.0)
    if not use_fractional:
        if prev_pos == "CASH":
            target_pos = "BTC" if (is_above and vol_ok) else "CASH"
        else:
            target_pos = "CASH" if (below_streak >= exit_confirm_days) else "BTC"
        tranche_active = [1 if target_pos == "BTC" else 0]
        base_frac = 1.0 if target_pos == "BTC" else 0.0
        frozen_vol_scalar = 1.0
        target_weight = base_frac
    else:
        st = prev_state if isinstance(prev_state, dict) else {}
        k_cnt = 2 if trend_kernel == "sma_donchian" else 1
        raw_tr = st.get("tranche_active")
        if isinstance(raw_tr, list) and len(raw_tr) == k_cnt:
            tranche_active = [1 if int(x) else 0 for x in raw_tr]
        else:
            tranche_active = [1 if prev_pos == "BTC" else 0] * k_cnt
        prev_active = sum(tranche_active)
        # Tranche 0: SMA
        if tranche_active[0]:
            if below_streak >= exit_confirm_days:
                tranche_active[0] = 0
        elif is_above and vol_ok:
            tranche_active[0] = 1
        # Tranche 1: Donchian Midpoint (when sma_donchian)
        if k_cnt == 2:
            if tranche_active[1]:
                if donch_below_streak >= exit_confirm_days:
                    tranche_active[1] = 0
            elif last_close > donchian_mid and vol_ok:
                tranche_active[1] = 1

        base_frac = sum(tranche_active) / float(k_cnt)
        if base_frac <= 0.0:
            frozen_vol_scalar = 1.0
            target_weight = 0.0
        else:
            if vol_target_pct > 0.0 and realized_vol_ann is not None and realized_vol_ann > 1e-6:
                cur_scalar = min(1.0, vol_target_pct / realized_vol_ann)
            else:
                cur_scalar = 1.0
            prev_scalar = _safe_float(st.get("frozen_vol_scalar", 1.0), 1.0)
            if prev_scalar <= 0.0 or prev_scalar > 1.0:
                prev_scalar = 1.0
            deadband_frac = rebal_deadband_pct / 100.0
            if prev_active == 0:
                frozen_vol_scalar = cur_scalar
            elif deadband_frac < 1.0 and abs(cur_scalar - prev_scalar) >= deadband_frac * max(0.1, prev_scalar):
                frozen_vol_scalar = cur_scalar
            else:
                frozen_vol_scalar = prev_scalar
            target_weight = min(1.0, max(0.0, base_frac * frozen_vol_scalar))
        target_pos = "BTC" if target_weight > 1e-9 else "CASH"

    return {
        "strategy": strategy,
        "is_hybrid": is_hybrid,
        "trend_kernel": trend_kernel,
        "vol_target_pct": vol_target_pct,
        "completed_candle": completed_candle,
        "last_close": last_close,
        "active_sma_len": active_sma_len,
        "active_sma": active_sma,
        "donchian_mid": donchian_mid,
        "bull_sma_len": bull_sma_len,
        "bear_sma_len": bear_sma_len,
        "regime_sma_len": regime_sma_len,
        "regime_sma": regime_sma,
        "is_bull": is_bull,
        "regime_label": "강세장 (200일선 위)" if is_bull else "약세/박스권 (200일선 아래)",
        "last_vol": last_vol,
        "vol_ma_len": vol_ma_len,
        "vol_mult": vol_mult,
        "prior_vol_avg": prior_vol_avg,
        "vol_ratio": vol_ratio,
        "vol_ok": vol_ok,
        "realized_vol_20d": realized_vol_ann,
        "is_above": is_above,
        "is_below": is_below,
        "below_streak": below_streak,
        "donch_below_streak": donch_below_streak,
        "exit_confirm_days": exit_confirm_days,
        "tranche_active": tranche_active,
        "base_frac": base_frac,
        "vol_scalar": frozen_vol_scalar,
        "target_weight": target_weight,
        "target_pos": target_pos,
    }


def _strategy_display_name(config, snap=None):
    strategy = config.get("strategy_name", "daily_sma40_hybrid")
    ecd = max(1, _safe_int(config.get("exit_confirm_days", 1), 1))
    ecd_tag = f" · {ecd}일확인" if ecd > 1 else ""
    tk = str(config.get("trend_kernel", "sma") or "sma").strip().lower()
    vt = _safe_float(config.get("vol_target_pct", 0.0), 0.0)
    vol_tag = f" · Vol{vt:g}%" if vt > 0.0 else ""
    if strategy in ("daily_sma40_hybrid", "daily_sma40_volume_c", "daily_sma_hybrid", "model_c"):
        b_sma = config.get("bull_sma_len", 40)
        r_sma = config.get("bear_sma_len", 40)
        reg = config.get("regime_sma_len", 200)
        b_m = config.get("bull_vol_mult", 1.2)
        b_l = config.get("bull_vol_ma", 20)
        s_m = config.get("bear_vol_mult", 1.8)
        s_l = config.get("bear_vol_ma", 5)
        if tk == "sma_donchian":
            return f"👑 v3 듀얼커널 (SMA+Donch {b_sma}일 · {reg}일 체제 · 불 {b_m}x/{b_l}일 · 약세 {s_m}x/{s_l}일{vol_tag}{ecd_tag})"
        return f"👑 모델 C ({b_sma}/{r_sma}일선 · {reg}일 체제전환 · 불 {b_m}x/{b_l}일 · 약세 {s_m}x/{s_l}일{vol_tag}{ecd_tag})"
    sma = config.get("strategy_sma", 44)
    vm = config.get("volume_multiplier", 1.2)
    vl = config.get("volume_ma_len", 20)
    return f"일봉 {sma}일 SMA + 거래량({vm}x, {vl}일{vol_tag}{ecd_tag})"


def evaluate_signal_and_step(config, state, manual_check=False, state_path=STATE_PATH, now_ms=None, candles=None):
    """Evaluate Model C (or configured strategy) on latest closed daily candle and execute virtual fill."""
    fee_rate = float(config.get("fee_rate", 0.0015))
    slip_rate = float(config.get("slippage_rate", 0.0005))
    token = config.get("bot_token")
    chat_id = config.get("chat_id")

    if candles is None:
        candles = fetch_binance_daily_candles(limit=_required_daily_limit(config))
    closed, forming = partition_daily_candles(candles, now_ms=now_ms)
    if not closed:
        print("Not enough closed daily candles fetched from Binance.")
        return state, "캔들 데이터 부족"

    prev_eval_ms = _safe_int(state.get("last_evaluated_candle_ms", 0), 0)
    latest_closed_ms = int(closed[-1]["open_time_ms"])
    if prev_eval_ms > 0:
        pending_indices = [k for k, c in enumerate(closed) if int(c["open_time_ms"]) > prev_eval_ms]
    else:
        pending_indices = [len(closed) - 1]
    is_new_daily_close = bool(pending_indices)

    if forming and forming[0]["open_time_ms"] == closed[-1]["close_time_ms"] + 1:
        latest_fill_price = _pick_positive_price(forming[0].get("open"), closed[-1].get("close"))
        current_price = _pick_positive_price(forming[-1].get("close"), latest_fill_price)
    else:
        ticker_p = fetch_binance_ticker_price()
        latest_fill_price = _pick_positive_price(ticker_p, closed[-1].get("close"))
        current_price = latest_fill_price

    trade_executed = False
    snap = None

    if not is_new_daily_close:
        try:
            snap = compute_live_signal_snapshot(closed, config, prev_pos=state.get("position", "CASH"), prev_state=state)
            state["last_regime_is_bull"] = bool(snap["is_bull"])
            state["realized_vol_20d"] = snap.get("realized_vol_20d")
        except ValueError as e:
            print(f"Signal computation error: {e}")
            return state, str(e)
    else:
        # Replay every un-evaluated closed daily candle chronologically (prevents missed signals after multi-day outages).
        for step_pos, k in enumerate(pending_indices):
            prev_pos = state.get("position", "CASH")
            try:
                step_snap = compute_live_signal_snapshot(closed[:k + 1], config, prev_pos=prev_pos, prev_state=state)
            except ValueError as e:
                if step_pos == len(pending_indices) - 1 and snap is None:
                    print(f"Signal computation error: {e}")
                    return state, str(e)
                continue

            snap = step_snap
            completed_candle = snap["completed_candle"]
            last_evaluated_ms = int(completed_candle["open_time_ms"])
            step_fill_price = float(closed[k + 1]["open"]) if (k + 1 < len(closed)) else float(latest_fill_price)

            state["last_evaluated_candle_ms"] = last_evaluated_ms
            target_pos = snap["target_pos"]
            target_w = float(snap.get("target_weight", 1.0 if target_pos == "BTC" else 0.0))
            prev_w = float(state.get("target_weight", 1.0 if prev_pos == "BTC" else 0.0))
            state["last_signal"] = target_pos
            state["last_regime_is_bull"] = bool(snap["is_bull"])
            state["tranche_active"] = list(snap.get("tranche_active", [1 if target_pos == "BTC" else 0]))
            state["frozen_vol_scalar"] = float(snap.get("vol_scalar", 1.0))
            state["realized_vol_20d"] = snap.get("realized_vol_20d")

            use_fractional = (snap.get("trend_kernel") == "sma_donchian") or (float(snap.get("vol_target_pct", 0.0)) > 0.0)
            if not use_fractional:
                state["target_weight"] = 1.0 if target_pos == "BTC" else 0.0
                if prev_pos != target_pos:
                    if target_pos == "BTC":
                        cash = float(state.get("cash", config.get("initial_seed_usdt", 10000.0)))
                        buy_fill = step_fill_price * (1.0 + slip_rate)
                        qty = cash / (buy_fill * (1.0 + fee_rate))
                        fee = qty * buy_fill * fee_rate
                        state["cash"] = 0.0
                        state["btc_qty"] = qty
                        state["cost_basis"] = cash
                        state["position"] = "BTC"
                        state["entry_price"] = buy_fill
                        state["entry_time_utc"] = datetime.now(timezone.utc).isoformat()

                        # Canonical Model C Combined rule: if an active 2h dip trade spans across or expires at
                        # daily BTC buy open (exit_open_ms >= daily_fill_open_ms), promote into trend without selling.
                        promoted_dip = None
                        daily_fill_open_ms = int(completed_candle["close_time_ms"]) + 1
                        open_dip = state.get("open_dip")
                        if isinstance(open_dip, dict) and _safe_int(open_dip.get("exit_open_ms", 0), 0) >= daily_fill_open_ms:
                            dip_entry_ref = float(open_dip.get("entry_ref_price") or open_dip.get("entry_fill") or step_fill_price)
                            handoff_ret_pct = ((step_fill_price / dip_entry_ref) - 1.0) * 100.0 if dip_entry_ref > 0 else 0.0
                            promoted_dip = {
                                "status": "PROMOTED_TO_TREND",
                                "signal_candle_ms": open_dip.get("signal_candle_ms", 0),
                                "entry_open_ms": open_dip.get("entry_open_ms", 0),
                                "exit_open_ms": daily_fill_open_ms,
                                "entry_time": open_dip.get("entry_time_utc", ""),
                                "exit_time": datetime.fromtimestamp(daily_fill_open_ms / 1000.0, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                "entry_price": float(open_dip.get("entry_fill", dip_entry_ref)),
                                "entry_ref_price": dip_entry_ref,
                                "exit_price": float(step_fill_price),
                                "exit_ref_price": float(step_fill_price),
                                "pnl": 0.0,
                                "pnl_pct": handoff_ret_pct,
                                "drop_pct_actual": open_dip.get("drop_pct_actual"),
                                "vol_ratio": open_dip.get("vol_ratio"),
                                "reason": "모델 C 추세 승계 (매도 없이 전환)",
                                "exit_alert_sent": True,
                            }
                            state.setdefault("dip_trades", []).append(promoted_dip)
                            if open_dip.get("cost_basis"):
                                state["dip_cash"] = float(open_dip["cost_basis"])
                            state["open_dip"] = None

                        trade_executed = True
                        trade_info = {
                            "action": "매수 (BUY)",
                            "price": buy_fill,
                            "reference_price": step_fill_price,
                            "qty": qty,
                            "fee": fee,
                            "regime": snap["regime_label"],
                            "vol_ratio": snap["vol_ratio"],
                            "promoted_dip": promoted_dip,
                            "reason": (
                                f"{snap['active_sma_len']}일선 돌파 & 거래량 {snap['vol_ratio']:.2f}배 "
                                f"(>= {snap['vol_mult']}배, {snap['regime_label']})"
                            ),
                            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        }
                        state["trades"].append(trade_info)
                    else:
                        qty = float(state.get("btc_qty", 0.0))
                        sell_fill = step_fill_price * (1.0 - slip_rate)
                        gross_cash = qty * sell_fill
                        fee = gross_cash * fee_rate
                        net_cash = gross_cash - fee
                        entry_p = float(state.get("entry_price", 0.0))
                        cost_basis = float(state.get("cost_basis") or (qty * entry_p * (1.0 + fee_rate)))
                        pnl = net_cash - cost_basis
                        pnl_pct = (pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0

                        state["cash"] = net_cash
                        state["btc_qty"] = 0.0
                        state["cost_basis"] = 0.0
                        state["position"] = "CASH"
                        state["entry_price"] = 0.0
                        state["entry_time_utc"] = ""
                        trade_executed = True
                        trade_info = {
                            "action": "매도 (SELL)",
                            "price": sell_fill,
                            "reference_price": step_fill_price,
                            "qty": qty,
                            "fee": fee,
                            "pnl": pnl,
                            "pnl_pct": pnl_pct,
                            "regime": snap["regime_label"],
                            "vol_ratio": snap["vol_ratio"],
                            "reason": (
                                f"{snap['active_sma_len']}일선 {snap.get('exit_confirm_days', 1)}일 연속 하향 이탈 청산"
                                if snap.get("exit_confirm_days", 1) > 1
                                else f"{snap['active_sma_len']}일선 하향 이탈 청산"
                            ),
                            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                        }
                        state["trades"].append(trade_info)

                    save_state(state, state_path=state_path)
                    msg = format_trade_alert(config, state, trade_info, snap)
                    send_telegram_message(token, chat_id, msg)
            else:
                # Fractional target-weight rebalancing (1:1 parity with simulation.simulate_target_weights)
                state["target_weight"] = target_w
                if abs(target_w - prev_w) > 1e-6:
                    cash = float(state.get("cash", config.get("initial_seed_usdt", 10000.0)))
                    cur_qty = float(state.get("btc_qty", 0.0))
                    eq_now = cash + cur_qty * step_fill_price
                    desired_val = eq_now * target_w
                    cur_val = cur_qty * step_fill_price
                    diff_val = desired_val - cur_val
                    trade_info = None
                    if target_w <= 1e-9:
                        if cur_qty > 0.0:
                            sell_fill = step_fill_price * (1.0 - slip_rate)
                            gross_cash = cur_qty * sell_fill
                            fee = gross_cash * fee_rate
                            net_cash = gross_cash - fee
                            cost_basis = float(state.get("cost_basis") or (cur_qty * float(state.get("entry_price", 0.0)) * (1.0 + fee_rate)))
                            pnl = net_cash - cost_basis
                            pnl_pct = (pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0
                            state["cash"] = cash + net_cash
                            state["btc_qty"] = 0.0
                            state["cost_basis"] = 0.0
                            state["position"] = "CASH"
                            state["entry_price"] = 0.0
                            state["entry_time_utc"] = ""
                            trade_info = {
                                "action": "매도 (SELL)",
                                "price": sell_fill,
                                "reference_price": step_fill_price,
                                "qty": cur_qty,
                                "fee": fee,
                                "pnl": pnl,
                                "pnl_pct": pnl_pct,
                                "target_weight": 0.0,
                                "regime": snap["regime_label"],
                                "vol_ratio": snap["vol_ratio"],
                                "reason": f"목표 비중 {prev_w*100:.0f}% → 0% 전량 청산 ({snap.get('exit_confirm_days', 1)}일 확인)",
                                "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                            }
                    elif diff_val > 1e-6 and cash > 1e-6:
                        buy_budget = min(cash, diff_val)
                        buy_fill = step_fill_price * (1.0 + slip_rate)
                        add_qty = buy_budget / (buy_fill * (1.0 + fee_rate))
                        fee = add_qty * buy_fill * fee_rate
                        new_qty = cur_qty + add_qty
                        new_basis = float(state.get("cost_basis", 0.0)) + buy_budget
                        state["cash"] = max(0.0, cash - buy_budget)
                        state["btc_qty"] = new_qty
                        state["cost_basis"] = new_basis
                        state["position"] = "BTC"
                        state["entry_price"] = (new_basis / (new_qty * (1.0 + fee_rate))) if new_qty > 0 else buy_fill
                        if prev_pos == "CASH":
                            state["entry_time_utc"] = datetime.now(timezone.utc).isoformat()
                        promoted_dip = None
                        daily_fill_open_ms = int(completed_candle["close_time_ms"]) + 1
                        open_dip = state.get("open_dip")
                        if prev_pos == "CASH" and isinstance(open_dip, dict) and _safe_int(open_dip.get("exit_open_ms", 0), 0) >= daily_fill_open_ms:
                            dip_entry_ref = float(open_dip.get("entry_ref_price") or open_dip.get("entry_fill") or step_fill_price)
                            handoff_ret_pct = ((step_fill_price / dip_entry_ref) - 1.0) * 100.0 if dip_entry_ref > 0 else 0.0
                            promoted_dip = {
                                "status": "PROMOTED_TO_TREND",
                                "signal_candle_ms": open_dip.get("signal_candle_ms", 0),
                                "entry_open_ms": open_dip.get("entry_open_ms", 0),
                                "exit_open_ms": daily_fill_open_ms,
                                "entry_time": open_dip.get("entry_time_utc", ""),
                                "exit_time": datetime.fromtimestamp(daily_fill_open_ms / 1000.0, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                                "entry_price": float(open_dip.get("entry_fill", dip_entry_ref)),
                                "entry_ref_price": dip_entry_ref,
                                "exit_price": float(step_fill_price),
                                "exit_ref_price": float(step_fill_price),
                                "pnl": 0.0,
                                "pnl_pct": handoff_ret_pct,
                                "drop_pct_actual": open_dip.get("drop_pct_actual"),
                                "vol_ratio": open_dip.get("vol_ratio"),
                                "reason": "v3 추세 승계 (매도 없이 전환)",
                                "exit_alert_sent": True,
                            }
                            state.setdefault("dip_trades", []).append(promoted_dip)
                            if open_dip.get("cost_basis"):
                                state["dip_cash"] = float(open_dip["cost_basis"])
                            state["open_dip"] = None
                        rv_txt = f" · 20d변동성 {snap['realized_vol_20d']:.0f}%" if snap.get("realized_vol_20d") is not None else ""
                        trade_info = {
                            "action": "매수 (BUY)" if prev_pos == "CASH" else "비중확대 (REBAL BUY)",
                            "price": buy_fill,
                            "reference_price": step_fill_price,
                            "qty": add_qty,
                            "fee": fee,
                            "target_weight": target_w,
                            "promoted_dip": promoted_dip,
                            "regime": snap["regime_label"],
                            "vol_ratio": snap["vol_ratio"],
                            "reason": f"목표 비중 {prev_w*100:.0f}% → {target_w*100:.0f}%{rv_txt}",
                            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        }
                    elif diff_val < -1e-6 and cur_qty > 0.0:
                        sell_qty = min(cur_qty, (-diff_val) / step_fill_price)
                        sell_fill = step_fill_price * (1.0 - slip_rate)
                        gross_cash = sell_qty * sell_fill
                        fee = gross_cash * fee_rate
                        net_cash = gross_cash - fee
                        frac_sold = sell_qty / cur_qty if cur_qty > 0 else 1.0
                        rem_basis = float(state.get("cost_basis", 0.0))
                        sold_basis = rem_basis * frac_sold
                        pnl = net_cash - sold_basis
                        pnl_pct = (pnl / sold_basis * 100.0) if sold_basis > 0 else 0.0
                        state["cash"] = cash + net_cash
                        state["btc_qty"] = max(0.0, cur_qty - sell_qty)
                        state["cost_basis"] = max(0.0, rem_basis - sold_basis)
                        state["position"] = "BTC" if state["btc_qty"] > 1e-12 else "CASH"
                        rv_txt = f" · 20d변동성 {snap['realized_vol_20d']:.0f}%" if snap.get("realized_vol_20d") is not None else ""
                        trade_info = {
                            "action": "비중축소 (REBAL TRIM)",
                            "price": sell_fill,
                            "reference_price": step_fill_price,
                            "qty": sell_qty,
                            "fee": fee,
                            "pnl": pnl,
                            "pnl_pct": pnl_pct,
                            "target_weight": target_w,
                            "regime": snap["regime_label"],
                            "vol_ratio": snap["vol_ratio"],
                            "reason": f"목표 비중 {prev_w*100:.0f}% → {target_w*100:.0f}%{rv_txt}",
                            "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                        }
                    if trade_info is not None:
                        trade_executed = True
                        state["trades"].append(trade_info)
                        save_state(state, state_path=state_path)
                        msg = format_trade_alert(config, state, trade_info, snap)
                        send_telegram_message(token, chat_id, msg)

            step_date_str = datetime.fromtimestamp(last_evaluated_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d")
            _record_oos_daily_point(state, step_date_str, float(snap["last_close"]))

        if snap is None:
            return state, "캔들 데이터 부족"
        if not trade_executed:
            save_state(state, state_path=state_path)
            if not manual_check and config.get("enable_daily_report", True):
                completed_date_str = datetime.fromtimestamp(latest_closed_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
                msg = format_daily_briefing(config, state, current_price, completed_date_str, snap)
                send_telegram_message(token, chat_id, msg)
        else:
            save_state(state, state_path=state_path)

    return state, {
        "current_price": current_price,
        "last_close": snap["last_close"],
        "current_sma": snap["active_sma"],
        "active_sma_len": snap["active_sma_len"],
        "regime_sma": snap["regime_sma"],
        "is_bull": snap["is_bull"],
        "last_vol": snap["last_vol"],
        "prior_vol_avg": snap["prior_vol_avg"],
        "vol_ratio": snap["vol_ratio"],
        "vol_mult": snap["vol_mult"],
        "vol_ma_len": snap["vol_ma_len"],
        "vol_ok": snap["vol_ok"],
        "position": state["position"],
        "trade_executed": trade_executed,
        "is_new_daily_close": is_new_daily_close
    }


def describe_position_action(state, snap, ref_price):
    """Return shared (badge, action_guide, cur_equity, total_ret, vol_mark) for briefings/status."""
    pos = state.get("position", "CASH")
    initial_seed = float(state.get("initial_seed", 10000.0))
    cur_equity = float(state.get("cash", 0.0)) + float(state.get("btc_qty", 0.0)) * float(ref_price)
    total_ret = (cur_equity / initial_seed - 1.0) * 100.0 if initial_seed > 0 else 0.0
    sma = snap["active_sma"]
    sma_len = snap["active_sma_len"]
    has_dip = isinstance(state.get("open_dip"), dict)
    tw_pct = float(state.get("target_weight", 1.0 if pos == "BTC" else 0.0)) * 100.0
    v_str = f" · 20d변동성 {snap['realized_vol_20d']:.0f}%" if snap.get("realized_vol_20d") is not None else ""

    if pos == "BTC":
        badge = f"🟢 [매수 유지 · BTC {tw_pct:.0f}%]" if tw_pct < 99.5 else "🟢 [매수 유지 · 보유]"
        action_guide = f"<b>관망(홀딩)</b> · {sma_len}일선(${sma:,.0f}) 이탈 시 매도{v_str}"
    elif has_dip:
        badge = "🟢 [단타 보유 · 추세 관망]"
        action_guide = f"급락 단타 진행 중 · 일봉 추세 <b>관망</b> (${sma:,.0f} 돌파+거래량 {snap['vol_mult']}배 대기)"
    elif snap["is_above"]:
        badge = "🟡 [관망 · 현금 대기]"
        action_guide = f"<b>관망</b> · {sma_len}일선 위지만 거래량 부족({snap['vol_ratio']:.2f}/{snap['vol_mult']}배)"
    else:
        badge = "🟡 [관망 · 현금 대기]"
        action_guide = f"<b>관망</b> · ${sma:,.0f} 돌파 + 거래량 {snap['vol_mult']}배 시 매수"

    vol_mark = "✅" if snap["vol_ok"] else "➖"
    return badge, action_guide, cur_equity, total_ret, vol_mark


def format_trade_alert(config, state, trade, snap):
    action = trade["action"]
    is_buy = ("매수" in action) or ("확대" in action)
    if "확대" in action:
        header = "🔵 [비중 확대] BTC 추가 매수"
    elif "축소" in action:
        header = "🟠 [비중 축소] 변동성·커널 리밸런싱"
    else:
        header = "🟢 [매수] BTC 진입 완료" if is_buy else "🔴 [매도] 전량 현금화"
    cur_equity = float(state.get("cash", 0.0)) + float(state.get("btc_qty", 0.0)) * trade["price"]
    initial_seed = float(state.get("initial_seed", 10000.0))
    total_ret = (cur_equity / initial_seed - 1.0) * 100.0 if initial_seed > 0 else 0.0
    promoted = trade.get("promoted_dip")
    promo_line = ""
    if isinstance(promoted, dict):
        promo_line = (
            f"\n• <b>단타 추세 승계:</b> 기존 단타(${promoted['entry_ref_price']:,.0f} · "
            f"{promoted['pnl_pct']:+.2f}%) 매도 없이 홀딩 연결"
        )

    tw_pct = float(state.get("target_weight", 1.0 if state["position"] == "BTC" else 0.0)) * 100.0
    pos_short = f"BTC {tw_pct:.0f}% / 현금 {max(0.0, 100.0 - tw_pct):.0f}%" if state["position"] == "BTC" else "USDT 100% 관망"
    reason_line = trade.get("reason") or f"{snap['active_sma_len']}일선(${snap['active_sma']:,.0f}) {'돌파' if is_buy else '이탈'} · 거래량 {snap['vol_ratio']:.2f}배"
    return f"""<b>{header}</b>
• <b>체결:</b> ${trade['price']:,.2f} ({snap['regime_label']})
• <b>근거:</b> {reason_line}{promo_line}
• <b>계좌:</b> {pos_short} · ${cur_equity:,.0f} (<b>{total_ret:+.2f}%</b>)
• <b>시각:</b> {trade['time']}"""


def format_daily_briefing(config, state, price, date_str, snap):
    sma = snap["active_sma"]
    sma_len = snap["active_sma_len"]
    diff_pct = (snap["last_close"] / sma - 1.0) * 100.0
    badge, action_guide, cur_equity, total_ret, vol_mark = describe_position_action(state, snap, price)
    extra_v3 = ""
    if snap.get("trend_kernel") == "sma_donchian" or float(snap.get("vol_target_pct", 0.0)) > 0.0:
        dm = snap.get("donchian_mid", sma)
        rv = snap.get("realized_vol_20d")
        rv_s = f"{rv:.1f}%" if rv is not None else "N/A"
        tw_s = f"{float(snap.get('target_weight', 0.0))*100:.0f}%"
        extra_v3 = f"\n• <b>v3 지표:</b> 돈치안{sma_len}중앙 ${dm:,.0f} · 20d변동성 {rv_s} · 목표비중 <b>{tw_s}</b>"
    return f"""<b>{badge} 일봉 마감 ({date_str})</b>
• <b>행동:</b> {action_guide}
• <b>종가/기준선:</b> ${snap['last_close']:,.0f} ({sma_len}일선 <b>{diff_pct:+.2f}%</b> · {snap['regime_label']}){extra_v3}
• <b>거래량:</b> {vol_mark} {snap['vol_ratio']:.2f}배 (기준 {snap['vol_mult']}배)
• <b>잔고:</b> ${cur_equity:,.0f} (<b>{total_ret:+.2f}%</b>)"""


def _resolve_touch_sma_lens(config, closed):
    """Resolve active SMA length, regime SMA length, and required volume multiplier from strategy config."""
    strategy = config.get("strategy_name", "daily_sma40_hybrid")
    is_hybrid = strategy in ("daily_sma40_hybrid", "daily_sma40_volume_c", "daily_sma_hybrid", "model_c")
    regime_len = max(2, _safe_int(config.get("regime_sma_len", 200), 200))
    closes = [float(c["close"]) for c in closed]
    sma_regime = (sum(closes[-regime_len:]) / float(regime_len)) if len(closes) >= regime_len else None
    is_bull = (sma_regime is not None) and (closes[-1] > sma_regime)
    if is_hybrid:
        bull_len = max(1, _safe_int(config.get("bull_sma_len", 40), 40))
        bear_len = max(1, _safe_int(config.get("bear_sma_len", 40), 40))
        active_len = bull_len if is_bull else bear_len
        req_mult = _safe_float(config.get("bull_vol_mult", 1.2), 1.2) if is_bull else _safe_float(config.get("bear_vol_mult", 1.8), 1.8)
    else:
        active_len = max(1, _safe_int(config.get("strategy_sma", 44), 44))
        req_mult = _safe_float(config.get("volume_multiplier", 1.2), 1.2)
    return active_len, regime_len, sma_regime, is_bull, req_mult


def check_sma_touch_alert(config, state, state_path=STATE_PATH):
    """Check if real-time BTC price approaches active trading SMA or regime SMA."""
    if not config.get("touch_alert_enabled", True):
        return None
    token = config.get("bot_token")
    chat_id = config.get("chat_id")
    if not token or not chat_id:
        return None

    cur_price = fetch_binance_ticker_price()
    if not cur_price:
        return None

    candles = fetch_binance_daily_candles(limit=_required_daily_limit(config))
    closed, _ = partition_daily_candles(candles)
    if not closed:
        return None

    active_len, regime_len, sma_regime, is_bull, req_mult = _resolve_touch_sma_lens(config, closed)
    if len(closed) < active_len:
        return None

    closes = [float(c["close"]) for c in closed]
    sma_active = sum(closes[-active_len:]) / float(active_len)

    threshold_pct = _safe_float(config.get("touch_threshold_pct", 0.5), 0.5)
    cooldown_sec = _safe_float(config.get("touch_cooldown_sec", 7200), 7200.0)
    now_ts = time.time()
    last_alerts = state.setdefault("last_touch_alerts", {})
    alert_sent = False

    diff_active = abs(cur_price - sma_active) / sma_active * 100.0
    if diff_active <= threshold_pct and (now_ts - _safe_float(last_alerts.get("sma_active", 0), 0.0) > cooldown_sec):
        pos_now = state.get("position", "CASH")
        if pos_now == "BTC":
            header = f"🟠 [매도 주의 · 관망] {active_len}일선 터치 (오차 {diff_active:.2f}%)"
            hint = f"UTC 00:00 일봉 마감 시 ${sma_active:,.0f} 아래면 <b>매도 전환</b>"
        else:
            header = f"🟡 [관망 · 매수 임박] {active_len}일선 터치 (오차 {diff_active:.2f}%)"
            hint = f"UTC 00:00 마감 시 ${sma_active:,.0f} 돌파 + 거래량 {req_mult}배면 <b>익일 매수</b>"
        msg = f"""<b>{header}</b>
• <b>현재가 / {active_len}일선:</b> ${cur_price:,.2f} / ${sma_active:,.2f}
• <b>판단 기준:</b> {hint}"""
        if send_telegram_message(token, chat_id, msg):
            last_alerts["sma_active"] = now_ts
            save_state(state, state_path=state_path)
            alert_sent = True

    diff_regime = None
    if sma_regime is not None:
        diff_regime = abs(cur_price - sma_regime) / sma_regime * 100.0
        if diff_regime <= threshold_pct and (now_ts - _safe_float(last_alerts.get("sma_regime", 0), 0.0) > cooldown_sec):
            side_tag = "강세장" if cur_price >= sma_regime else "약세장"
            msg = f"""<b>🟡 [관망 · 체제선 접근] {regime_len}일선 터치 ({diff_regime:.2f}%)</b>
• <b>현재가 / {regime_len}일선:</b> ${cur_price:,.2f} / ${sma_regime:,.2f}
• <b>참고:</b> {side_tag} 기준선 경계 (거래량 필터 자동 전환 구간)"""
            if send_telegram_message(token, chat_id, msg):
                last_alerts["sma_regime"] = now_ts
                save_state(state, state_path=state_path)
                alert_sent = True

    return {
        "cur_price": cur_price,
        "sma_active": sma_active,
        "sma_regime": sma_regime,
        "diff_active": diff_active,
        "diff_regime": diff_regime,
        "alert_sent": alert_sent
    }


def _compute_1h_baseline_vol(closed_1h, before_ms, days=14):
    """Average hourly volume over the trailing continuous `days` closed 1h bars strictly before `before_ms`."""
    if not closed_1h:
        return None
    prior = sorted(
        (c for c in closed_1h if int(c["close_time_ms"]) < int(before_ms) and float(c.get("volume", 0)) > 0),
        key=lambda c: int(c["open_time_ms"]),
    )
    need = int(days) * 24
    if len(prior) < need:
        return None
    window = prior[-need:]
    if int(window[-1]["open_time_ms"]) - int(window[0]["open_time_ms"]) != (need - 1) * 3_600_000:
        return None
    return sum(float(c["volume"]) for c in window) / float(need)


def compute_short_dip_snapshot(closed_5m, lookback_bars=24, drop_pct=4.5, scan_bars=1, vol_mult=1.0, closed_1h=None, min_open_ms=None):
    """Evaluate causal 2h dip + volume climax condition across closed 5m candles."""
    if not closed_5m:
        raise ValueError("5분봉 마감 데이터가 부족합니다.")
    lookback_bars = max(1, _safe_int(lookback_bars, 24))
    vol_mult = _safe_float(vol_mult, 1.0)
    by_open = {int(c["open_time_ms"]): c for c in closed_5m}

    def _window_vol_ratio(idx, ref_bar):
        if idx + 1 < lookback_bars:
            return None
        ref_open = int(ref_bar["open_time_ms"])
        bar_open = int(closed_5m[idx]["open_time_ms"])
        window_start_ms = ref_open + BAR_5M_MS
        recent_slice = closed_5m[idx + 1 - lookback_bars:idx + 1]
        if (
            len(recent_slice) != lookback_bars
            or int(recent_slice[0]["open_time_ms"]) != window_start_ms
            or int(recent_slice[-1]["open_time_ms"]) != bar_open
        ):
            return None
        recent_vol = sum(float(c["volume"]) for c in recent_slice)
        eff_bars = float(lookback_bars)
        base_vol = None
        required = 4032 + lookback_bars
        if idx + 1 >= required:
            first_idx = idx + 1 - required
            if bar_open - int(closed_5m[first_idx]["open_time_ms"]) != (required - 1) * BAR_5M_MS:
                return None
            prior = closed_5m[first_idx:idx + 1 - lookback_bars]
            base_vol = (sum(float(c["volume"]) for c in prior) / 4032.0) * eff_bars
        elif closed_1h:
            hr_avg = _compute_1h_baseline_vol(closed_1h, window_start_ms, days=14)
            if hr_avg and hr_avg > 0:
                base_vol = hr_avg * (eff_bars / 12.0)
        if base_vol and base_vol > 0:
            return recent_vol / base_vol
        return None

    latest = closed_5m[-1]
    latest_ref = by_open.get(int(latest["open_time_ms"]) - lookback_bars * BAR_5M_MS)
    latest_ret_pct = ((latest["close"] / latest_ref["close"]) - 1.0) * 100.0 if (latest_ref and latest_ref["close"] > 0) else None
    latest_vol_ratio = _window_vol_ratio(len(closed_5m) - 1, latest_ref) if latest_ref else None

    threshold = -abs(_safe_float(drop_pct, 4.5))
    start_idx = max(0, len(closed_5m) - max(1, _safe_int(scan_bars, 1)))
    triggered_bar = None
    triggered_ref = None
    triggered_ret_pct = None
    triggered_vol_ratio = None
    for idx in range(start_idx, len(closed_5m)):
        bar = closed_5m[idx]
        bar_ms = int(bar["open_time_ms"])
        if min_open_ms is not None and bar_ms <= int(min_open_ms):
            continue
        ref = by_open.get(bar_ms - lookback_bars * BAR_5M_MS)
        if not ref or ref["close"] <= 0:
            continue
        ret_pct = ((bar["close"] / ref["close"]) - 1.0) * 100.0
        if ret_pct <= threshold:
            v_ratio = _window_vol_ratio(idx, ref)
            if vol_mult > 1.0 and (v_ratio is None or v_ratio < vol_mult):
                continue
            triggered_bar = bar
            triggered_ref = ref
            triggered_ret_pct = ret_pct
            triggered_vol_ratio = v_ratio
            break

    return {
        "latest_bar": latest,
        "latest_ref": latest_ref,
        "latest_ret_pct": latest_ret_pct,
        "latest_vol_ratio": latest_vol_ratio,
        "triggered": triggered_bar is not None,
        "triggered_bar": triggered_bar,
        "triggered_ref": triggered_ref,
        "triggered_ret_pct": triggered_ret_pct,
        "triggered_vol_ratio": triggered_vol_ratio,
        "lookback_bars": lookback_bars,
        "drop_pct": _safe_float(drop_pct, 4.5),
        "vol_mult": vol_mult,
    }


def summarize_dip_ledger(config, state):
    """Return summary stats for closed 2h dip paper trades and trend promotions."""
    seed = float(state.get("dip_initial_seed") or config.get("initial_seed_usdt", 10000.0))
    dip_cash = float(state.get("dip_cash", seed) if state.get("dip_cash") is not None else seed)
    trades = state.get("dip_trades", []) or []
    closed = [t for t in trades if t.get("status", "CLOSED_TIME") == "CLOSED_TIME"]
    promoted = [t for t in trades if t.get("status") == "PROMOTED_TO_TREND"]
    wins = sum(1 for t in closed if float(t.get("pnl", 0.0)) > 0)
    losses = len(closed) - wins
    win_rate_pct = (wins / float(len(closed)) * 100.0) if closed else 0.0
    total_ret_pct = ((dip_cash / seed) - 1.0) * 100.0 if seed > 0 else 0.0
    return {
        "dip_seed": seed,
        "dip_cash": dip_cash,
        "total_ret_pct": total_ret_pct,
        "closed_count": len(closed),
        "wins": wins,
        "losses": losses,
        "promoted_count": len(promoted),
        "win_rate_pct": win_rate_pct,
    }


def format_dip_exit_alert(config, state, record):
    """Build concise Telegram notification for a 2h fixed-time short-dip exit settlement."""
    summary = summarize_dip_ledger(config, state)
    pnl_pct = float(record.get("pnl_pct", 0.0))
    icon = "🟢" if pnl_pct > 0 else "🔴"
    vr = record.get("vol_ratio")
    drop_actual = record.get("drop_pct_actual")
    trigger_note = ""
    if drop_actual is not None:
        trigger_note = f"{float(drop_actual):+.2f}%"
        if vr is not None:
            trigger_note += f" · {float(vr):.1f}x"
    entry_ref = float(record.get("entry_ref_price") or 0.0)
    exit_ref = float(record.get("exit_ref_price") or 0.0)
    exit_utc = datetime.fromtimestamp(int(record["exit_open_ms"]) / 1000.0, tz=timezone.utc)
    exit_tor = exit_utc.astimezone(TORONTO_TZ)
    hold_h = int(record.get("hold_bars", 24)) * 5 / 60.0

    return f"""<b>🔴 [매도 · {hold_h:g}h 단타] 청산 타임 도래 ({icon} {pnl_pct:+.2f}%)</b>
• <b>행동:</b> <b>단타 전량 매도(청산)</b> · ${entry_ref:,.0f} → ${exit_ref:,.0f}
• <b>신호/시각:</b> {trigger_note or '-'} · {exit_tor.strftime('%H:%M %Z')} ({exit_utc.strftime('%H:%M UTC')})
• <b>누적 원장:</b> {summary['closed_count']}전 {summary['wins']}승 {summary['losses']}패 ({summary['win_rate_pct']:.1f}%) · <b>{summary['total_ret_pct']:+.2f}%</b> (${summary['dip_cash']:,.0f})"""


def _retry_unsent_dip_exits(config, state, state_path, cur_ms, token, chat_id):
    """Retry recent closed dip exit notifications that previously failed to deliver."""
    if not token or not chat_id:
        return False
    trades = state.get("dip_trades", []) or []
    retried_ok = False
    dirty = False
    for rec in trades:
        if rec.get("status", "CLOSED_TIME") != "CLOSED_TIME" or rec.get("exit_alert_sent", True):
            continue
        age_ms = cur_ms - int(rec.get("exit_open_ms", cur_ms))
        if age_ms > 24 * 3600 * 1000:
            rec["exit_alert_sent"] = True
            dirty = True
            continue
        if send_telegram_message(token, chat_id, format_dip_exit_alert(config, state, rec)):
            rec["exit_alert_sent"] = True
            dirty = True
            retried_ok = True
    if dirty:
        save_state(state, state_path=state_path)
    return retried_ok


def _format_dip_entry_alert(config, open_dip, cur_ms, live_price):
    lookback_bars = max(1, _safe_int(config.get("dip_alert_lookback_bars", 24), 24))
    drop_pct = _safe_float(config.get("dip_alert_drop_pct", 4.5), 4.5)
    vol_mult = _safe_float(config.get("dip_alert_vol_mult", 2.5), 2.5)
    hold_bars = max(1, _safe_int(open_dip.get("hold_bars", config.get("dip_alert_hold_bars", 24)), 24))
    lb_hours = lookback_bars * 5 / 60.0
    hold_hours = hold_bars * 5 / 60.0
    entry_open_ms = _safe_int(open_dip.get("entry_open_ms", 0), 0)
    exit_open_ms = _safe_int(open_dip.get("exit_open_ms", 0), 0)
    exit_utc = datetime.fromtimestamp(exit_open_ms / 1000.0, tz=timezone.utc)
    exit_tor = exit_utc.astimezone(TORONTO_TZ)
    lag_min = max(0, int((cur_ms - entry_open_ms) // 60000))
    vr = open_dip.get("vol_ratio")
    vol_note = f" · 거래량 <b>{float(vr):.2f}배</b>" if (vol_mult > 1.0 and vr is not None) else ""
    vol_crit = f"·{vol_mult:g}x" if vol_mult > 1.0 else ""
    lag_tag = f" ({lag_min}분 경과)" if lag_min >= 5 else ""
    ret_pct = _safe_float(open_dip.get("drop_pct_actual", 0.0), 0.0)
    price_disp = _pick_positive_price(live_price, open_dip.get("entry_live_price"), open_dip.get("entry_ref_price"))
    return f"""<b>🟢 [매수 · {hold_hours:g}h 단타] 급락 반등 타점{lag_tag}</b>
• <b>행동:</b> <b>BTC 단타 매수</b> (현금대기 전용 · {hold_hours:g}h 후 매도)
• <b>현재가:</b> <b>${price_disp:,.2f}</b> (급락 <b>{ret_pct:+.2f}%</b>{vol_note})
• <b>기준/청산:</b> {lb_hours:g}h -{drop_pct:g}%{vol_crit} · 예정 <b>{exit_tor.strftime('%H:%M %Z')}</b> ({exit_utc.strftime('%H:%M UTC')})"""


def _retry_unsent_dip_entries(config, state, state_path, cur_ms, token, chat_id, live_price):
    """Retry delivering an active open_dip entry notification that previously failed to send."""
    if not token or not chat_id:
        return False
    open_dip = state.get("open_dip")
    if not isinstance(open_dip, dict) or open_dip.get("entry_alert_sent", True):
        return False
    exit_open_ms = _safe_int(open_dip.get("exit_open_ms", 0), 0)
    if exit_open_ms > 0 and cur_ms >= exit_open_ms:
        return False
    msg = _format_dip_entry_alert(config, open_dip, cur_ms, live_price)
    if send_telegram_message(token, chat_id, msg):
        open_dip["entry_alert_sent"] = True
        state["last_dip_alert_ts"] = cur_ms / 1000.0
        state["last_dip_alert_candle_ms"] = _safe_int(open_dip.get("signal_candle_ms", 0), 0)
        save_state(state, state_path=state_path)
        return True
    return False


def _settle_due_dip_trade(config, state, state_path, cur_ms, closed_5m, forming_5m, token, chat_id, allow_ticker=True):
    """Settle an active 2h short-dip paper trade once its canonical exit bar opens."""
    open_dip = state.get("open_dip")
    if not isinstance(open_dip, dict):
        return None
    exit_open_ms = _safe_int(open_dip.get("exit_open_ms", 0), 0)
    if exit_open_ms <= 0 or cur_ms < exit_open_ms:
        return None

    all_bars = sorted(closed_5m + forming_5m, key=lambda c: int(c["open_time_ms"]))
    exit_bar = None
    if all_bars and int(all_bars[0]["open_time_ms"]) > exit_open_ms:
        # Long outage pushed exit_open_ms outside the fetched 5m window: query exact bar at exit_open_ms.
        if allow_ticker:
            hist_bars = fetch_binance_5m_candles(limit=5, start_time_ms=exit_open_ms)
            if hist_bars:
                exit_bar = next(
                    (c for c in sorted(hist_bars, key=lambda x: int(x["open_time_ms"]))
                     if int(c["open_time_ms"]) >= exit_open_ms and _safe_float(c.get("open", 0), 0.0) > 0),
                    None,
                )
        if exit_bar is None:
            return None
    else:
        exit_bar = next((c for c in all_bars if int(c["open_time_ms"]) >= exit_open_ms and _safe_float(c.get("open", 0), 0.0) > 0), None)
    if exit_bar is not None:
        exit_ref_price = float(exit_bar["open"])
        actual_exit_ms = int(exit_bar["open_time_ms"])
    elif cur_ms < exit_open_ms + BAR_5M_MS:
        return None
    else:
        live_p = fetch_binance_ticker_price() if allow_ticker else None
        exit_ref_price = _pick_positive_price(
            live_p,
            forming_5m[-1].get("close") if forming_5m else None,
            closed_5m[-1].get("close") if closed_5m else None,
            open_dip.get("entry_ref_price"),
        )
        actual_exit_ms = exit_open_ms

    fee_rate = float(config.get("fee_rate", 0.0015))
    slip_rate = float(config.get("slippage_rate", 0.0005))
    exit_fill = exit_ref_price * (1.0 - slip_rate)
    qty = float(open_dip.get("qty", 0.0))
    cost_basis = float(open_dip.get("cost_basis", 0.0))
    gross_cash = qty * exit_fill
    exit_fee = gross_cash * fee_rate
    net_cash = gross_cash - exit_fee
    pnl = net_cash - cost_basis
    pnl_pct = (pnl / cost_basis * 100.0) if cost_basis > 0 else 0.0

    record = {
        "status": "CLOSED_TIME",
        "signal_candle_ms": int(open_dip.get("signal_candle_ms", 0)),
        "entry_open_ms": int(open_dip.get("entry_open_ms", 0)),
        "exit_open_ms": actual_exit_ms,
        "hold_bars": int(open_dip.get("hold_bars", 24)),
        "entry_time": open_dip.get("entry_time_utc", ""),
        "exit_time": datetime.fromtimestamp(actual_exit_ms / 1000.0, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        "entry_price": float(open_dip.get("entry_fill", 0.0)),
        "entry_ref_price": float(open_dip.get("entry_ref_price", 0.0)),
        "entry_live_price": float(open_dip.get("entry_live_price", open_dip.get("entry_ref_price", 0.0))),
        "exit_price": exit_fill,
        "exit_ref_price": exit_ref_price,
        "exit_fee": exit_fee,
        "pnl": pnl,
        "pnl_pct": pnl_pct,
        "drop_pct_actual": open_dip.get("drop_pct_actual"),
        "vol_ratio": open_dip.get("vol_ratio"),
        "reason": f"{int(open_dip.get('hold_bars', 24))}봉({int(open_dip.get('hold_bars', 24)) * 5 / 60:g}h) 고정 시간청산",
        "exit_alert_sent": False,
    }
    state["dip_cash"] = net_cash
    state["open_dip"] = None
    state.setdefault("dip_trades", []).append(record)
    save_state(state, state_path=state_path)

    if token and chat_id:
        if send_telegram_message(token, chat_id, format_dip_exit_alert(config, state, record)):
            record["exit_alert_sent"] = True
            save_state(state, state_path=state_path)
    return record


def check_dip_alert(config, state, state_path=STATE_PATH, now_ms=None, candles_5m=None, scan_bars=1, candles_1h=None):
    """Send 2h dip entry/exit alerts, settle 2h paper trades, and enforce CASH-only gating."""
    enabled = config.get("dip_alert_enabled", True)
    pos = state.get("position", "CASH")
    tw_cur = _safe_float(state.get("target_weight", 1.0 if pos == "BTC" else 0.0), 1.0 if pos == "BTC" else 0.0)
    is_full_btc = (pos != "CASH") and (tw_cur >= 0.99)
    cash_only = config.get("dip_alert_cash_only", True)
    has_open_dip = isinstance(state.get("open_dip"), dict)
    has_unsent_exit = any(
        (t.get("status", "CLOSED_TIME") == "CLOSED_TIME" and not t.get("exit_alert_sent", True))
        for t in (state.get("dip_trades", []) or [])
    )

    if not enabled and not has_open_dip and not has_unsent_exit:
        return {"checked": False, "alert_sent": False, "skipped_reason": "disabled"}

    if enabled and cash_only and is_full_btc and not has_open_dip and not has_unsent_exit:
        return {"checked": False, "alert_sent": False, "skipped_reason": "in_btc_position", "position": pos}

    token = config.get("bot_token")
    chat_id = config.get("chat_id")
    lookback_bars = int(config.get("dip_alert_lookback_bars", 24))
    drop_pct = float(config.get("dip_alert_drop_pct", 4.5))
    vol_mult = float(config.get("dip_alert_vol_mult", 2.5))
    hold_bars = int(config.get("dip_alert_hold_bars", 24))
    cooldown_sec = float(config.get("dip_alert_cooldown_sec", 7200))
    cur_ms = int(now_ms) if now_ms is not None else int(time.time() * 1000)

    exit_retried = _retry_unsent_dip_exits(config, state, state_path, cur_ms, token, chat_id)

    raw_5m = candles_5m if candles_5m is not None else fetch_binance_5m_candles(limit=max(lookback_bars + scan_bars + 10, hold_bars + 48, 72))
    if not raw_5m:
        return {"checked": False, "alert_sent": False, "skipped_reason": "fetch_failed", "exit_retried": exit_retried}

    closed_5m, forming_5m = partition_candles(raw_5m, now_ms=cur_ms)
    if not closed_5m:
        return {"checked": False, "alert_sent": False, "skipped_reason": "no_closed_bars", "exit_retried": exit_retried}

    settled = _settle_due_dip_trade(
        config, state, state_path, cur_ms, closed_5m, forming_5m, token, chat_id,
        allow_ticker=(candles_5m is None),
    )

    if not enabled:
        return {
            "checked": False,
            "alert_sent": False,
            "skipped_reason": "disabled",
            "exit_settled": settled is not None,
            "exit_alert_sent": bool(settled and settled.get("exit_alert_sent")),
            "settled_trade": settled,
            "exit_retried": exit_retried,
        }

    if cash_only and is_full_btc:
        return {
            "checked": False,
            "alert_sent": False,
            "skipped_reason": "in_btc_position",
            "position": pos,
            "exit_settled": settled is not None,
            "exit_alert_sent": bool(settled and settled.get("exit_alert_sent")),
            "settled_trade": settled,
            "exit_retried": exit_retried,
        }

    # Bear-only regime gate (<= SMA200): block NEW idle-cash dip entries in bull regime (after settling any open dip)
    bear_only = bool(config.get("dip_alert_bear_only", False))
    if bear_only and not isinstance(state.get("open_dip"), dict):
        is_bull_reg = state.get("last_regime_is_bull")
        if is_bull_reg is None and candles_5m is None:
            try:
                d_raw = fetch_binance_daily_candles(limit=_required_daily_limit(config))
                d_closed, _ = partition_daily_candles(d_raw, now_ms=cur_ms)
                reg_len = max(2, _safe_int(config.get("regime_sma_len", 200), 200))
                if len(d_closed) >= reg_len:
                    d_closes = [float(x["close"]) for x in d_closed]
                    reg_sma = sum(d_closes[-reg_len:]) / float(reg_len)
                    is_bull_reg = bool(d_closes[-1] > reg_sma)
                    state["last_regime_is_bull"] = is_bull_reg
            except Exception:
                is_bull_reg = None
        if is_bull_reg is True:
            return {
                "checked": False,
                "alert_sent": False,
                "skipped_reason": "bull_regime_blocked",
                "position": pos,
                "exit_settled": settled is not None,
                "exit_alert_sent": bool(settled and settled.get("exit_alert_sent")),
                "settled_trade": settled,
                "exit_retried": exit_retried,
            }

    # Retry unsent entry alert for an already-recorded open_dip (prevents ledger loss & delivers on recovery)
    existing_open = state.get("open_dip")
    if isinstance(existing_open, dict) and existing_open.get("entry_alert_sent") is False:
        live_p_retry = _pick_positive_price(
            fetch_binance_ticker_price() if candles_5m is None else None,
            forming_5m[-1].get("close") if forming_5m else None,
            closed_5m[-1].get("close") if closed_5m else None,
            existing_open.get("entry_live_price"),
        )
        entry_retried = _retry_unsent_dip_entries(config, state, state_path, cur_ms, token, chat_id, live_p_retry)
        return {
            "checked": True,
            "alert_sent": bool(entry_retried),
            "entry_retried": bool(entry_retried),
            "position": pos,
            "triggered_ret_pct": existing_open.get("drop_pct_actual"),
            "triggered_vol_ratio": existing_open.get("vol_ratio"),
            "triggered_candle_ms": _safe_int(existing_open.get("signal_candle_ms", 0), 0),
            "live_price": live_p_retry,
            "open_dip": state.get("open_dip"),
            "exit_settled": settled is not None,
            "exit_alert_sent": bool(settled and settled.get("exit_alert_sent")),
            "settled_trade": settled,
            "exit_retried": exit_retried,
        }

    raw_1h = candles_1h
    if raw_1h is None and candles_5m is None and vol_mult > 1.0:
        try:
            raw_1h = get_cached_binance_1h_candles(
                limit=_required_1h_limit(lookback_bars, scan_bars),
                now_ts=cur_ms / 1000.0,
            )
        except Exception:
            raw_1h = None
    closed_1h, _ = partition_candles(raw_1h, now_ms=cur_ms) if raw_1h else ([], [])

    last_candle_ms = _safe_int(state.get("last_dip_alert_candle_ms", 0), 0)
    if not isinstance(state.get("open_dip"), dict) and last_candle_ms > 0 and int(scan_bars) > 1:
        snap = compute_short_dip_snapshot(
            closed_5m, lookback_bars=lookback_bars, drop_pct=drop_pct, scan_bars=scan_bars,
            vol_mult=vol_mult, closed_1h=closed_1h or None, min_open_ms=last_candle_ms,
        )
        if not snap["triggered"]:
            snap = compute_short_dip_snapshot(
                closed_5m, lookback_bars=lookback_bars, drop_pct=drop_pct, scan_bars=scan_bars,
                vol_mult=vol_mult, closed_1h=closed_1h or None,
            )
    else:
        snap = compute_short_dip_snapshot(
            closed_5m, lookback_bars=lookback_bars, drop_pct=drop_pct, scan_bars=scan_bars,
            vol_mult=vol_mult, closed_1h=closed_1h or None,
        )
    if not snap["triggered"]:
        return {
            "checked": True,
            "alert_sent": False,
            "position": pos,
            "latest_ret_pct": snap["latest_ret_pct"],
            "latest_vol_ratio": snap["latest_vol_ratio"],
            "exit_settled": settled is not None,
            "exit_alert_sent": bool(settled and settled.get("exit_alert_sent")),
            "settled_trade": settled,
            "exit_retried": exit_retried,
        }

    trig_bar = snap["triggered_bar"]
    trig_ref = snap["triggered_ref"]
    trig_ms = int(trig_bar["open_time_ms"])
    now_ts = cur_ms / 1000.0
    last_ts = _safe_float(state.get("last_dip_alert_ts", 0.0), 0.0)

    last_settled_exit_ms = 0
    for rec in (state.get("dip_trades", []) or []):
        if isinstance(rec, dict):
            ex_ms = _safe_int(rec.get("exit_open_ms", 0), 0)
            if ex_ms > last_settled_exit_ms:
                last_settled_exit_ms = ex_ms

    entry_open_ms = trig_ms + BAR_5M_MS
    same_bar_reentry = last_settled_exit_ms > 0 and entry_open_ms <= last_settled_exit_ms
    if last_candle_ms > 0:
        in_cooldown = (
            trig_ms <= last_candle_ms
            or same_bar_reentry
            or (cooldown_sec > 0 and (trig_ms - last_candle_ms) <= int(cooldown_sec * 1000))
        )
    else:
        in_cooldown = same_bar_reentry or (last_ts > 0 and cooldown_sec > 0 and (now_ts - last_ts) <= cooldown_sec)

    if in_cooldown:
        return {
            "checked": True,
            "alert_sent": False,
            "skipped_reason": "cooldown",
            "position": pos,
            "triggered_ret_pct": snap["triggered_ret_pct"],
            "triggered_vol_ratio": snap["triggered_vol_ratio"],
            "triggered_candle_ms": trig_ms,
            "exit_settled": settled is not None,
            "exit_alert_sent": bool(settled and settled.get("exit_alert_sent")),
            "settled_trade": settled,
            "exit_retried": exit_retried,
        }

    if isinstance(state.get("open_dip"), dict):
        return {
            "checked": True,
            "alert_sent": False,
            "skipped_reason": "dip_open",
            "position": pos,
            "triggered_ret_pct": snap["triggered_ret_pct"],
            "triggered_vol_ratio": snap["triggered_vol_ratio"],
            "triggered_candle_ms": trig_ms,
            "exit_settled": settled is not None,
            "exit_alert_sent": bool(settled and settled.get("exit_alert_sent")),
            "settled_trade": settled,
            "exit_retried": exit_retried,
        }

    if not token or not chat_id:
        return {
            "checked": True,
            "alert_sent": False,
            "skipped_reason": "no_credentials",
            "triggered_ret_pct": snap["triggered_ret_pct"],
            "exit_settled": settled is not None,
            "exit_retried": exit_retried,
        }

    live_price = _pick_positive_price(
        fetch_binance_ticker_price() if candles_5m is None else None,
        forming_5m[-1].get("close") if forming_5m else None,
        trig_bar.get("close"),
    )

    exit_open_ms = entry_open_ms + hold_bars * BAR_5M_MS
    all_5m_by_open = {int(c["open_time_ms"]): c for c in (closed_5m + forming_5m)}
    entry_bar = all_5m_by_open.get(entry_open_ms)
    entry_ref_price = _pick_positive_price(
        entry_bar.get("open") if entry_bar else None,
        live_price,
    )

    fee_rate = _safe_float(config.get("fee_rate", 0.0015), 0.0015)
    slip_rate = _safe_float(config.get("slippage_rate", 0.0005), 0.0005)
    entry_fill = entry_ref_price * (1.0 + slip_rate)
    dip_seed = _safe_float(state.get("dip_initial_seed") or config.get("initial_seed_usdt", 10000.0), 10000.0)
    dip_cash = _safe_float(state.get("dip_cash", dip_seed) if state.get("dip_cash") is not None else dip_seed, dip_seed)
    dip_qty = dip_cash / (entry_fill * (1.0 + fee_rate)) if (entry_fill > 0 and dip_cash > 0) else 0.0
    entry_fee = dip_qty * entry_fill * fee_rate
    entry_utc = datetime.fromtimestamp(entry_open_ms / 1000.0, tz=timezone.utc)
    vr = snap["triggered_vol_ratio"]

    new_open_dip = {
        "signal_candle_ms": trig_ms,
        "entry_open_ms": entry_open_ms,
        "exit_open_ms": exit_open_ms,
        "hold_bars": hold_bars,
        "entry_ref_price": entry_ref_price,
        "entry_live_price": float(live_price),
        "entry_fill": entry_fill,
        "qty": dip_qty,
        "cost_basis": dip_cash,
        "entry_fee": entry_fee,
        "drop_pct_actual": float(snap["triggered_ret_pct"]),
        "vol_ratio": float(vr) if vr is not None else None,
        "entry_time_utc": entry_utc.strftime("%Y-%m-%d %H:%M UTC"),
        "entry_alert_sent": False,
    }
    if cur_ms < exit_open_ms:
        state["open_dip"] = new_open_dip
        save_state(state, state_path=state_path)

    msg = _format_dip_entry_alert(config, new_open_dip, cur_ms, live_price)
    sent = send_telegram_message(token, chat_id, msg)
    if sent:
        state["last_dip_alert_ts"] = now_ts
        state["last_dip_alert_candle_ms"] = trig_ms
        if isinstance(state.get("open_dip"), dict) and _safe_int(state["open_dip"].get("signal_candle_ms", 0), 0) == trig_ms:
            state["open_dip"]["entry_alert_sent"] = True
        save_state(state, state_path=state_path)

    return {
        "checked": True,
        "alert_sent": bool(sent),
        "position": pos,
        "triggered_ret_pct": snap["triggered_ret_pct"],
        "triggered_vol_ratio": snap["triggered_vol_ratio"],
        "triggered_candle_ms": trig_ms,
        "live_price": live_price,
        "open_dip": state.get("open_dip"),
        "exit_settled": settled is not None,
        "exit_alert_sent": bool(settled and settled.get("exit_alert_sent")),
        "settled_trade": settled,
        "exit_retried": exit_retried,
    }


def get_dip_status_text(config, state, now_ms=None, candles_5m=None, candles_1h=None):
    lookback_bars = int(config.get("dip_alert_lookback_bars", 24))
    drop_pct = float(config.get("dip_alert_drop_pct", 4.5))
    vol_mult = float(config.get("dip_alert_vol_mult", 2.5))
    hold_bars = int(config.get("dip_alert_hold_bars", 24))
    cooldown_sec = float(config.get("dip_alert_cooldown_sec", 7200))
    pos = state.get("position", "CASH")
    cash_only = config.get("dip_alert_cash_only", True)
    enabled = config.get("dip_alert_enabled", True)

    raw_5m = candles_5m if candles_5m is not None else fetch_binance_5m_candles(limit=max(lookback_bars + 10, 72))
    if not raw_5m:
        return "⚠️ 바이낸스 5분봉 시세 조회 실패"

    raw_1h = candles_1h
    cur_ms = int(now_ms) if now_ms is not None else int(time.time() * 1000)
    if raw_1h is None and candles_5m is None and vol_mult > 1.0:
        try:
            raw_1h = get_cached_binance_1h_candles(
                limit=_required_1h_limit(lookback_bars, 1),
                now_ts=cur_ms / 1000.0,
            )
        except Exception:
            raw_1h = None

    closed_5m, forming_5m = partition_candles(raw_5m, now_ms=cur_ms)
    if not closed_5m:
        return "⚠️ 확정 5분봉 데이터가 부족합니다."
    closed_1h, _ = partition_candles(raw_1h, now_ms=cur_ms) if raw_1h else ([], [])

    snap = compute_short_dip_snapshot(
        closed_5m, lookback_bars=lookback_bars, drop_pct=drop_pct, scan_bars=1,
        vol_mult=vol_mult, closed_1h=closed_1h or None,
    )
    latest = snap["latest_bar"]
    ref = snap["latest_ref"]
    ret_pct = snap["latest_ret_pct"]
    vol_ratio = snap["latest_vol_ratio"]
    live_price = _pick_positive_price(
        fetch_binance_ticker_price() if candles_5m is None else None,
        forming_5m[-1].get("close") if forming_5m else None,
        latest.get("close"),
    )
    lb_hours = lookback_bars * 5 / 60.0
    hold_hours = hold_bars * 5 / 60.0
    vol_tag = f" (거래량 {vol_mult:g}x)" if vol_mult > 1.0 else ""

    open_dip = state.get("open_dip")
    open_block = ""
    if isinstance(open_dip, dict):
        exit_open_ms = _safe_int(open_dip.get("exit_open_ms", 0), 0)
        rem_min = max(0, int((exit_open_ms - cur_ms) // 60000)) if exit_open_ms > 0 else 0
        entry_ref = _pick_positive_price(open_dip.get("entry_ref_price"), open_dip.get("entry_fill"))
        mtm_pct = ((live_price / entry_ref) - 1.0) * 100.0 if entry_ref > 0 else 0.0
        exit_tor_str = (
            datetime.fromtimestamp(exit_open_ms / 1000.0, tz=timezone.utc).astimezone(TORONTO_TZ).strftime('%H:%M %Z')
            if exit_open_ms > 0 else "시간 미정"
        )
        header_action = f"🟢 [단타 매수 보유] 진행 중 (청산 약 {rem_min}분 뒤)"
        open_block = (
            f"\n• <b>진입 → 청산:</b> ${entry_ref:,.0f} (<b>{mtm_pct:+.2f}%</b>) → {exit_tor_str} 매도"
        )
    elif not enabled:
        header_action = "🟡 [관망 · 단타 꺼짐]"
    elif cash_only and pos != "CASH":
        header_action = "🟡 [단타 일시정지 · 모델 C 매수 보유 중]"
    elif bool(config.get("dip_alert_bear_only", False)) and state.get("last_regime_is_bull") is True:
        header_action = "🟡 [단타 일시정지 · 강세장(200일선 위 · 약세전용 모드)]"
    else:
        remain_cd = max(0, int(cooldown_sec - (cur_ms / 1000.0 - float(state.get("last_dip_alert_ts", 0.0) or 0.0))))
        bear_tag = " · 약세전용" if bool(config.get("dip_alert_bear_only", False)) else ""
        if remain_cd > 0:
            header_action = f"🟡 [관망 · 쿨다운] {remain_cd // 60}분 뒤 재감시"
        elif snap["triggered"]:
            header_action = f"🟢 [매수 타점] 급락+거래량 조건 충족!{bear_tag}"
        else:
            header_action = f"🟡 [관망 · 단타 대기] {lb_hours:g}h -{drop_pct:g}%{vol_tag}{bear_tag}"

    ret_str = f"<b>{ret_pct:+.2f}%</b>" if ret_pct is not None else "데이터 부족"
    remain_drop = (ret_pct + drop_pct) if ret_pct is not None else None
    dist_str = f"목표까지 {remain_drop:+.2f}%p" if remain_drop is not None else "결측"
    vol_line = f"\n• <b>거래량 배수:</b> <b>{vol_ratio:.2f}배</b> (기준 {vol_mult:g}배)" if (vol_mult > 1.0 and vol_ratio is not None) else ""

    summary = summarize_dip_ledger(config, state)
    ledger_line = ""
    if summary["closed_count"] > 0 or summary["promoted_count"] > 0:
        ledger_line = f"\n• <b>단타 누적:</b> {summary['closed_count']}전 {summary['wins']}승 {summary['losses']}패 ({summary['win_rate_pct']:.1f}%) · <b>{summary['total_ret_pct']:+.2f}%</b>"

    return f"""<b>{header_action}</b>
• <b>현재가 / {lb_hours:g}h 등락:</b> ${live_price:,.2f} · {ret_str} ({dist_str}){vol_line}{open_block}{ledger_line}"""


def get_touch_status_text(config):
    candles = fetch_binance_daily_candles(limit=_required_daily_limit(config))
    closed, forming = partition_daily_candles(candles)
    if not closed:
        return "⚠️ 바이낸스 일봉 조회 실패 또는 데이터 부족"

    active_len, regime_len, sma_regime, _, _ = _resolve_touch_sma_lens(config, closed)
    if len(closed) < active_len:
        return "⚠️ 바이낸스 일봉 조회 실패 또는 데이터 부족"

    closes = [float(c["close"]) for c in closed]
    cur_price = _pick_positive_price(
        fetch_binance_ticker_price(),
        forming[-1].get("close") if forming else None,
        closes[-1],
    )
    sma_active = sum(closes[-active_len:]) / float(active_len)

    threshold = _safe_float(config.get("touch_threshold_pct", 0.5), 0.5)
    gap_a = (cur_price / sma_active - 1.0) * 100.0
    flag_a = "🚨 터치권!" if abs(gap_a) <= threshold else "관망"
    reg_line = ""
    if sma_regime is not None:
        gap_r = (cur_price / sma_regime - 1.0) * 100.0
        flag_r = "🚨 터치권!" if abs(gap_r) <= threshold else ("강세권" if cur_price > sma_regime else "약세권")
        reg_line = f"\n• <b>{regime_len}일선(체제선):</b> ${sma_regime:,.0f} (<b>{gap_r:+.2f}%</b> · {flag_r})"

    return f"""<b>🎯 [기준선 터치 감시]</b> (현재가 ${cur_price:,.2f})
• <b>{active_len}일선(매매선):</b> ${sma_active:,.0f} (<b>{gap_a:+.2f}%</b> · {flag_a}){reg_line}
• <b>알림 기준:</b> ±{threshold}% 이내 근접 시 즉시 알림"""


def get_current_status_text(config, state):
    candles = fetch_binance_daily_candles(limit=_required_daily_limit(config))
    closed, forming = partition_daily_candles(candles)
    if not closed:
        return "⚠️ 바이낸스 시세 조회 실패"

    prev_pos = state.get("position", "CASH")
    try:
        snap = compute_live_signal_snapshot(closed, config, prev_pos=prev_pos, prev_state=state)
    except ValueError as e:
        return f"⚠️ {e}"

    current_price = _pick_positive_price(
        fetch_binance_ticker_price(),
        forming[-1].get("close") if forming else None,
        snap["last_close"],
    )
    sma = snap["active_sma"]
    sma_len = snap["active_sma_len"]
    diff_pct = (current_price / sma - 1.0) * 100.0

    pos = prev_pos
    initial_seed = _safe_float(state.get("initial_seed", 10000.0), 10000.0)
    cur_equity = _safe_float(state.get("cash", 0.0), 0.0) + _safe_float(state.get("btc_qty", 0.0), 0.0) * current_price
    total_ret = (cur_equity / initial_seed - 1.0) * 100.0 if initial_seed > 0 else 0.0
    vol_ok_txt = "✅충족" if snap["vol_ok"] else "미달"
    tw_pct = _safe_float(state.get("target_weight", 1.0 if pos == "BTC" else 0.0), 1.0 if pos == "BTC" else 0.0) * 100.0

    dip_lb_h = max(1, _safe_int(config.get("dip_alert_lookback_bars", 24), 24)) * 5 / 60.0
    dip_drop = _safe_float(config.get("dip_alert_drop_pct", 4.5), 4.5)
    dip_vm = _safe_float(config.get("dip_alert_vol_mult", 2.5), 2.5)
    dip_spec = f"{dip_lb_h:g}h -{dip_drop:g}% · {dip_vm:g}x" if dip_vm > 1.0 else f"{dip_lb_h:g}h -{dip_drop:g}%"
    open_dip = state.get("open_dip")
    if not config.get("dip_alert_enabled", True):
        dip_status_str = "꺼짐"
    elif isinstance(open_dip, dict):
        ex_ms = _safe_int(open_dip.get("exit_open_ms", 0), 0)
        ex_str = datetime.fromtimestamp(ex_ms / 1000.0, tz=timezone.utc).strftime('%H:%M UTC') if ex_ms > 0 else "시간 미정"
        dip_status_str = f"단타 보유 중 (청산 {ex_str})"
    elif config.get("dip_alert_cash_only", True) and pos != "CASH" and tw_pct >= 99.0:
        dip_status_str = "정지 (BTC 100% 보유 중)"
    else:
        dip_status_str = f"감시 중 ({dip_spec})"

    if pos == "BTC":
        header = f"🟢 [매수 유지 · BTC {tw_pct:.0f}% 보유 중]" if tw_pct < 99.5 else "🟢 [매수 유지 · BTC 보유 중]"
        next_cond = f"{sma_len}일선(${sma:,.0f}) 이탈 시 <b>매도</b>"
    elif isinstance(open_dip, dict):
        header = "🟢 [단타 매수 보유] · 추세는 관망"
        next_cond = f"${sma:,.0f} 돌파+거래량 {snap['vol_mult']}배 시 <b>추세 매수</b>"
    else:
        header = "🟡 [관망 · 현금 100% 대기 중]"
        next_cond = f"${sma:,.0f} 돌파 + 거래량 {snap['vol_mult']}배 시 <b>매수</b>"

    dip_summary = summarize_dip_ledger(config, state)
    dip_ledger_str = ""
    if dip_summary["closed_count"] > 0 or dip_summary["promoted_count"] > 0:
        dip_ledger_str = f" · {dip_summary['wins']}승{dip_summary['losses']}패({dip_summary['total_ret_pct']:+.1f}%)"

    v3_line = ""
    if snap.get("trend_kernel") == "sma_donchian" or float(snap.get("vol_target_pct", 0.0)) > 0.0:
        rv = snap.get("realized_vol_20d")
        rv_s = f"{rv:.1f}%" if rv is not None else "N/A"
        v3_line = f"\n• <b>v3 듀얼/변동성:</b> 돈치안{sma_len}중앙 ${snap.get('donchian_mid', sma):,.0f} · 20d변동성 {rv_s} (목표 {snap.get('vol_target_pct', 0):g}%)"

    return f"""<b>{header}</b>
• <b>현재가 / {sma_len}일선:</b> ${current_price:,.2f} (<b>{diff_pct:+.2f}%</b> · {snap['regime_label']}){v3_line}
• <b>거래량:</b> {snap['vol_ratio']:.2f}배 / 기준 {snap['vol_mult']}배 ({vol_ok_txt})
• <b>다음 기준:</b> {next_cond}
• <b>단타 감시:</b> {dip_status_str}{dip_ledger_str}
• <b>모의잔고:</b> ${cur_equity:,.0f} (<b>{total_ret:+.2f}%</b>)"""


def _record_oos_daily_point(state, date_str, btc_price):
    """Record daily unseen live-OOS equity snapshot for Model C paper account."""
    p = _safe_float(btc_price, 0.0)
    if p <= 0.0 or not date_str:
        return
    pos = state.get("position", "CASH")
    cur_eq = _safe_float(state.get("cash", 0.0), 0.0) + _safe_float(state.get("btc_qty", 0.0), 0.0) * p
    if not state.get("oos_start_utc"):
        state["oos_start_utc"] = f"{date_str}T00:00:00Z"
    if _safe_float(state.get("oos_start_btc_price", 0.0), 0.0) <= 0.0:
        state["oos_start_btc_price"] = round(p, 2)
    if _safe_float(state.get("oos_start_equity", 0.0), 0.0) <= 0.0:
        state["oos_start_equity"] = round(cur_eq if cur_eq > 0 else _safe_float(state.get("initial_seed", 10000.0), 10000.0), 2)
    hist = state.setdefault("oos_equity_history", [])
    if not isinstance(hist, list):
        hist = []
        state["oos_equity_history"] = hist
    pt = {"date": str(date_str), "equity": round(cur_eq, 2), "btc_price": round(p, 2), "position": pos}
    if hist and isinstance(hist[-1], dict) and hist[-1].get("date") == date_str:
        hist[-1] = pt
    else:
        hist.append(pt)
    if len(hist) > 1500:
        del hist[:-1500]


def compute_live_oos_scorecard(config, state, current_btc_price=None):
    """Compute true unseen live forward-OOS scorecard separate from observed historical backtests."""
    seed = _safe_float(state.get("oos_start_equity") or state.get("initial_seed") or config.get("initial_seed_usdt", 10000.0), 10000.0)
    pos = state.get("position", "CASH")
    cur_p = _pick_positive_price(
        current_btc_price,
        state.get("entry_price") if pos == "BTC" else None,
        (state.get("oos_equity_history") or [{}])[-1].get("btc_price") if state.get("oos_equity_history") else None,
        state.get("oos_start_btc_price"),
    )
    cash_now = _safe_float(state.get("cash", seed if pos == "CASH" else 0.0), seed if pos == "CASH" else 0.0)
    btc_now = _safe_float(state.get("btc_qty", 0.0), 0.0)
    cur_eq = (cash_now + btc_now * cur_p) if (cur_p > 0 or pos == "CASH") else (cash_now + _safe_float(state.get("cost_basis", seed), seed))
    model_c_ret = ((cur_eq / seed) - 1.0) * 100.0 if seed > 0 else 0.0

    hist = state.get("oos_equity_history") if isinstance(state.get("oos_equity_history"), list) else []
    eq_series = [seed] + [_safe_float(x.get("equity", seed), seed) for x in hist if isinstance(x, dict)]
    eq_series.append(cur_eq)
    peak = eq_series[0] if eq_series else seed
    mdd = 0.0
    for v in eq_series:
        if v > peak:
            peak = v
        if peak > 0:
            mdd = max(mdd, (peak - v) / peak)

    trades = [t for t in (state.get("trades") or []) if isinstance(t, dict)]
    closed_trades = [t for t in trades if "매도" in str(t.get("action", "")) or "SELL" in str(t.get("action", "")).upper()]
    wins = sum(1 for t in closed_trades if _safe_float(t.get("pnl", 0.0), 0.0) > 0.0)
    losses = len(closed_trades) - wins
    wr = (wins / float(len(closed_trades)) * 100.0) if closed_trades else 0.0

    start_utc = str(state.get("oos_start_utc") or "")
    if not start_utc:
        last_ms = _safe_int(state.get("last_evaluated_candle_ms", 0), 0)
        if last_ms > 0:
            start_utc = datetime.fromtimestamp((last_ms + 86_400_000) / 1000.0, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        else:
            start_utc = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        start_dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
        days_elapsed = max(0.0, (datetime.now(timezone.utc) - start_dt).total_seconds() / 86400.0)
    except ValueError:
        days_elapsed = float(len(hist))

    start_btc = _pick_positive_price(
        state.get("oos_start_btc_price"),
        hist[0].get("btc_price") if (hist and isinstance(hist[0], dict)) else None,
        cur_p,
    )
    btc_bh_ret = ((cur_p / start_btc) - 1.0) * 100.0 if (start_btc > 0 and cur_p > 0) else 0.0
    alpha_pct = model_c_ret - btc_bh_ret
    dip_summary = summarize_dip_ledger(config, state)
    sample_status = (
        f"초기 미관측 OOS 관측 중 ({len(closed_trades)}/10거래 · 통계 판단 전)"
        if len(closed_trades) < 10 else
        f"실시간 미관측 OOS 누적 중 ({len(closed_trades)}거래)"
    )

    return {
        "oos_start_utc": start_utc,
        "days_elapsed": round(days_elapsed, 1),
        "history_days": len(hist),
        "position": pos,
        "initial_equity": round(seed, 2),
        "current_equity": round(cur_eq, 2),
        "model_c_return_pct": round(model_c_ret, 2),
        "model_c_mdd_pct": round(mdd * 100.0, 2),
        "closed_trades": len(closed_trades),
        "wins": wins,
        "losses": losses,
        "win_rate_pct": round(wr, 1),
        "btc_start_price": round(start_btc, 2) if start_btc > 0 else None,
        "btc_current_price": round(cur_p, 2) if cur_p > 0 else None,
        "btc_bh_return_pct": round(btc_bh_ret, 2),
        "alpha_vs_bh_pct": round(alpha_pct, 2),
        "dip_summary": dip_summary,
        "sample_status": sample_status,
    }


def get_oos_status_text(config, state, current_btc_price=None):
    """Render true unseen live-OOS scorecard for Telegram /oos command."""
    live_p = _pick_positive_price(current_btc_price, fetch_binance_ticker_price() if current_btc_price is None else None)
    sc = compute_live_oos_scorecard(config, state, current_btc_price=live_p if live_p > 0 else None)
    pos_txt = "BTC 100% 보유" if sc["position"] == "BTC" else "USDT 현금 대기"
    btc_line = (
        f"<b>{sc['btc_bh_return_pct']:+.2f}%</b> (${sc['btc_start_price']:,.0f} → ${sc['btc_current_price']:,.0f})"
        if (sc["btc_start_price"] and sc["btc_current_price"]) else "기준가 대기 중"
    )
    dip = sc["dip_summary"]
    return f"""<b>🧪 [실시간 미관측 OOS 점수판]</b>
• <b>추적 시작:</b> {sc['oos_start_utc'][:10]} ({sc['days_elapsed']:.0f}일 경과 · {pos_txt})
• <b>모델 C 실전 모의:</b> <b>{sc['model_c_return_pct']:+.2f}%</b> (${sc['current_equity']:,.0f} · MDD -{sc['model_c_mdd_pct']:.2f}%)
• <b>동일기간 BTC 보유:</b> {btc_line} (알파 <b>{sc['alpha_vs_bh_pct']:+.2f}%p</b>)
• <b>완료 거래:</b> {sc['closed_trades']}전 {sc['wins']}승 {sc['losses']}패 (승률 {sc['win_rate_pct']:.1f}%)
• <b>2h 단타 참고:</b> {dip['closed_count']}전 {dip['wins']}승 {dip['losses']}패 ({dip['total_ret_pct']:+.2f}%)
• <b>신뢰도 상태:</b> {sc['sample_status']}"""


if __name__ == "__main__":
    cfg = load_config()
    st = load_state(cfg)
    print("Testing Binance Data & Model C Signal Snapshot...")
    st, info = evaluate_signal_and_step(cfg, st, manual_check=True)
    print("\n" + "=" * 50)
    print(get_current_status_text(cfg, st))
    print("=" * 50)
    print("Evaluation info:", info)
