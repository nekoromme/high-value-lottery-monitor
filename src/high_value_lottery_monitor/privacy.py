"""公開される診断ファイルへ、Discordの投稿用URLを残さないための共通処理。"""

from __future__ import annotations

import os
import re
from urllib.parse import quote, urlsplit


# 通信エラーにはURL全体ではなく「/api/webhooks/番号/鍵」だけが
# 含まれることもある。環境変数にないURLも、この形なら伏せる。
WEBHOOK_PATTERN = re.compile(
    r"(?:https?://(?:[a-z0-9-]+\.)?discord(?:app)?\.com)?"
    r"/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9_.~-]+"
    r"(?:\?[^\s\"'<>\\]*)?",
    re.IGNORECASE,
)
REDACTED = "[REDACTED]"


def redact_text(value: str) -> str:
    """URL全体・鍵だけ・URLエンコードされた表記を伏せる。"""

    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if webhook:
        sensitive = {webhook}
        try:
            parts = urlsplit(webhook)
            sensitive.add(parts.path)
            if "/webhooks/" in parts.path:
                sensitive.add(parts.path.rsplit("/", 1)[-1])
        except ValueError:
            # 設定が不正でも、渡された値そのものは必ず伏せる。
            pass
        for raw in sorted(sensitive, key=len, reverse=True):
            if len(raw) < 8:
                continue
            for encoded in {raw, quote(raw, safe=""), quote(raw, safe="/")}:
                value = value.replace(encoded, REDACTED)
    return WEBHOOK_PATTERN.sub(REDACTED, value)


def redact_diagnostics(value):
    """入れ子の診断値もコピーして伏せ、実行中の状態や送信先は変更しない。"""

    if isinstance(value, str):
        return redact_text(value)
    if isinstance(value, dict):
        return {
            redact_text(key) if isinstance(key, str) else key: redact_diagnostics(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [redact_diagnostics(item) for item in value]
    return value
