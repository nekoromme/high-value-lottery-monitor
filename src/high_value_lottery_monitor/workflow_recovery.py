"""RICOH監視の定期起動が欠けた時だけ、GitHub Actions上で再起動する。

GitHubの ``schedule`` は厳密な時刻保証ではなく、混雑時には遅延や取りこぼしが
起こり得る。このモジュールは別ワークフローから本体の実行履歴を確認し、
最後の成功が古い場合だけ ``workflow_dispatch`` で本体を起動する。

外部パッケージに依存させていないため、復旧役自身はインストール処理なしで
すぐ動ける。ログへアクセストークンを出さないことも、この層で保証する。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable


UTC = timezone.utc
API_VERSION = "2022-11-28"
USER_AGENT = "nekoromme-ricoh-monitor-recovery/1.0"
ACTIVE_STATUSES = frozenset({"queued", "in_progress", "waiting", "pending", "requested"})


class RecoveryError(RuntimeError):
    """復旧判定または再起動を安全に完了できなかった時の例外。"""


@dataclass(frozen=True)
class RecoveryDecision:
    """再起動が必要かと、その判断理由。"""

    should_dispatch: bool
    reason: str
    last_success_at: datetime | None = None


def parse_github_time(value: str | None) -> datetime | None:
    """GitHubのISO 8601時刻をUTCの ``datetime`` に変換する。"""

    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _run_time(run: dict[str, Any]) -> datetime:
    """並び替え用の実行時刻。壊れた値は最古として扱う。"""

    return parse_github_time(run.get("created_at")) or datetime.min.replace(tzinfo=UTC)


def decide_recovery(
    runs: list[dict[str, Any]],
    *,
    now: datetime,
    max_success_age_minutes: int,
    active_grace_minutes: int,
) -> RecoveryDecision:
    """実行履歴だけから、重複起動せずに復旧すべきか判断する。"""

    ordered = sorted(runs, key=_run_time, reverse=True)
    successes = [
        run
        for run in ordered
        if run.get("status") == "completed" and run.get("conclusion") == "success"
    ]
    last_success = successes[0] if successes else None
    last_success_at = parse_github_time(
        (last_success or {}).get("updated_at") or (last_success or {}).get("created_at")
    )

    if last_success_at is not None:
        success_age = max(0, int((now - last_success_at).total_seconds() // 60))
        if success_age <= max_success_age_minutes:
            return RecoveryDecision(
                should_dispatch=False,
                reason=(
                    f"最後の正常終了から{success_age}分。"
                    f"復旧基準の{max_success_age_minutes}分以内です"
                ),
                last_success_at=last_success_at,
            )

    # 本体がすでに起動済みなら、その完了を待つ。古く固まった実行は本体側の
    # timeout-minutesで終了するため、猶予を超えたものだけ復旧対象に戻す。
    for run in ordered:
        if str(run.get("status")) not in ACTIVE_STATUSES:
            continue
        started_at = _run_time(run)
        active_age = max(0, int((now - started_at).total_seconds() // 60))
        if active_age <= active_grace_minutes:
            return RecoveryDecision(
                should_dispatch=False,
                reason=f"本体が起動済みです（開始から{active_age}分）",
                last_success_at=last_success_at,
            )

    if last_success_at is None:
        return RecoveryDecision(
            should_dispatch=True,
            reason="正常終了の履歴がないため、本体を起動します",
        )

    success_age = max(0, int((now - last_success_at).total_seconds() // 60))
    return RecoveryDecision(
        should_dispatch=True,
        reason=(
            f"最後の正常終了から{success_age}分経過し、"
            f"復旧基準の{max_success_age_minutes}分を超えました"
        ),
        last_success_at=last_success_at,
    )


class GitHubActionsClient:
    """必要なGitHub APIだけを呼ぶ、トークン非表示の小さなクライアント。"""

    def __init__(
        self,
        *,
        repository: str,
        token: str,
        api_url: str = "https://api.github.com",
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if "/" not in repository:
            raise RecoveryError("GITHUB_REPOSITORY が owner/name 形式ではありません")
        if not token.strip():
            raise RecoveryError("GITHUB_TOKEN が空です")
        self.repository = repository
        self.token = token.strip()
        self.api_url = api_url.rstrip("/")
        self.sleep = sleep

    def _request(
        self,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
    ) -> Any:
        url = f"{self.api_url}{path}"
        data = None if payload is None else json.dumps(payload).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": USER_AGENT,
        }
        if data is not None:
            headers["Content-Type"] = "application/json"

        # 履歴取得だけは一時的なGitHub側エラーを3回まで再試行する。
        # 起動POSTの再試行は、応答だけ消えた場合の二重起動を避けるため行わない。
        delays = (0, 2, 6) if method == "GET" else (0,)
        last_error: Exception | None = None
        for attempt, delay in enumerate(delays):
            if delay:
                self.sleep(delay)
            request = urllib.request.Request(url, data=data, headers=headers, method=method)
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    body = response.read()
                    return json.loads(body) if body else {}
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:1000]
                last_error = RecoveryError(
                    f"GitHub API {method} {path} が HTTP {exc.code}: {body}"
                )
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt == len(delays) - 1:
                    raise last_error from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                last_error = RecoveryError(
                    f"GitHub API {method} {path} への接続に失敗しました: {exc}"
                )
                if attempt == len(delays) - 1:
                    raise last_error from exc

        raise RecoveryError(str(last_error or "GitHub APIで不明なエラーが発生しました"))

    def workflow_runs(self, workflow: str) -> list[dict[str, Any]]:
        encoded_workflow = urllib.parse.quote(workflow, safe="")
        result = self._request(
            "GET",
            (
                f"/repos/{self.repository}/actions/workflows/"
                f"{encoded_workflow}/runs?per_page=30"
            ),
        )
        return list(result.get("workflow_runs", []))

    def dispatch(self, workflow: str, *, ref: str, mode: str = "auto") -> None:
        encoded_workflow = urllib.parse.quote(workflow, safe="")
        self._request(
            "POST",
            f"/repos/{self.repository}/actions/workflows/{encoded_workflow}/dispatches",
            {"ref": ref, "inputs": {"mode": mode}},
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="止まったRICOH監視を自動で再起動します")
    parser.add_argument("--workflow", default="monitor.yml", help="監視本体のファイル名")
    parser.add_argument("--ref", default="main", help="起動するブランチ")
    parser.add_argument(
        "--max-age-minutes",
        type=int,
        default=450,
        help="最後の成功から何分で自動復旧するか",
    )
    parser.add_argument(
        "--active-grace-minutes",
        type=int,
        default=30,
        help="起動済みの本体を待つ時間",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.max_age_minutes <= 0 or args.active_grace_minutes <= 0:
        print("復旧基準と実行待ち時間は1分以上にしてください", file=sys.stderr)
        return 2

    try:
        client = GitHubActionsClient(
            repository=os.environ.get("GITHUB_REPOSITORY", ""),
            token=os.environ.get("GITHUB_TOKEN", ""),
            api_url=os.environ.get("GITHUB_API_URL", "https://api.github.com"),
        )
        now = datetime.now(UTC)
        runs = client.workflow_runs(args.workflow)
        decision = decide_recovery(
            runs,
            now=now,
            max_success_age_minutes=args.max_age_minutes,
            active_grace_minutes=args.active_grace_minutes,
        )
        print(decision.reason)
        if not decision.should_dispatch:
            print("自動復旧は不要です")
            return 0

        client.dispatch(args.workflow, ref=args.ref, mode="auto")
        print(f"{args.workflow} を自動で再起動しました")
        return 0
    except Exception as exc:  # GitHub Actionsのログへ原因を必ず残す。
        print(f"自動復旧に失敗しました: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
