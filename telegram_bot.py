"""
Telegram Bot Daemon for BTC Quant Lab Paper Trading (Model C: 40/40 Hybrid)
Runs in background, handles commands, reliably triggers daily close evaluation after UTC 00:00:10
with catch-up recovery, and checks real-time 40d/200d SMA touch alerts every 30 seconds.
"""
import json
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from paper_trader import (
    CONFIG_PATH,
    _get_ssl_context,
    _strategy_display_name,
    check_dip_alert,
    check_sma_touch_alert,
    compute_live_signal_snapshot,
    describe_position_action,
    evaluate_signal_and_step,
    fetch_binance_24h_ticker,
    fetch_binance_daily_candles,
    fetch_binance_ticker_price,
    get_current_status_text,
    get_dip_status_text,
    get_touch_status_text,
    load_config,
    load_state,
    partition_daily_candles,
    save_state,
    send_telegram_message,
    summarize_dip_ledger,
)

BASE_DIR = Path(__file__).resolve().parent
TORONTO_TZ = ZoneInfo("America/Toronto")


def get_telegram_updates(token, offset=None):
    if not token:
        return []
    url = f"https://api.telegram.org/bot{token}/getUpdates?timeout=10"
    if offset is not None:
        url += f"&offset={offset}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    ctx = _get_ssl_context()
    try:
        with urllib.request.urlopen(req, timeout=15, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            if data.get("ok"):
                return data.get("result", [])
    except Exception:
        pass
    return []


def should_run_daily_evaluation(now_utc, last_evaluated_candle_ms, buffer_sec=10):
    """Return True if yesterday's UTC daily candle has closed (+buffer) and hasn't been evaluated yet."""
    if now_utc.tzinfo is None:
        now_utc = now_utc.replace(tzinfo=timezone.utc)
    day_start = datetime(now_utc.year, now_utc.month, now_utc.day, tzinfo=timezone.utc)
    elapsed_sec = (now_utc - day_start).total_seconds()
    expected_closed_ms = int(day_start.timestamp() * 1000) - 86_400_000
    return elapsed_sec >= buffer_sec and int(last_evaluated_candle_ms or 0) < expected_closed_ms


def format_trades_report_text(config, state):
    """Render both Model C daily trades and 2h short-dip mock ledger for /trades."""
    trades = state.get("trades", []) or []
    dip_trades = state.get("dip_trades", []) or []
    open_dip = state.get("open_dip")
    sections = []

    if not trades:
        sections.append("<b>📜 [모델 C 일봉 추세 내역]</b>\n• 아직 체결된 모의 매매 내역이 없습니다.")
    else:
        lines = ["<b>📜 [모델 C 일봉 추세 내역 (최신순)]</b>"]
        for t in reversed(trades[-5:]):
            pnl_str = f" | PnL {t['pnl_pct']:+.2f}%" if "pnl_pct" in t else ""
            lines.append(f"• {t['time']}: <b>{t['action']}</b> @ ${t['price']:,.2f}{pnl_str} ({t.get('reason', '')})")
        sections.append("\n".join(lines))

    summary = summarize_dip_ledger(config, state)
    dip_lines = ["<b>⚡ [2시간 단타 모의 원장]</b>"]
    if isinstance(open_dip, dict):
        entry_ref = float(open_dip.get("entry_ref_price") or open_dip.get("entry_fill") or 0.0)
        exit_utc = datetime.fromtimestamp(int(open_dip.get("exit_open_ms", 0) or 0) / 1000.0, tz=timezone.utc)
        dip_lines.append(f"• ⏳ <b>진행 중:</b> 진입 기준가 ${entry_ref:,.2f} (예정 청산 {exit_utc.strftime('%H:%M UTC')})")
    if not dip_trades and not isinstance(open_dip, dict):
        dip_lines.append("• 아직 체결된 2시간 단타 기록이 없습니다.")
    else:
        dip_lines.append(
            f"• 누적: {summary['closed_count']}회 청산 ({summary['wins']}승 {summary['losses']}패 · "
            f"승률 {summary['win_rate_pct']:.1f}%) · 승계 {summary['promoted_count']}회 · "
            f"수익률 <b>{summary['total_ret_pct']:+.2f}%</b>"
        )
        for d in reversed(dip_trades[-5:]):
            tag = "🔁 승계" if d.get("status") == "PROMOTED_TO_TREND" else ("🟢 익절" if float(d.get("pnl", 0.0)) > 0 else "🔴 손절/청산")
            dip_lines.append(
                f"• {d.get('exit_time', d.get('entry_time', ''))}: {tag} "
                f"<b>{float(d.get('pnl_pct', 0.0)):+.2f}%</b> "
                f"(${float(d.get('entry_ref_price', d.get('entry_price', 0.0))):,.0f} → ${float(d.get('exit_ref_price', d.get('exit_price', 0.0))):,.0f})"
            )
    sections.append("\n".join(dip_lines))
    return "\n\n".join(sections)


def format_toronto_morning_preview(config, state):
    now_toronto = datetime.now(TORONTO_TZ)
    candles = fetch_binance_daily_candles(limit=250)
    closed, forming = partition_daily_candles(candles)
    if not closed:
        return "⚠️ 바이낸스 시세 조회 실패"

    prev_pos = state.get("position", "CASH")
    try:
        snap = compute_live_signal_snapshot(closed, config, prev_pos=prev_pos)
    except ValueError as e:
        return f"⚠️ {e}"

    ticker = fetch_binance_24h_ticker()
    cur_p = (ticker["price"] if ticker and ticker["price"] > 0 else None) or fetch_binance_ticker_price() or (forming[-1]["close"] if forming else snap["last_close"])
    chg_24h = ticker["change_24h_pct"] if ticker else 0.0

    sma = snap["active_sma"]
    sma_len = snap["active_sma_len"]
    diff_active = (cur_p / sma - 1.0) * 100.0
    badge, action_guide, cur_equity, total_ret, vol_mark = describe_position_action(state, snap, cur_p)

    return f"""<b>{badge} 아침 브리핑 ({now_toronto.strftime('%m/%d %H:%M %Z')})</b>
• <b>행동:</b> {action_guide}
• <b>현재가:</b> ${cur_p:,.0f} (24h <b>{chg_24h:+.2f}%</b> · {sma_len}일선 <b>{diff_active:+.2f}%</b>)
• <b>거래량/장세:</b> {vol_mark} {snap['vol_ratio']:.2f}배(기준 {snap['vol_mult']}배) · {snap['regime_label']}
• <b>잔고:</b> ${cur_equity:,.0f} (<b>{total_ret:+.2f}%</b>)"""


def main():
    print("=" * 60)
    print("🚀 Starting BTC Quant Lab Telegram Bot Daemon (Model C 40/40 Hybrid)...")
    config = load_config()
    token = str(config.get("bot_token", "")).strip()
    chat_id = str(config.get("chat_id", "")).strip()

    if not token or not chat_id:
        print("\n⚠️ 봇 토큰 또는 Chat ID가 설정되지 않았습니다.")
        return

    print(f"✅ Target Chat ID: {chat_id}")
    print(f"✅ Active Strategy: {_strategy_display_name(config)}")

    offset = None
    last_touch_check_ts = 0.0
    last_daily_attempt_ts = 0.0

    while True:
        try:
            config = load_config()
            state = load_state(config)
            now_ts = time.time()
            now_utc = datetime.now(timezone.utc)

            # 1. Real-time SMA Touch & Idle-Cash 2h Dip Check (Every 30 seconds; scan last 6 closed 5m bars = 30m sleep catch-up)
            if now_ts - last_touch_check_ts >= 30:
                last_touch_check_ts = now_ts
                check_sma_touch_alert(config, state)
                check_dip_alert(config, state, scan_bars=6)

            # 2. Catch-up safe daily evaluation after UTC 00:00:10 (retried at most every 60s if API fails)
            if should_run_daily_evaluation(now_utc, state.get("last_evaluated_candle_ms", 0)):
                if now_ts - last_daily_attempt_ts >= 60:
                    last_daily_attempt_ts = now_ts
                    print(f"[{now_utc.isoformat()}] Closed UTC daily candle detected. Evaluating Model C signal...")
                    state, info = evaluate_signal_and_step(config, state, manual_check=False)

            # 3. Poll for incoming user commands
            updates = get_telegram_updates(token, offset)
            for upd in updates:
                offset = upd["update_id"] + 1
                msg = upd.get("message")
                if not msg:
                    continue
                sender_chat_id = str(msg.get("chat", {}).get("id"))
                text = msg.get("text", "").strip()

                if chat_id and sender_chat_id != chat_id:
                    print(f"Ignored message from unauthorized chat_id: {sender_chat_id}")
                    continue

                print(f"Received command: '{text}' from {sender_chat_id}")

                if text in ["/start", "/help", "도움말"]:
                    reply = (
                        f"<b>👋 안녕하세요! BTC Quant Lab 봇입니다. (토론토 EST/EDT 기준)</b>\n\n"
                        f"<b>🔥 활성 전략: {_strategy_display_name(config)}</b>\n\n"
                        "<b>📌 사용 가능한 명령어:</b>\n"
                        "• <code>/status</code> 또는 <code>/상태</code>: 실시간 시세, 40일/200일선, 거래량 배수, 모의잔고 조회\n"
                        "• <code>/touch</code> 또는 <code>/터치</code>: 40일선 / 200일선까지 남은 거리 및 감시 현황\n"
                        "• <code>/dip</code> 또는 <code>/급락</code>: ⚡ 현금대기 전용 2시간 -4.5%(거래량 2.5x) 급락 감시 현황 조회\n"
                        "• <code>/briefing</code> 또는 <code>/아침</code>: 🌅 토론토 모닝 브리핑 즉시 미리보기\n"
                        "• <code>/mode dip</code>: [현금대기 급락 알림 전용] (브리핑/터치 끄고 2h -4.5%·거래량2.5x 급락만 감시)\n"
                        "• <code>/mode touch</code>: [닿을 때만 알림 모드] (아침 브리핑 OFF · 터치+급락 ON)\n"
                        "• <code>/mode all</code>: [전체 알림 모드] (브리핑 + 터치 알림 + 현금대기 급락 알림)\n"
                        "• <code>/check</code>: 바이낸스 일봉 즉시 재계산 및 신호 확인\n"
                        "• <code>/trades</code>: 최근 체결 내역 조회\n"
                        "• <code>/help</code>: 도움말"
                    )
                    send_telegram_message(token, sender_chat_id, reply)

                elif text in ["/briefing", "/아침", "브리핑", "아침"]:
                    reply = format_toronto_morning_preview(config, state)
                    send_telegram_message(token, sender_chat_id, reply)

                elif text in ["/status", "/상태", "상태"]:
                    reply = get_current_status_text(config, state)
                    send_telegram_message(token, sender_chat_id, reply)

                elif text in ["/touch", "/터치", "터치"]:
                    reply = get_touch_status_text(config)
                    send_telegram_message(token, sender_chat_id, reply)

                elif text in ["/dip", "/급락", "급락"]:
                    reply = get_dip_status_text(config, state)
                    send_telegram_message(token, sender_chat_id, reply)

                elif text.startswith("/mode") or text.startswith("/모드"):
                    parts = text.split()
                    sub = parts[1].lower() if len(parts) > 1 else ""
                    if sub in ["dip", "급락"]:
                        config["enable_daily_report"] = False
                        config["touch_alert_enabled"] = False
                        config["dip_alert_enabled"] = True
                        config["dip_alert_cash_only"] = True
                        save_state(config, state_path=CONFIG_PATH)
                        reply = "<b>⚡ [설정 완료] 현금대기 중 급락 알림 전용 모드</b>\n\n모델 C 일일 브리핑·이평선 터치 알림은 끄고, <b>현금(CASH) 대기 상태일 때 2시간 -4.5%(거래량 2.5배) 급락 반등 조건이 뜨면 즉시 알림</b>을 보냅니다!"
                    elif sub in ["touch", "터치"]:
                        config["enable_daily_report"] = False
                        config["touch_alert_enabled"] = True
                        config["dip_alert_enabled"] = True
                        config["dip_alert_cash_only"] = True
                        save_state(config, state_path=CONFIG_PATH)
                        reply = "<b>🔕 [설정 완료] 닿을 때만 알림 모드</b>\n\n정기 브리핑은 끄고, <b>40/200일선 터치 및 현금대기 2시간 -4.5%(거래량 2.5배) 급락 시에만</b> 즉시 알림을 발송합니다!"
                    elif sub in ["all", "전체"]:
                        config["enable_daily_report"] = True
                        config["touch_alert_enabled"] = True
                        config["dip_alert_enabled"] = True
                        config["dip_alert_cash_only"] = True
                        save_state(config, state_path=CONFIG_PATH)
                        reply = "<b>🔔 [설정 완료] 전체 알림 모드</b>\n\n일봉 마감 브리핑, <b>40/200일선 실시간 터치</b>, 그리고 <b>현금대기 2시간 -4.5%(거래량 2.5배) 급락 알림</b>을 모두 받습니다."
                    else:
                        if not config.get("enable_daily_report", True) and not config.get("touch_alert_enabled", True) and config.get("dip_alert_enabled", True):
                            mode_cur = "현금대기 급락 알림 전용"
                        elif not config.get("enable_daily_report", True):
                            mode_cur = "닿을 때만 알림 (+현금대기 급락)"
                        else:
                            mode_cur = "전체 알림(브리핑 포함)"
                        reply = (
                            f"현재 모드: <b>{mode_cur}</b>\n"
                            "• <code>/mode dip</code>: 현금대기 2시간 -4.5%(거래량 2.5x) 급락 알림 전용\n"
                            "• <code>/mode touch</code>: 이평선 터치 + 급락 알림\n"
                            "• <code>/mode all</code>: 전체 알림(브리핑 포함)"
                        )
                    send_telegram_message(token, sender_chat_id, reply)

                elif text in ["/check", "/확인", "확인"]:
                    state, _ = evaluate_signal_and_step(config, state, manual_check=True)
                    reply = "<b>✅ 신호 계산 완료:</b>\n" + get_current_status_text(config, state)
                    send_telegram_message(token, sender_chat_id, reply)

                elif text in ["/trades", "/내역"]:
                    reply = format_trades_report_text(config, state)
                    send_telegram_message(token, sender_chat_id, reply)

            time.sleep(2)
        except KeyboardInterrupt:
            print("\nBot stopped by user.")
            break
        except Exception as e:
            print(f"Error in bot loop: {e}")
            time.sleep(5)


if __name__ == "__main__":
    main()
