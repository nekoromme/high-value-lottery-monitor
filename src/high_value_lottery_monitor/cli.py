"""コマンドライン入口。GitHub Actionsもローカル試験も同じ入口を使う。"""

from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from high_value_lottery_monitor.monitor import JsonlAuditLog, run_monitor
from high_value_lottery_monitor.providers.ricoh import RicohOnlineStoreProvider
from high_value_lottery_monitor.services.calendar import GoogleCalendarWriter
from high_value_lottery_monitor.services.discord import DiscordNotifier
from high_value_lottery_monitor.state import load_state

JST = ZoneInfo("Asia/Tokyo")


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="高額抽選販売モニター")
    parser.add_argument(
        "--mode",
        choices=("auto", "baseline", "run", "dry-run"),
        default="auto",
        help="初回はauto、通知なし確認はdry-run",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=Path(os.getenv("STATE_FILE", "state/state.json")),
    )
    parser.add_argument(
        "--log-dir",
        type=Path,
        default=Path(os.getenv("LOG_DIR", "logs")),
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    now = datetime.now(JST)
    log_path = args.log_dir / f"run-{now.strftime('%Y%m%d-%H%M%S')}.jsonl"
    audit = JsonlAuditLog(log_path)
    state = load_state(args.state_file)

    provider = RicohOnlineStoreProvider(
        user_agent_contact=os.getenv("MONITOR_USER_AGENT_CONTACT")
    )
    notifier = DiscordNotifier(os.getenv("DISCORD_WEBHOOK_URL"))
    calendar = GoogleCalendarWriter(
        os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON"),
        os.getenv("GOOGLE_CALENDAR_ID"),
    )

    summary = run_monitor(
        mode=args.mode,
        state=state,
        state_path=args.state_file,
        provider=provider,
        notifier=notifier,
        calendar=calendar,
        audit=audit,
        now=now,
    )
    print(json.dumps(summary.as_dict(), ensure_ascii=False, indent=2))

    # 解析自体は成功していても通知・Calendar失敗があればActionsを赤くする。
    # 状態には失敗が保存され、次回同じ通知を再試行できる。
    return 1 if summary.errors else 0


if __name__ == "__main__":
    raise SystemExit(main())

