"""実物の鍵・外部通信を使わず、公開成果物への漏えいと再通知を検証する。"""

import json
import sys
import traceback
from pathlib import Path
from urllib.parse import quote

import pytest
import requests

from high_value_lottery_monitor import cli
from high_value_lottery_monitor.monitor import JsonlAuditLog, RunSummary
from high_value_lottery_monitor.privacy import redact_diagnostics
from high_value_lottery_monitor.services.discord import DiscordDeliveryError, DiscordNotifier
from high_value_lottery_monitor.state import EMPTY_STATE
from test_monitor import FakeCalendar, FakeProvider, make_case


# テスト専用。実際に送れるWebhookをコードやログへ置かない。
TOKEN = "audit_dummy_" + "x" * 40
WEBHOOK = "https://discord.com/api/webhooks/" + "0" * 18 + "/" + TOKEN


def response(status: int, content: bytes = b'{"id":"test-message"}'):
    result = requests.Response()
    result.status_code = status
    result.reason = "audit response"
    result.url = WEBHOOK + "?wait=true"
    result._content = content
    return result


@pytest.mark.parametrize("failure", ["http429", "http500", "connection", "timeout", "json"])
def test_delivery_errors_never_expose_url_even_in_traceback(monkeypatch, failure):
    def fake_post(*args, **kwargs):
        if failure == "connection":
            # requestsはホスト名とパスを分けて例外へ含めることがある。
            raise requests.ConnectionError("failed at " + WEBHOOK.split("discord.com")[1])
        if failure == "timeout":
            raise requests.ReadTimeout("timeout at " + WEBHOOK)
        if failure == "json":
            return response(200, WEBHOOK.encode())
        return response(int(failure[4:]))

    monkeypatch.setattr(requests, "post", fake_post)
    with pytest.raises(DiscordDeliveryError) as caught:
        DiscordNotifier(WEBHOOK)._post({"content": "offline test"})
    formatted = "".join(traceback.format_exception(caught.value))
    assert TOKEN not in formatted
    assert "api/webhooks" not in formatted
    assert "Discord通知に失敗" in formatted
    if failure.startswith("http"):
        assert "HTTP " + failure[4:] in formatted


def test_saved_diagnostics_and_summary_have_independent_redaction(tmp_path, monkeypatch):
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    # 通知処理以外が生の例外を渡しても、ファイル保存の直前に伏せる。
    raw = {"nested": [{"error": WEBHOOK + "?wait=true", "encoded": quote(WEBHOOK, safe="")}],
           "token_only": TOKEN, "count": 3, "ok": False}
    path = tmp_path / "audit.jsonl"
    JsonlAuditLog(path).write("discord_error", details=raw)
    summary = RunSummary(mode="run", errors=["Discord: " + WEBHOOK])
    for serialized in [path.read_text(), json.dumps(summary.as_dict())]:
        assert TOKEN not in serialized
        assert "api/webhooks" not in serialized
        assert "[REDACTED]" in serialized
    # ログの伏せ字が、実際の送信先や実行状態を書き換えてはいけない。
    assert raw["token_only"] == TOKEN
    saved = json.loads(path.read_text())["details"]
    assert saved["count"] == 3 and saved["ok"] is False


def test_unconfigured_webhook_and_path_are_redacted(monkeypatch):
    monkeypatch.delenv("DISCORD_WEBHOOK_URL", raising=False)
    for value in [WEBHOOK, WEBHOOK.split("discord.com")[1],
                  WEBHOOK.replace("discord.com", "canary.discordapp.com")]:
        assert TOKEN not in redact_diagnostics({"error": value})["error"]
    assert redact_diagnostics("HTTP 500 / https://example.test/item") == "HTTP 500 / https://example.test/item"


def test_cli_artifacts_failure_retry_and_deduplication(tmp_path, monkeypatch, capsys):
    """Actionsと同じ入口で、stdout・診断・履歴・次回再試行を合わせて確認。"""
    state_path = tmp_path / "state.json"
    state_path.write_text(json.dumps({**EMPTY_STATE, "armed": True}))
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", WEBHOOK)
    monkeypatch.setattr(sys, "argv", ["lottery-monitor", "--mode", "run", "--state-file",
                                      str(state_path), "--log-dir", str(tmp_path / "logs")])
    case = make_case(form_url="https://forms.gle/offline-test")
    # 時刻は受付期間内に固定する。テスト実施日が変わっても実通知しない。
    import high_value_lottery_monitor.monitor as monitor
    from test_monitor import NOW
    original_run = monitor.run_monitor
    monkeypatch.setattr(cli, "run_monitor", lambda **kwargs: original_run(**{**kwargs, "now": NOW}))
    monkeypatch.setattr(cli, "RicohOnlineStoreProvider", lambda **kwargs: FakeProvider(case))
    calendar = FakeCalendar()
    monkeypatch.setattr(cli, "GoogleCalendarWriter", lambda *args: calendar)
    calls = []

    def fake_post(url, **kwargs):
        calls.append(url)
        return response(500 if len(calls) == 1 else 200)

    monkeypatch.setattr(requests, "post", fake_post)
    assert cli.main() == 1
    first = json.loads(capsys.readouterr().out)
    assert first["errors"] and "HTTP 500" in first["errors"][0]
    assert not json.loads(state_path.read_text())["cases"][case.case_id].get("form_notified")
    assert cli.main() == 0
    second = json.loads(capsys.readouterr().out)
    assert second["form_notifications"] == 1
    assert cli.main() == 0
    third = json.loads(capsys.readouterr().out)
    assert third["form_notifications"] == 0
    assert len(calls) == 2
    assert all(url == WEBHOOK for url in calls)
    assert len(calendar.calls) == 1
    # 成果物2種類と保存状態のどこにも偽物の秘密値が出ない。
    saved = json.dumps([first, second, third]) + state_path.read_text()
    saved += "".join(p.read_text() for p in (tmp_path / "logs").glob("*.jsonl"))
    assert TOKEN not in saved
    assert "api/webhooks" not in saved
