"""
BTC Quant Lab - Paper Trading Engine (Model C: 40/40 Hybrid + 200 SMA Regime Switch)
100% Pure Python Standard Library (Python 3.9+ compatible)
Strategy: 일봉 40/40 SMA + 200일선 체제전환 + 거래량(강세 1.2x·20일 / 약세 1.8x·5일) 필터
"""
import json
import math
import os
import ssl
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
    "dip_alert_enabled": True,    # 현금대기 중 2시간 -4.5%(거래량 2.5x) 급락 알림
    "dip_alert_cash_only": True,  # 모델 C 현금(CASH) 대기 상태일 때만 급락 알림 발송
    "dip_alert_lookback_bars": 24,  # 24 × 5분 = 2시간
    "dip_alert_drop_pct": 4.5,    # 2시간 종가 대비 -4.5% 이상 급락 시 알림
    "dip_alert_vol_mult": 2.5,    # 직전 14일 평균 대비 투매 거래량 배수 필터 (1.0=미사용)
    "dip_alert_hold_bars": 24,    # 권장 보유 2시간(24봉) 안내용
    "dip_alert_cooldown_sec": 7200  # 급락 알림 중복 방지 쿨다운 (2시간)
}


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


def _atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f".{path.name}.tmp.{os.getpid()}")
    tmp_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp_path, path)


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
        "last_touch_alerts": {},
        "last_dip_alert_ts": 0.0,
        "last_dip_alert_candle_ms": 0,
        "dip_initial_seed": seed,
        "dip_cash": seed,
        "open_dip": None,
        "dip_trades": [],
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
        if data.get("open_dip") is not None and not isinstance(data.get("open_dip"), dict):
            data["open_dip"] = None
        if float(data.get("dip_initial_seed", 0) or 0) <= 0:
            data["dip_initial_seed"] = float(base["dip_initial_seed"])
        if data.get("dip_cash") is None:
            data["dip_cash"] = float(data["dip_initial_seed"])
        return data
    except Exception as e:
        print(f"State load error (backing up corrupt file): {e}")
        try:
            corrupt = path.with_name(f"{path.name}.corrupt.{int(time.time())}")
            os.replace(path, corrupt)
        except Exception:
            pass
        _atomic_write_json(path, base)
        return base


def save_state(state, state_path=STATE_PATH):
    _atomic_write_json(state_path, state)


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


def _fetch_binance_klines(interval, limit):
    """Fetch spot klines from Binance public API in chronological order."""
    path = f"/api/v3/klines?symbol=BTCUSDT&interval={interval}&limit={int(limit)}"
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


def fetch_binance_5m_candles(limit=40):
    """Fetch 5-minute candles from Binance Spot public API in chronological order."""
    return _fetch_binance_klines("5m", limit)


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


def partition_candles(candles, now_ms=None):
    """Split candles strictly into closed valid bars vs forming open bar (QIS-7 Closed Candle Rule)."""
    if now_ms is None:
        now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    closed = []
    forming = []
    for c in sorted(candles, key=lambda x: x["open_time_ms"]):
        if c["close_time_ms"] < now_ms:
            if c["volume"] > 0 and c["low"] > 0 and c["close"] > 0:
                closed.append(c)
        else:
            forming.append(c)
    return closed, forming


def partition_daily_candles(candles, now_ms=None):
    """Split daily candles strictly into closed valid bars vs forming open bar (QIS-7 Closed Candle Rule)."""
    return partition_candles(candles, now_ms=now_ms)


def compute_live_signal_snapshot(closed_candles, config, prev_pos="CASH"):
    """Exact Model C (40/40 Hybrid + 200 SMA) or legacy single-SMA snapshot on closed daily bars."""
    strategy = config.get("strategy_name", "daily_sma40_hybrid")
    is_hybrid = strategy in ("daily_sma40_hybrid", "daily_sma40_volume_c", "daily_sma_hybrid", "model_c")

    if is_hybrid:
        bull_sma_len = int(config.get("bull_sma_len", 40))
        bear_sma_len = int(config.get("bear_sma_len", 40))
        regime_sma_len = int(config.get("regime_sma_len", 200))
        bull_vol_mult = float(config.get("bull_vol_mult", 1.2))
        bull_vol_ma = int(config.get("bull_vol_ma", 20))
        bear_vol_mult = float(config.get("bear_vol_mult", 1.8))
        bear_vol_ma = int(config.get("bear_vol_ma", 5))
    else:
        sma_len = int(config.get("strategy_sma", 44))
        bull_sma_len = bear_sma_len = sma_len
        regime_sma_len = int(config.get("regime_sma_len", 200))
        bull_vol_mult = bear_vol_mult = float(config.get("volume_multiplier", 1.2))
        bull_vol_ma = bear_vol_ma = int(config.get("volume_ma_len", 20))

    min_needed = max(bull_sma_len, bear_sma_len, bull_vol_ma + 1, bear_vol_ma + 1)
    if len(closed_candles) < min_needed:
        raise ValueError(f"완료 일봉 부족: 최소 {min_needed}일 필요, 현재 {len(closed_candles)}일")

    completed_candle = closed_candles[-1]
    closes = [c["close"] for c in closed_candles]
    vols = [c["volume"] for c in closed_candles]
    last_close = completed_candle["close"]
    last_vol = completed_candle["volume"]

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
    prior_vols = vols[-(vol_ma_len + 1):-1]
    prior_vol_avg = sum(prior_vols) / float(vol_ma_len)
    vol_ratio = (last_vol / prior_vol_avg) if prior_vol_avg > 0 else 0.0
    vol_ok = (len(prior_vols) == vol_ma_len) and (prior_vol_avg > 0) and (last_vol >= vol_mult * prior_vol_avg)

    is_above = last_close > active_sma
    is_below = last_close < active_sma

    if prev_pos == "CASH":
        target_pos = "BTC" if (is_above and vol_ok) else "CASH"
    else:
        target_pos = "CASH" if is_below else "BTC"

    return {
        "strategy": strategy,
        "is_hybrid": is_hybrid,
        "completed_candle": completed_candle,
        "last_close": last_close,
        "active_sma_len": active_sma_len,
        "active_sma": active_sma,
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
        "is_above": is_above,
        "is_below": is_below,
        "target_pos": target_pos,
    }


def _strategy_display_name(config, snap=None):
    strategy = config.get("strategy_name", "daily_sma40_hybrid")
    if strategy in ("daily_sma40_hybrid", "daily_sma40_volume_c", "daily_sma_hybrid", "model_c"):
        b_sma = config.get("bull_sma_len", 40)
        r_sma = config.get("bear_sma_len", 40)
        reg = config.get("regime_sma_len", 200)
        b_m = config.get("bull_vol_mult", 1.2)
        b_l = config.get("bull_vol_ma", 20)
        s_m = config.get("bear_vol_mult", 1.8)
        s_l = config.get("bear_vol_ma", 5)
        return f"👑 모델 C ({b_sma}/{r_sma}일선 · {reg}일 체제전환 · 불 {b_m}x/{b_l}일 · 약세 {s_m}x/{s_l}일)"
    sma = config.get("strategy_sma", 44)
    vm = config.get("volume_multiplier", 1.2)
    vl = config.get("volume_ma_len", 20)
    return f"일봉 {sma}일 SMA + 거래량({vm}x, {vl}일)"


def evaluate_signal_and_step(config, state, manual_check=False, state_path=STATE_PATH, now_ms=None, candles=None):
    """Evaluate Model C (or configured strategy) on latest closed daily candle and execute virtual fill."""
    fee_rate = float(config.get("fee_rate", 0.0015))
    slip_rate = float(config.get("slippage_rate", 0.0005))
    token = config.get("bot_token")
    chat_id = config.get("chat_id")

    if candles is None:
        candles = fetch_binance_daily_candles(limit=250)
    closed, forming = partition_daily_candles(candles, now_ms=now_ms)
    if not closed:
        print("Not enough closed daily candles fetched from Binance.")
        return state, "캔들 데이터 부족"

    prev_pos = state.get("position", "CASH")
    try:
        snap = compute_live_signal_snapshot(closed, config, prev_pos=prev_pos)
    except ValueError as e:
        print(f"Signal computation error: {e}")
        return state, str(e)

    completed_candle = snap["completed_candle"]
    last_evaluated_ms = completed_candle["open_time_ms"]
    is_new_daily_close = (last_evaluated_ms != state.get("last_evaluated_candle_ms", 0))

    if forming and forming[0]["open_time_ms"] == completed_candle["close_time_ms"] + 1:
        fill_price = forming[0]["open"]
        current_price = forming[-1]["close"]
    else:
        ticker_p = fetch_binance_ticker_price()
        fill_price = ticker_p or completed_candle["close"]
        current_price = fill_price

    completed_date_str = datetime.fromtimestamp(last_evaluated_ms / 1000, timezone.utc).strftime("%Y-%m-%d")
    trade_executed = False
    trade_info = None

    # Execute position transition only once per newly closed daily candle (prevents intraday /check desync).
    if is_new_daily_close:
        state["last_evaluated_candle_ms"] = last_evaluated_ms
        target_pos = snap["target_pos"]
        state["last_signal"] = target_pos

        if prev_pos != target_pos:
            if target_pos == "BTC":
                cash = float(state.get("cash", config.get("initial_seed_usdt", 10000.0)))
                buy_fill = fill_price * (1.0 + slip_rate)
                qty = cash / (buy_fill * (1.0 + fee_rate))
                fee = qty * buy_fill * fee_rate
                state["cash"] = 0.0
                state["btc_qty"] = qty
                state["cost_basis"] = cash
                state["position"] = "BTC"
                state["entry_price"] = buy_fill
                state["entry_time_utc"] = datetime.now(timezone.utc).isoformat()

                # Canonical Model C Combined rule: if an active 2h dip trade spans across daily BTC buy,
                # carry it forward into the trend position without selling separately.
                promoted_dip = None
                daily_fill_open_ms = int(completed_candle["close_time_ms"]) + 1
                open_dip = state.get("open_dip")
                if isinstance(open_dip, dict) and int(open_dip.get("exit_open_ms", 0) or 0) > daily_fill_open_ms:
                    dip_entry_ref = float(open_dip.get("entry_ref_price") or open_dip.get("entry_fill") or fill_price)
                    handoff_ret_pct = ((fill_price / dip_entry_ref) - 1.0) * 100.0 if dip_entry_ref > 0 else 0.0
                    promoted_dip = {
                        "status": "PROMOTED_TO_TREND",
                        "signal_candle_ms": open_dip.get("signal_candle_ms", 0),
                        "entry_open_ms": open_dip.get("entry_open_ms", 0),
                        "exit_open_ms": daily_fill_open_ms,
                        "entry_time": open_dip.get("entry_time_utc", ""),
                        "exit_time": datetime.fromtimestamp(daily_fill_open_ms / 1000.0, timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
                        "entry_price": float(open_dip.get("entry_fill", dip_entry_ref)),
                        "entry_ref_price": dip_entry_ref,
                        "exit_price": float(fill_price),
                        "exit_ref_price": float(fill_price),
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
                    "reference_price": fill_price,
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
                sell_fill = fill_price * (1.0 - slip_rate)
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
                    "reference_price": fill_price,
                    "qty": qty,
                    "fee": fee,
                    "pnl": pnl,
                    "pnl_pct": pnl_pct,
                    "regime": snap["regime_label"],
                    "vol_ratio": snap["vol_ratio"],
                    "reason": f"{snap['active_sma_len']}일선 하향 이탈 청산",
                    "time": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
                }
                state["trades"].append(trade_info)

            save_state(state, state_path=state_path)
            msg = format_trade_alert(config, state, trade_info, snap)
            send_telegram_message(token, chat_id, msg)
        else:
            save_state(state, state_path=state_path)
            if not manual_check and config.get("enable_daily_report", True):
                msg = format_daily_briefing(config, state, current_price, completed_date_str, snap)
                send_telegram_message(token, chat_id, msg)

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


def format_trade_alert(config, state, trade, snap):
    action = trade["action"]
    icon = "🚀" if "매수" in action else "🛡️"
    cur_equity = float(state["cash"]) if state["position"] == "CASH" else float(state["btc_qty"]) * trade["price"]
    initial_seed = float(state.get("initial_seed", 10000.0))
    total_ret = (cur_equity / initial_seed - 1.0) * 100.0 if initial_seed > 0 else 0.0
    reg_str = f"${snap['regime_sma']:,.2f}" if snap.get("regime_sma") is not None else "준비 중"
    promoted = trade.get("promoted_dip")
    promo_line = ""
    if isinstance(promoted, dict):
        promo_line = (
            f"\n• <b>🔁 단타 추세 승계:</b> 보유 중이던 2h 단타(기준가 ${promoted['entry_ref_price']:,.2f}, "
            f"전환 시점 {promoted['pnl_pct']:+.2f}%) 매도 없이 모델 C 추세로 자동 승계!"
        )

    msg = f"""<b>{icon} [BTC Quant Lab 실시간 모의매매 신호 체결]</b>
━━━━━━━━━━━━━━━━━━━━
• <b>전략:</b> {_strategy_display_name(config, snap)}
• <b>체제 판별:</b> {snap['regime_label']} (200일선: {reg_str})
• <b>주문 구분:</b> <b>{action}</b>
• <b>체결 가격:</b> ${trade['price']:,.2f}
• <b>체결 사유:</b> {trade.get('reason', '전략 신호 발생')}
• <b>기준 일봉 종가:</b> ${snap['last_close']:,.2f} ({snap['active_sma_len']}일선: ${snap['active_sma']:,.2f})
• <b>거래량 지표:</b> {snap['vol_ma_len']}일 평균의 <b>{snap['vol_ratio']:.2f}배</b> (기준 {snap['vol_mult']}배){promo_line}
━━━━━━━━━━━━━━━━━━━━
• <b>포지션 상태:</b> {'🟢 BTC 100% 보유' if state['position'] == 'BTC' else '⚪ 100% USDT 현금 (휩소 방어)'}
• <b>현재 평가잔고:</b> ${cur_equity:,.2f} (누적 수익률: <b>{total_ret:+.2f}%</b>)
• <b>체결 시각:</b> {trade['time']}
"""
    return msg


def format_daily_briefing(config, state, price, date_str, snap):
    pos = state.get("position", "CASH")
    initial_seed = float(state.get("initial_seed", 10000.0))
    cur_equity = float(state["cash"]) if pos == "CASH" else float(state["btc_qty"]) * price
    total_ret = (cur_equity / initial_seed - 1.0) * 100.0 if initial_seed > 0 else 0.0
    sma = snap["active_sma"]
    sma_len = snap["active_sma_len"]
    diff_pct = (snap["last_close"] / sma - 1.0) * 100.0
    reg_str = f"${snap['regime_sma']:,.2f}" if snap.get("regime_sma") is not None else "워밍업 부족"

    status_icon = "🟢" if pos == "BTC" else "⚪"
    status_text = "BTC 보유 (상승 추세 유지)" if pos == "BTC" else "USDT 현금 대기 (하락/관망 방어)"

    if pos == "BTC":
        guidance = f"현재 {sma_len}일선 위에서 안전하게 추세를 홀딩 중입니다. ({sma_len}일선 하향 이탈 전까지 매도 없음)"
    elif snap["is_above"]:
        guidance = (
            f"가격이 {sma_len}일선 위에 있으나 거래량이 {snap['vol_ma_len']}일 평균의 {snap['vol_ratio']:.2f}배로 "
            f"{snap['regime_label']} 기준({snap['vol_mult']}배)에 미달하여 현금을 유지합니다."
        )
    else:
        guidance = (
            f"가격이 {sma_len}일선 아래({diff_pct:+.2f}%)에 머물러 현금 100% 보존 중입니다. "
            f"{sma_len}일선 돌파 & 거래량 {snap['vol_mult']}배 충족 시 익일 시가 매수합니다."
        )

    vol_tag = f"🟢 {snap['vol_ratio']:.2f}배 ({snap['vol_mult']}배 충족)" if snap["vol_ok"] else f"⚪ {snap['vol_ratio']:.2f}배 ({snap['vol_mult']}배 미달)"

    msg = f"""<b>📅 [BTC Quant Lab 일봉 마감 브리핑]</b>
━━━━━━━━━━━━━━━━━━━━
• <b>전략:</b> {_strategy_display_name(config, snap)}
• <b>기준일자:</b> {date_str} (UTC 00:00 마감)
• <b>장세 체제:</b> {snap['regime_label']} (200일선: {reg_str})
• <b>확정 종가:</b> ${snap['last_close']:,.2f}
• <b>일봉 {sma_len}일선:</b> ${sma:,.2f} (괴리율: <b>{diff_pct:+.2f}%</b>)
• <b>일봉 거래량:</b> {snap['last_vol']:,.1f} BTC ({snap['vol_ma_len']}일 평균: {snap['prior_vol_avg']:,.1f} BTC)
• <b>거래량 필터:</b> <b>{vol_tag}</b>
━━━━━━━━━━━━━━━━━━━━
• <b>현재 포지션:</b> {status_icon} <b>{status_text}</b>
• <b>가상 평가자산:</b> ${cur_equity:,.2f}
• <b>누적 모의수익률:</b> <b>{total_ret:+.2f}%</b> (초기 ${initial_seed:,.0f})
━━━━━━━━━━━━━━━━━━━━
💡 <b>오늘의 전략 가이드:</b>
{guidance}
"""
    return msg


def check_sma_touch_alert(config, state, state_path=STATE_PATH):
    """Check if real-time BTC price approaches active trading SMA (40d) or regime SMA (200d)."""
    if not config.get("touch_alert_enabled", True):
        return None
    token = config.get("bot_token")
    chat_id = config.get("chat_id")
    if not token or not chat_id:
        return None

    cur_price = fetch_binance_ticker_price()
    if not cur_price:
        return None

    candles = fetch_binance_daily_candles(limit=250)
    closed, _ = partition_daily_candles(candles)
    if len(closed) < 40:
        return None

    closes = [c["close"] for c in closed]
    active_len = int(config.get("bull_sma_len", config.get("strategy_sma", 40)))
    regime_len = int(config.get("regime_sma_len", 200))
    sma_active = sum(closes[-active_len:]) / float(active_len)
    sma_regime = sum(closes[-regime_len:]) / float(regime_len) if len(closes) >= regime_len else None

    threshold_pct = float(config.get("touch_threshold_pct", 0.5))
    cooldown_sec = float(config.get("touch_cooldown_sec", 7200))
    now_ts = time.time()
    last_alerts = state.setdefault("last_touch_alerts", {})
    alert_sent = False

    diff_active = abs(cur_price - sma_active) / sma_active * 100.0
    if diff_active <= threshold_pct and (now_ts - last_alerts.get("sma_active", 0) > cooldown_sec):
        direction = f"위에서 {active_len}일선으로 하강 터치 ⬇️" if cur_price >= sma_active else f"아래에서 {active_len}일선으로 상승 돌파 터치 ⬆️"
        is_bull = sma_regime is not None and cur_price > sma_regime
        req_mult = config.get("bull_vol_mult", 1.2) if is_bull else config.get("bear_vol_mult", 1.8)
        req_ma = config.get("bull_vol_ma", 20) if is_bull else config.get("bear_vol_ma", 5)
        msg = f"""<b>🔔 [BTC {active_len}일선(모델 C 매매선) 실시간 터치 경보!]</b>
━━━━━━━━━━━━━━━━━━━━
• <b>비트코인 현재가:</b> ${cur_price:,.2f}
• <b>일봉 {active_len}일선:</b> ${sma_active:,.2f}
• <b>이평선 괴리율:</b> <b>{diff_active:.2f}%</b>
• <b>터치 형태:</b> {direction}
━━━━━━━━━━━━━━━━━━━━
💡 <b>체결 조건 ({'강세장' if is_bull else '약세장'} 모드):</b>
일봉 마감(UTC 00:00) 시 종가 &gt; {active_len}일선 및 당일 거래량 ≥ 직전 {req_ma}일 평균 × <b>{req_mult}배</b> 충족 시 익일 시가 매수."""
        if send_telegram_message(token, chat_id, msg):
            last_alerts["sma_active"] = now_ts
            save_state(state, state_path=state_path)
            alert_sent = True

    diff_regime = None
    if sma_regime is not None:
        diff_regime = abs(cur_price - sma_regime) / sma_regime * 100.0
        if diff_regime <= threshold_pct and (now_ts - last_alerts.get("sma_regime", 0) > cooldown_sec):
            direction = f"위에서 {regime_len}일선으로 하강 터치 ⬇️" if cur_price >= sma_regime else f"아래에서 {regime_len}일선으로 상승 돌파 터치 ⬆️"
            msg = f"""<b>🔔 [BTC {regime_len}일선(거시 체제선) 실시간 터치 경보!]</b>
━━━━━━━━━━━━━━━━━━━━
• <b>비트코인 현재가:</b> ${cur_price:,.2f}
• <b>일봉 {regime_len}일선:</b> ${sma_regime:,.2f}
• <b>이평선 괴리율:</b> <b>{diff_regime:.2f}%</b>
• <b>터치 형태:</b> {direction}
━━━━━━━━━━━━━━━━━━━━
💡 <b>참고:</b> 200일선 상회 시 강세 필터(1.2x·20일), 하회 시 약세 스나이퍼(1.8x·5일)로 장세 모드가 전환됩니다."""
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
    """Average hourly volume over the trailing `days` closed 1h bars strictly before `before_ms`."""
    if not closed_1h:
        return None
    prior = [float(c["volume"]) for c in closed_1h if int(c["close_time_ms"]) <= int(before_ms) and float(c.get("volume", 0)) > 0]
    need = int(days) * 24
    if len(prior) < 24:
        return None
    window = prior[-need:]
    return sum(window) / float(len(window))


def compute_short_dip_snapshot(closed_5m, lookback_bars=24, drop_pct=4.5, scan_bars=1, vol_mult=1.0, closed_1h=None):
    """Evaluate causal 2h dip + volume climax condition across closed 5m candles."""
    if not closed_5m:
        raise ValueError("5분봉 마감 데이터가 부족합니다.")
    lookback_bars = int(lookback_bars)
    vol_mult = float(vol_mult)
    by_open = {int(c["open_time_ms"]): c for c in closed_5m}

    def _window_vol_ratio(idx, ref_bar):
        if idx + 1 < lookback_bars:
            return None
        recent_vol = sum(float(c["volume"]) for c in closed_5m[idx - lookback_bars + 1:idx + 1])
        ref_open = int(ref_bar["open_time_ms"])
        base_vol = None
        if idx >= 4032 + lookback_bars:
            prior = closed_5m[idx - lookback_bars + 1 - 4032:idx - lookback_bars + 1]
            base_vol = (sum(float(c["volume"]) for c in prior) / 4032.0) * lookback_bars
        elif closed_1h:
            hr_avg = _compute_1h_baseline_vol(closed_1h, ref_open, days=14)
            if hr_avg and hr_avg > 0:
                base_vol = hr_avg * (lookback_bars / 12.0)
        else:
            prior_5m = closed_5m[:max(0, idx - lookback_bars + 1)]
            if len(prior_5m) >= 6:
                base_vol = (sum(float(c["volume"]) for c in prior_5m) / float(len(prior_5m))) * lookback_bars
        if base_vol and base_vol > 0:
            return recent_vol / base_vol
        return None

    latest = closed_5m[-1]
    latest_ref = by_open.get(int(latest["open_time_ms"]) - lookback_bars * BAR_5M_MS)
    latest_ret_pct = ((latest["close"] / latest_ref["close"]) - 1.0) * 100.0 if latest_ref else None
    latest_vol_ratio = _window_vol_ratio(len(closed_5m) - 1, latest_ref) if latest_ref else None

    threshold = -abs(float(drop_pct))
    start_idx = max(0, len(closed_5m) - max(1, int(scan_bars)))
    triggered_bar = None
    triggered_ref = None
    triggered_ret_pct = None
    triggered_vol_ratio = None
    for idx in range(len(closed_5m) - 1, start_idx - 1, -1):
        bar = closed_5m[idx]
        ref = by_open.get(int(bar["open_time_ms"]) - lookback_bars * BAR_5M_MS)
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
        "drop_pct": float(drop_pct),
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
    """Build Telegram notification for a 2h fixed-time short-dip exit settlement."""
    summary = summarize_dip_ledger(config, state)
    pnl_pct = float(record.get("pnl_pct", 0.0))
    icon = "🟢" if pnl_pct > 0 else "🔴"
    vr = record.get("vol_ratio")
    drop_actual = record.get("drop_pct_actual")
    trigger_note = ""
    if drop_actual is not None:
        trigger_note = f"{float(drop_actual):+.2f}%"
        if vr is not None:
            trigger_note += f" · 거래량 {float(vr):.2f}배"
    entry_live = float(record.get("entry_live_price") or record.get("entry_ref_price") or 0.0)
    entry_ref = float(record.get("entry_ref_price") or 0.0)
    exit_ref = float(record.get("exit_ref_price") or 0.0)
    live_gap_pct = ((entry_live / entry_ref) - 1.0) * 100.0 if entry_ref > 0 and entry_live > 0 else 0.0
    exit_utc = datetime.fromtimestamp(int(record["exit_open_ms"]) / 1000.0, tz=timezone.utc)
    exit_tor = exit_utc.astimezone(TORONTO_TZ)

    return f"""<b>🏁 [현금대기 단타 · {int(record.get('hold_bars', 24)) * 5 / 60:g}시간 청산 타임 도래!]</b>
━━━━━━━━━━━━━━━━━━━━
• <b>청산 결과:</b> {icon} <b>{pnl_pct:+.2f}%</b> (수수료·슬리피지 반영 후)
• <b>진입 신호 조건:</b> {trigger_note or '-'}
• <b>매수 기준 시가(QIS-7):</b> ${entry_ref:,.2f} (체결가 ${record['entry_price']:,.2f} · 당시 라이브 ${entry_live:,.2f} [{live_gap_pct:+.2f}%])
• <b>청산 기준 시가(QIS-7):</b> ${exit_ref:,.2f} (체결가 ${record['exit_price']:,.2f})
• <b>청산 시각:</b> {exit_utc.strftime('%H:%M UTC')} (토론토 {exit_tor.strftime('%H:%M %Z')})
━━━━━━━━━━━━━━━━━━━━
📊 <b>단타 모의 원장 누적:</b> {summary['closed_count']}전 {summary['wins']}승 {summary['losses']}패 (승률 {summary['win_rate_pct']:.1f}% · 추세승계 {summary['promoted_count']}회)
• <b>단타 누적 수익률:</b> <b>{summary['total_ret_pct']:+.2f}%</b> (모의잔고 ${summary['dip_cash']:,.2f})"""


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


def _settle_due_dip_trade(config, state, state_path, cur_ms, closed_5m, forming_5m, token, chat_id, allow_ticker=True):
    """Settle an active 2h short-dip paper trade once its canonical exit bar opens."""
    open_dip = state.get("open_dip")
    if not isinstance(open_dip, dict):
        return None
    exit_open_ms = int(open_dip.get("exit_open_ms", 0) or 0)
    if exit_open_ms <= 0 or cur_ms < exit_open_ms:
        return None

    all_bars = sorted(closed_5m + forming_5m, key=lambda c: int(c["open_time_ms"]))
    exit_bar = next((c for c in all_bars if int(c["open_time_ms"]) >= exit_open_ms and float(c.get("open", 0)) > 0), None)
    if exit_bar is not None:
        exit_ref_price = float(exit_bar["open"])
        actual_exit_ms = int(exit_bar["open_time_ms"])
    elif cur_ms < exit_open_ms + BAR_5M_MS:
        return None
    else:
        live_p = fetch_binance_ticker_price() if allow_ticker else None
        if live_p is None:
            live_p = float(forming_5m[-1]["close"] if forming_5m else closed_5m[-1]["close"])
        exit_ref_price = float(live_p)
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
    cash_only = config.get("dip_alert_cash_only", True)
    has_open_dip = isinstance(state.get("open_dip"), dict)
    has_unsent_exit = any(
        (t.get("status", "CLOSED_TIME") == "CLOSED_TIME" and not t.get("exit_alert_sent", True))
        for t in (state.get("dip_trades", []) or [])
    )

    if not enabled and not has_open_dip and not has_unsent_exit:
        return {"checked": False, "alert_sent": False, "skipped_reason": "disabled"}

    if enabled and cash_only and pos != "CASH" and not has_open_dip and not has_unsent_exit:
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

    if cash_only and pos != "CASH":
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

    raw_1h = candles_1h
    if raw_1h is None and candles_5m is None and vol_mult > 1.0:
        try:
            raw_1h = get_cached_binance_1h_candles(limit=360, now_ts=cur_ms / 1000.0)
        except Exception:
            raw_1h = None
    closed_1h, _ = partition_candles(raw_1h, now_ms=cur_ms) if raw_1h else ([], [])

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
    last_ts = float(state.get("last_dip_alert_ts", 0.0) or 0.0)
    last_candle_ms = int(state.get("last_dip_alert_candle_ms", 0) or 0)

    if trig_ms == last_candle_ms or (now_ts - last_ts <= cooldown_sec):
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

    live_price = fetch_binance_ticker_price() if candles_5m is None else None
    if live_price is None:
        live_price = forming_5m[-1]["close"] if forming_5m else trig_bar["close"]

    lb_hours = lookback_bars * 5 / 60.0
    hold_hours = hold_bars * 5 / 60.0
    entry_open_ms = trig_ms + BAR_5M_MS
    exit_open_ms = entry_open_ms + hold_bars * BAR_5M_MS
    all_5m_by_open = {int(c["open_time_ms"]): c for c in (closed_5m + forming_5m)}
    entry_bar = all_5m_by_open.get(entry_open_ms)
    entry_ref_price = float(entry_bar["open"]) if (entry_bar and float(entry_bar.get("open", 0)) > 0) else float(live_price)

    fee_rate = float(config.get("fee_rate", 0.0015))
    slip_rate = float(config.get("slippage_rate", 0.0005))
    entry_fill = entry_ref_price * (1.0 + slip_rate)
    dip_seed = float(state.get("dip_initial_seed") or config.get("initial_seed_usdt", 10000.0))
    dip_cash = float(state.get("dip_cash", dip_seed) if state.get("dip_cash") is not None else dip_seed)
    dip_qty = dip_cash / (entry_fill * (1.0 + fee_rate)) if (entry_fill > 0 and dip_cash > 0) else 0.0
    entry_fee = dip_qty * entry_fill * fee_rate

    entry_utc = datetime.fromtimestamp(entry_open_ms / 1000.0, tz=timezone.utc)
    exit_utc = datetime.fromtimestamp(exit_open_ms / 1000.0, tz=timezone.utc)
    entry_tor = entry_utc.astimezone(TORONTO_TZ)
    exit_tor = exit_utc.astimezone(TORONTO_TZ)
    lag_min = max(0, int((cur_ms - entry_open_ms) // 60000))
    lag_note = f" (신호 발생 약 {lag_min}분 경과)" if lag_min >= 5 else " (방금 확정)"
    vr = snap["triggered_vol_ratio"]
    vol_line = f"\n• <b>투매 거래량 배수:</b> <b>{vr:.2f}배</b> (14일 평균 대비 · 기준 {vol_mult:g}배)" if (vol_mult > 1.0 and vr is not None) else ""
    vol_tag = f" (거래량 {vol_mult:g}x)" if vol_mult > 1.0 else ""

    msg = f"""<b>⚡ [현금대기 전용 · BTC {lb_hours:g}시간 -{drop_pct:g}%{vol_tag} 급락 반등 경보!]</b>
━━━━━━━━━━━━━━━━━━━━
• <b>현재 모델 C 상태:</b> ⚪ 현금 대기 중 (CASH)
• <b>확정 5분봉 급락률:</b> <b>{snap['triggered_ret_pct']:+.2f}%</b>{lag_note}{vol_line}
• <b>기준가 ({lb_hours:g}h 전 종가):</b> ${trig_ref['close']:,.2f}
• <b>확정 종가:</b> ${trig_bar['close']:,.2f} (현재가: ${live_price:,.2f} · QIS-7 시가: ${entry_ref_price:,.2f})
━━━━━━━━━━━━━━━━━━━━
📌 <b>단타 전략 규격 (SL/TP 미사용 · 고정 시간청산)</b>
• <b>신호 봉 기준 시가:</b> {entry_utc.strftime('%H:%M UTC')} (토론토 {entry_tor.strftime('%H:%M %Z')})
• <b>예상 {hold_hours:g}시간 청산:</b> {exit_utc.strftime('%H:%M UTC')} (토론토 {exit_tor.strftime('%H:%M %Z')}) — 청산 시각 자동 알림
• <b>백테스트 참고 (QIS-7):</b> 단독 +592.4% (25~26년 +27.1% · 승률 63.1%), 폭락 중 좁은 손절 금지."""

    sent = send_telegram_message(token, chat_id, msg)
    if sent:
        state["last_dip_alert_ts"] = now_ts
        state["last_dip_alert_candle_ms"] = trig_ms
        if cur_ms < exit_open_ms:
            state["open_dip"] = {
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
            }
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
            raw_1h = get_cached_binance_1h_candles(limit=360, now_ts=cur_ms / 1000.0)
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
    live_price = (fetch_binance_ticker_price() if candles_5m is None else None) or (forming_5m[-1]["close"] if forming_5m else latest["close"])
    lb_hours = lookback_bars * 5 / 60.0
    hold_hours = hold_bars * 5 / 60.0
    vol_tag = f" (거래량 {vol_mult:g}x)" if vol_mult > 1.0 else ""

    open_dip = state.get("open_dip")
    open_block = ""
    if isinstance(open_dip, dict):
        exit_open_ms = int(open_dip.get("exit_open_ms", 0) or 0)
        rem_min = max(0, int((exit_open_ms - cur_ms) // 60000))
        entry_ref = float(open_dip.get("entry_ref_price") or open_dip.get("entry_fill") or 0.0)
        mtm_pct = ((live_price / entry_ref) - 1.0) * 100.0 if entry_ref > 0 else 0.0
        exit_utc = datetime.fromtimestamp(exit_open_ms / 1000.0, tz=timezone.utc)
        exit_tor = exit_utc.astimezone(TORONTO_TZ)
        watch_status = f"⏳ 2h 단타 모의 보유 중 (청산까지 약 {rem_min}분)"
        open_block = (
            f"\n• <b>진행 중 단타:</b> 진입 시가 ${entry_ref:,.2f} (현재 <b>{mtm_pct:+.2f}%</b> · "
            f"예정 청산 {exit_utc.strftime('%H:%M UTC')} / {exit_tor.strftime('%H:%M %Z')})"
        )
    elif not enabled:
        watch_status = "🔕 비활성화 (<code>/mode dip</code> 또는 <code>/mode all</code>로 켜기)"
    elif cash_only and pos != "CASH":
        watch_status = "⏸️ 일시정지 (현재 모델 C <b>BTC 보유 중</b> — 현금 전환 시 자동 가동)"
    else:
        remain_cd = max(0, int(cooldown_sec - (cur_ms / 1000.0 - float(state.get("last_dip_alert_ts", 0.0) or 0.0))))
        if remain_cd > 0:
            watch_status = f"⏳ 쿨다운 중 ({remain_cd // 60}분 후 재알림 가능)"
        elif snap["triggered"]:
            watch_status = "🚨 급락+거래량 조건 충족!"
        else:
            watch_status = f"🟢 현금대기 감시 중 ({lb_hours:g}h -{drop_pct:g}%{vol_tag} 대기)"

    ret_str = f"<b>{ret_pct:+.2f}%</b>" if ret_pct is not None else "데이터 부족 (결측 구간)"
    ref_str = f"${ref['close']:,.2f}" if ref else "결측"
    remain_drop = (ret_pct + drop_pct) if ret_pct is not None else None
    dist_str = f"{remain_drop:+.2f}%p" if remain_drop is not None else "N/A"
    vol_line = f"\n• <b>최근 {lb_hours:g}시간 거래량 배수:</b> <b>{vol_ratio:.2f}배</b> (14일 평균 대비 · 기준 {vol_mult:g}배)" if (vol_mult > 1.0 and vol_ratio is not None) else ""

    summary = summarize_dip_ledger(config, state)
    ledger_line = ""
    if summary["closed_count"] > 0 or summary["promoted_count"] > 0:
        ledger_line = (
            f"\n• <b>단타 모의원장:</b> {summary['closed_count']}회 청산 ({summary['wins']}승 {summary['losses']}패 · "
            f"승률 {summary['win_rate_pct']:.1f}%) · 추세승계 {summary['promoted_count']}회 · 누적 <b>{summary['total_ret_pct']:+.2f}%</b>"
        )

    return f"""<b>⚡ [현금대기 전용 · {lb_hours:g}시간 -{drop_pct:g}%{vol_tag} 급락 감시 현황]</b>
━━━━━━━━━━━━━━━━━━━━
• <b>현재 모델 C 포지션:</b> {'⚪ CASH (현금 대기)' if pos == 'CASH' else '🟢 BTC (추세 보유 중)'}
• <b>감시 상태:</b> {watch_status}{open_block}
• <b>비트코인 현재가:</b> ${live_price:,.2f} (직전 5m 종가: ${latest['close']:,.2f})
• <b>{lb_hours:g}시간 전 기준 종가:</b> {ref_str}
• <b>최근 {lb_hours:g}시간 등락률:</b> {ret_str} (트리거 -{drop_pct:g}%까지 {dist_str}){vol_line}
• <b>청산 규격:</b> 진입 후 {hold_hours:g}시간({hold_bars}봉) 고정 청산{ledger_line}
"""


def get_touch_status_text(config):
    candles = fetch_binance_daily_candles(limit=250)
    closed, forming = partition_daily_candles(candles)
    if len(closed) < 40:
        return "⚠️ 바이낸스 일봉 조회 실패 또는 데이터 부족"

    closes = [c["close"] for c in closed]
    cur_price = fetch_binance_ticker_price() or (forming[-1]["close"] if forming else closes[-1])
    active_len = int(config.get("bull_sma_len", config.get("strategy_sma", 40)))
    regime_len = int(config.get("regime_sma_len", 200))
    sma_active = sum(closes[-active_len:]) / float(active_len)
    sma_regime = sum(closes[-regime_len:]) / float(regime_len) if len(closes) >= regime_len else None

    threshold = float(config.get("touch_threshold_pct", 0.5))
    gap_a = cur_price - sma_active
    diff_a = abs(gap_a) / sma_active * 100.0

    regime_block = "• <b>일봉 200일선:</b> 데이터 준비 중"
    if sma_regime is not None:
        gap_r = cur_price - sma_regime
        diff_r = abs(gap_r) / sma_regime * 100.0
        regime_block = (
            f"• <b>일봉 {regime_len}일선 (체제선):</b> ${sma_regime:,.2f}\n"
            f"  - 남은 거리: ${abs(gap_r):,.2f} (괴리율 <b>{gap_r / sma_regime * 100:+.2f}%</b>)\n"
            f"  - 감시 상태: {'🚨 터치권 진입!' if diff_r <= threshold else '대기 중'}"
        )

    return f"""<b>🎯 [BTC 주요 이평선 터치 감시 현황]</b>
━━━━━━━━━━━━━━━━━━━━
• <b>비트코인 현재가:</b> ${cur_price:,.2f}

• <b>일봉 {active_len}일선 (모델 C 매매선):</b> ${sma_active:,.2f}
  - 남은 거리: ${abs(gap_a):,.2f} (괴리율 <b>{gap_a / sma_active * 100:+.2f}%</b>)
  - 감시 상태: {'🚨 터치권 진입!' if diff_a <= threshold else '대기 중'}

{regime_block}
━━━━━━━━━━━━━━━━━━━━
• <b>터치 감도:</b> 오차 ±{threshold}% 이내 도달 시 즉시 알림
• <b>전략:</b> {_strategy_display_name(config)}
"""


def get_current_status_text(config, state):
    candles = fetch_binance_daily_candles(limit=250)
    closed, forming = partition_daily_candles(candles)
    if not closed:
        return "⚠️ 바이낸스 시세 조회 실패"

    prev_pos = state.get("position", "CASH")
    try:
        snap = compute_live_signal_snapshot(closed, config, prev_pos=prev_pos)
    except ValueError as e:
        return f"⚠️ {e}"

    current_price = fetch_binance_ticker_price() or (forming[-1]["close"] if forming else snap["last_close"])
    sma = snap["active_sma"]
    sma_len = snap["active_sma_len"]
    diff_pct = (current_price / sma - 1.0) * 100.0
    reg_str = f"${snap['regime_sma']:,.2f}" if snap["regime_sma"] is not None else "준비 중"

    pos = prev_pos
    initial_seed = float(state.get("initial_seed", 10000.0))
    cur_equity = float(state["cash"]) if pos == "CASH" else float(state["btc_qty"]) * current_price
    total_ret = (cur_equity / initial_seed - 1.0) * 100.0 if initial_seed > 0 else 0.0
    vol_tag = f"🟢 {snap['vol_ratio']:.2f}배 ({snap['vol_mult']}배 충족)" if snap["vol_ok"] else f"⚪ {snap['vol_ratio']:.2f}배 ({snap['vol_mult']}배 미달)"

    dip_lb_h = int(config.get("dip_alert_lookback_bars", 24)) * 5 / 60.0
    dip_drop = float(config.get("dip_alert_drop_pct", 4.5))
    dip_vm = float(config.get("dip_alert_vol_mult", 2.5))
    dip_spec = f"{dip_lb_h:g}h -{dip_drop:g}% · {dip_vm:g}x" if dip_vm > 1.0 else f"{dip_lb_h:g}h -{dip_drop:g}%"
    open_dip = state.get("open_dip")
    if not config.get("dip_alert_enabled", True):
        dip_status_str = "🔕 꺼짐"
    elif isinstance(open_dip, dict):
        exit_utc = datetime.fromtimestamp(int(open_dip.get("exit_open_ms", 0) or 0) / 1000.0, tz=timezone.utc)
        dip_status_str = f"⏳ 단타 보유 중 (청산 {exit_utc.strftime('%H:%M UTC')})"
    elif config.get("dip_alert_cash_only", True) and pos != "CASH":
        dip_status_str = "⏸️ 정지 (BTC 보유 중)"
    else:
        dip_status_str = f"🟢 가동 중 ({dip_spec})"

    dip_summary = summarize_dip_ledger(config, state)
    dip_ledger_str = ""
    if dip_summary["closed_count"] > 0 or dip_summary["promoted_count"] > 0:
        dip_ledger_str = (
            f"\n• <b>단타 모의원장:</b> {dip_summary['closed_count']}회 청산 ({dip_summary['wins']}승 "
            f"{dip_summary['losses']}패 · 승률 {dip_summary['win_rate_pct']:.1f}%) · 승계 {dip_summary['promoted_count']}회 · "
            f"누적 <b>{dip_summary['total_ret_pct']:+.2f}%</b>"
        )

    return f"""<b>📊 [BTC Quant Lab 실시간 모의투자 현황]</b>
━━━━━━━━━━━━━━━━━━━━
• <b>전략:</b> {_strategy_display_name(config, snap)}
• <b>장세 체제:</b> {snap['regime_label']} (200일선: {reg_str})
• <b>비트코인 현재가:</b> ${current_price:,.2f} (직전 마감: ${snap['last_close']:,.2f})
• <b>일봉 {sma_len}일선:</b> ${sma:,.2f} (괴리율: <b>{diff_pct:+.2f}%</b> {'🟢 상회' if diff_pct > 0 else '🔴 하회'})
• <b>직전 거래량:</b> {snap['last_vol']:,.1f} BTC ({snap['vol_ma_len']}일 평균: {snap['prior_vol_avg']:,.1f} BTC)
• <b>거래량 배수:</b> <b>{vol_tag}</b>
━━━━━━━━━━━━━━━━━━━━
• <b>보유 포지션:</b> {'🟢 비트코인 100% 보유' if pos == 'BTC' else '⚪ 100% USDT 현금 (휩소 방어 중)'}
• <b>현금대기 급락알림:</b> {dip_status_str}
• <b>가상 평가잔고:</b> ${cur_equity:,.2f} USDT
• <b>누적 수익률:</b> <b>{total_ret:+.2f}%</b>
• <b>총 체결 건수:</b> {len(state.get('trades', []))}회{dip_ledger_str}
"""


if __name__ == "__main__":
    cfg = load_config()
    st = load_state(cfg)
    print("Testing Binance Data & Model C Signal Snapshot...")
    st, info = evaluate_signal_and_step(cfg, st, manual_check=True)
    print("\n" + "=" * 50)
    print(get_current_status_text(cfg, st))
    print("=" * 50)
    print("Evaluation info:", info)
