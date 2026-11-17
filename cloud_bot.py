"""
BTC Quant Lab - Cloud Bot for GitHub Actions (Model C: 40/40 Hybrid + 200 SMA Regime Switch)
Runs every 30 minutes on Microsoft GitHub cloud servers (Python 3.9+ / 3.11 compatible).
Timezone: America/Toronto (EST/EDT)
Shares canonical Model C signal & accounting engine with paper_trader.py.
"""
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from paper_trader import (
    CONFIG_PATH,
    STATE_PATH,
    _strategy_display_name,
    check_dip_alert,
    check_sma_touch_alert,
    evaluate_signal_and_step,
    get_dip_status_text,
    load_config,
    load_state,
    save_state,
    send_telegram_message,
)
from telegram_bot import format_toronto_morning_preview

TORONTO_TZ = ZoneInfo("America/Toronto")


def load_cloud_config():
    """Load config file and override bot credentials from GitHub Secrets environment variables."""
    config = load_config(CONFIG_PATH)
    env_token = os.environ.get("BOT_TOKEN", "").strip()
    env_chat = os.environ.get("CHAT_ID", "").strip()
    if env_token:
        config["bot_token"] = env_token
    if env_chat:
        config["chat_id"] = str(env_chat)
    return config


def run_cloud_monitor(state_path=STATE_PATH):
    now_toronto = datetime.now(TORONTO_TZ)
    config = load_cloud_config()
    state = load_state(config, state_path=state_path)

    print(f"[{now_toronto.strftime('%Y-%m-%d %H:%M:%S %Z')}] Active Strategy: {_strategy_display_name(config)}")
    if not str(config.get("bot_token", "")).strip() or not str(config.get("chat_id", "")).strip():
        print("⚠️ WARNING: BOT_TOKEN or CHAT_ID is empty! Set them in GitHub Settings -> Secrets and variables -> Actions.")

    # 1. Real-time 40d/200d SMA Touch Alert (±0.5% threshold, 2h cooldown)
    touch_info = check_sma_touch_alert(config, state, state_path=state_path)
    if touch_info:
        print(
            f"Price: ${touch_info['cur_price']:,.2f} | "
            f"Active SMA: ${touch_info['sma_active']:,.2f} ({touch_info['diff_active']:.2f}%)"
        )

    # 1b. Idle-Cash Dedicated 2h -4.5% (2.5x vol) Dip Alert & 2h Exit Settlement (scan last 12 closed 5m bars = 1h buffer for cron jitter)
    dip_info = check_dip_alert(config, state, state_path=state_path, scan_bars=12)
    if isinstance(dip_info, dict) and dip_info.get("checked"):
        ret_disp = f"{dip_info['latest_ret_pct']:+.2f}%" if dip_info.get("latest_ret_pct") is not None else f"{dip_info.get('triggered_ret_pct', 0):+.2f}%"
        vr = dip_info.get("latest_vol_ratio") if dip_info.get("latest_vol_ratio") is not None else dip_info.get("triggered_vol_ratio")
        vol_disp = f"{vr:.2f}x" if vr is not None else "N/A"
        print(
            f"Cash-Only 2h Dip Monitor: ret_2h={ret_disp} | vol={vol_disp} | "
            f"alert_sent={dip_info.get('alert_sent')}{' | exit_settled=True' if dip_info.get('exit_settled') else ''}"
        )
    elif isinstance(dip_info, dict) and dip_info.get("skipped_reason"):
        suffix = " | exit_settled=True" if dip_info.get("exit_settled") else ""
        print(f"Cash-Only 2h Dip Monitor skipped: {dip_info['skipped_reason']}{suffix}")

    # 1c. Optional one-shot 5m close status notification (via workflow_dispatch input or state flag)
    want_status_once = (
        os.environ.get("NOTIFY_STATUS", "").strip().lower() in ("1", "true", "yes")
        or bool(state.get("notify_next_5m_close"))
    )
    if want_status_once:
        status_msg = "<b>☁️ [GitHub 클라우드 봇 · 5분봉 마감 점검 리포트]</b>\n\n" + get_dip_status_text(config, state)
        if send_telegram_message(config.get("bot_token"), config.get("chat_id"), status_msg):
            print("Sent 5m candle close status report to Telegram!")
            if state.get("notify_next_5m_close"):
                state["notify_next_5m_close"] = False
                save_state(state, state_path=state_path)

    # 2. Closed UTC Daily Candle Strategy Evaluation & Execution (once per closed day)
    state, eval_info = evaluate_signal_and_step(config, state, manual_check=False, state_path=state_path)
    if isinstance(eval_info, dict):
        print(
            f"Evaluated: new_close={eval_info.get('is_new_daily_close')} | "
            f"pos={eval_info.get('position')} | traded={eval_info.get('trade_executed')}"
        )

    # 3. Toronto Morning Briefing (Every day at or after 09:00 AM Toronto Time)
    today_toronto_str = now_toronto.strftime("%Y-%m-%d")
    if now_toronto.hour >= 9 and state.get("last_morning_report_date") != today_toronto_str:
        if config.get("enable_daily_report", True):
            briefing_msg = format_toronto_morning_preview(config, state)
            if briefing_msg.startswith("⚠️"):
                print(f"Skipped Toronto Morning Briefing due to data fetch error: {briefing_msg}")
                return
            sent = send_telegram_message(config.get("bot_token"), config.get("chat_id"), briefing_msg)
            if sent:
                print(f"Sent Toronto Morning Briefing for {today_toronto_str}!")
                state["last_morning_report_date"] = today_toronto_str
                save_state(state, state_path=state_path)
            else:
                print(f"Failed to send Toronto Morning Briefing for {today_toronto_str}; will retry next run.")
        else:
            state["last_morning_report_date"] = today_toronto_str
            save_state(state, state_path=state_path)


def main():
    print("=" * 60)
    print("🚀 Starting Cloud Bot (Model C 40/40 Hybrid - Toronto Timezone)...")
    print("=" * 60)
    run_cloud_monitor()
    print("Cloud monitor run finished successfully.")


if __name__ == "__main__":
    main()
