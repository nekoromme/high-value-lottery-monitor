"""Windows PCでRICOH抽選フォームを送信直前まで準備する。

処理順:
1. RICOH公式から受付中の応募フォームと表示価格を取得する。
2. JANコードで買取ルデヤの新品買取価格を取得する。
3. RICOH購入価格の方が高い商品は除外する。
4. 対象だけをブラウザで入力し、送信ボタンは絶対に押さずに停止する。

応募者情報はPC内の ``pc_config.json`` にだけ保存する。ログへは書かない。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from zoneinfo import ZoneInfo

import requests

from high_value_lottery_monitor.models import LotteryCase
from high_value_lottery_monitor.providers.ricoh import RicohOnlineStoreProvider
from high_value_lottery_monitor.providers.rudeya import (
    MODEL_JANS,
    RudeyaClient,
    RudeyaPriceError,
    RudeyaQuote,
)

if TYPE_CHECKING:
    from playwright.sync_api import BrowserContext, Page


JST = ZoneInfo("Asia/Tokyo")
PREFECTURES = (
    "北海道", "青森県", "岩手県", "宮城県", "秋田県", "山形県", "福島県",
    "群馬県", "栃木県", "茨城県", "埼玉県", "千葉県", "東京都", "神奈川県",
    "新潟県", "富山県", "石川県", "福井県", "山梨県", "長野県", "岐阜県",
    "静岡県", "愛知県", "三重県", "滋賀県", "京都府", "大阪府", "兵庫県",
    "奈良県", "和歌山県", "鳥取県", "島根県", "岡山県", "広島県", "山口県",
    "徳島県", "香川県", "愛媛県", "高知県", "福岡県", "佐賀県", "長崎県",
    "熊本県", "大分県", "宮崎県", "鹿児島県", "沖縄県",
)


@dataclass(frozen=True, slots=True)
class Applicant:
    """Googleフォームへ入力する本人情報。"""

    email: str
    last_name: str
    first_name: str
    postal_code: str
    prefecture: str
    address: str
    phone: str

    @classmethod
    def from_dict(cls, value: dict) -> "Applicant":
        applicant = cls(
            email=str(value.get("email", "")).strip(),
            last_name=str(value.get("last_name", "")).strip(),
            first_name=str(value.get("first_name", "")).strip(),
            postal_code=str(value.get("postal_code", "")).strip(),
            prefecture=str(value.get("prefecture", "")).strip(),
            address=str(value.get("address", "")).strip(),
            phone=re.sub(r"[^0-9]", "", str(value.get("phone", ""))),
        )
        applicant.validate()
        return applicant

    def validate(self) -> None:
        errors: list[str] = []
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", self.email):
            errors.append("メールアドレス")
        if not self.last_name:
            errors.append("氏名（姓）")
        if not self.first_name:
            errors.append("氏名（名）")
        if not re.fullmatch(r"\d{3}-\d{4}", self.postal_code):
            errors.append("郵便番号（123-4567形式）")
        if self.prefecture not in PREFECTURES:
            errors.append("都道府県")
        if not self.address:
            errors.append("市区町村・丁目番地号")
        if not re.fullmatch(r"\d{10,11}", self.phone):
            errors.append("電話番号（数字10～11桁）")
        if errors:
            raise ValueError("設定が不正です: " + "、".join(errors))


@dataclass(frozen=True, slots=True)
class PriceDecision:
    """1機種をフォーム入力するかどうかの判定。"""

    model_name: str
    purchase_price_yen: int | None
    buyback_price_yen: int | None
    gross_difference_yen: int | None
    should_fill: bool
    reason: str
    form_url: str | None
    quote_url: str | None


class LocalAuditLog:
    """個人情報を含めず、価格判定と失敗原因だけをJSONLへ残す。"""

    def __init__(self, directory: Path):
        directory.mkdir(parents=True, exist_ok=True)
        now = datetime.now(JST).strftime("%Y%m%d-%H%M%S")
        self.path = directory / f"pc-run-{now}.jsonl"

    def write(self, event: str, **details: object) -> None:
        record = {
            "timestamp": datetime.now(JST).isoformat(),
            "event": event,
            **details,
        }
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")


def _application_dir() -> Path:
    """EXE版ならEXEの隣、ソース版なら実行した場所を使う。"""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path.cwd()


def _parser() -> argparse.ArgumentParser:
    base_dir = _application_dir()
    parser = argparse.ArgumentParser(description="RICOH抽選応募準備（最終送信はしません）")
    parser.add_argument("--config", type=Path, default=base_dir / "pc_config.json")
    parser.add_argument("--setup", action="store_true", help="本人情報を設定し直す")
    parser.add_argument(
        "--dry-run", action="store_true", help="価格判定だけ行いブラウザを開かない"
    )
    parser.add_argument(
        "--self-test", action="store_true", help="Windows版の同梱部品だけを確認する"
    )
    return parser


def _prompt_nonempty(label: str) -> str:
    while True:
        value = input(f"{label}: ").strip()
        if value:
            return value
        print("空欄にはできません。")


def setup_config(path: Path) -> Applicant:
    """初回だけ対話入力し、本人情報をローカルへ保存する。"""

    print("\n初回設定です。RICOHの注文者情報と同じ内容を入力してください。")
    print("この情報はこのPC内だけに保存し、GitHubやDiscordへ送りません。\n")
    while True:
        raw = {
            "email": _prompt_nonempty("メールアドレス"),
            "last_name": _prompt_nonempty("氏名（姓）"),
            "first_name": _prompt_nonempty("氏名（名）"),
            "postal_code": _prompt_nonempty("郵便番号（例 021-0000）"),
            "prefecture": _prompt_nonempty("都道府県（例 岩手県）"),
            "address": _prompt_nonempty("市区町村・丁目番地号"),
            "phone": _prompt_nonempty("電話番号（ハイフン不要）"),
        }
        try:
            applicant = Applicant.from_dict(raw)
        except ValueError as exc:
            print(f"\n{exc}\n最初から入力し直してください。\n")
            continue
        break

    payload = {
        "version": 1,
        "applicant": asdict(applicant),
        # 会員価格など、公式ページの表示価格と実際の購入額が違う場合だけ
        # 機種名: 金額 を手作業で追加できる。通常は空のままでよい。
        "purchase_price_overrides": {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"\n設定を保存しました: {path}")
    return applicant


def load_config(path: Path, *, force_setup: bool = False) -> tuple[Applicant, dict[str, int]]:
    if force_setup or not path.exists():
        return setup_config(path), {}

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"設定ファイルを読めません: {path}: {exc}") from exc
    if data.get("version") != 1 or not isinstance(data.get("applicant"), dict):
        raise RuntimeError("pc_config.jsonの形式が不正です。--setupで作り直してください")

    applicant = Applicant.from_dict(data["applicant"])
    raw_overrides = data.get("purchase_price_overrides", {})
    if not isinstance(raw_overrides, dict):
        raise RuntimeError("purchase_price_overridesは辞書形式にしてください")
    overrides: dict[str, int] = {}
    for model_name, raw_price in raw_overrides.items():
        price = int(raw_price)
        if model_name not in MODEL_JANS or price <= 0:
            raise RuntimeError(f"購入価格の上書き設定が不正です: {model_name}: {raw_price}")
        overrides[model_name] = price
    return applicant, overrides


def is_case_open(case: LotteryCase, now: datetime) -> bool:
    """フォームがあり、現在が受付期間内ならTrue。"""

    if not case.form_url:
        return False
    if case.starts_at is not None and now < case.starts_at:
        return False
    if case.ends_at is not None and now > case.ends_at:
        return False
    return True


def decide_price(
    case: LotteryCase,
    quote: RudeyaQuote | None,
    *,
    purchase_price_override: int | None = None,
    quote_error: str | None = None,
) -> PriceDecision:
    """指定どおり『購入価格 > 買取価格』の時だけ赤字除外する。

    価格不明・取得失敗・JAN未登録は安全側に倒し、自動入力しない。
    購入価格と買取価格が同額なら、ユーザー指定に合わせて入力対象とする。
    """

    purchase_price = purchase_price_override or case.price_yen
    buyback_price = quote.price_yen if quote else None
    quote_url = quote.source_url if quote else None

    if purchase_price is None:
        return PriceDecision(
            case.model_name, None, buyback_price, None, False,
            "RICOH購入価格を取得できないため要確認", case.form_url, quote_url,
        )
    if quote is None:
        detail = quote_error or "買取価格を取得できないため要確認"
        return PriceDecision(
            case.model_name, purchase_price, None, None, False,
            detail, case.form_url, None,
        )

    difference = buyback_price - purchase_price
    if purchase_price > buyback_price:
        reason = f"赤字見込み {abs(difference):,}円のため除外"
        should_fill = False
    else:
        reason = f"差額 {difference:+,}円のため入力対象"
        should_fill = True
    return PriceDecision(
        case.model_name,
        purchase_price,
        buyback_price,
        difference,
        should_fill,
        reason,
        case.form_url,
        quote_url,
    )


def _yen(value: int | None) -> str:
    return "取得失敗" if value is None else f"{value:,}円"


def print_decisions(decisions: list[PriceDecision]) -> None:
    print("\n価格判定（買取ルデヤは新品・完品の表示価格）")
    print("=" * 72)
    for item in decisions:
        status = "入力する" if item.should_fill else "入力しない"
        print(f"[{status}] {item.model_name}")
        print(
            f"  RICOH {_yen(item.purchase_price_yen)} / "
            f"ルデヤ {_yen(item.buyback_price_yen)} / {item.reason}"
        )
    print("=" * 72)


def _question(page: "Page", label: str):
    """質問見出しから、その質問だけを含むコンテナを取得する。"""

    heading = page.get_by_role(
        "heading",
        name=re.compile(
            rf"^{re.escape(label)}\s*(?:必須の質問|\*)?\s*$"
        ),
    )
    return heading.locator("xpath=ancestor::*[@role='listitem'][1]")


def fill_google_form(page: "Page", case: LotteryCase, applicant: Applicant) -> None:
    """フォームを入力・検査し、送信ボタンに触れずに返す。"""

    assert case.form_url is not None
    page.goto(case.form_url, wait_until="domcontentloaded", timeout=60_000)
    page.get_by_role("heading", name=re.compile("抽選販売エントリーフォーム")).wait_for(
        state="visible", timeout=30_000
    )
    title = page.get_by_role("heading", level=1).inner_text()
    if case.model_name.upper().replace("RICOH ", "") not in title.upper():
        raise RuntimeError(f"フォームの商品名が一致しません: {case.model_name} / {title}")

    fields = {
        "メールアドレス": applicant.email,
        "メールアドレス(確認用)": applicant.email,
        "氏名（姓）": applicant.last_name,
        "氏名（名）": applicant.first_name,
        "郵便番号": applicant.postal_code,
        "住所：市区町村・丁目番地号": applicant.address,
        "電話番号": applicant.phone,
    }
    for label, value in fields.items():
        item = _question(page, label)
        textbox = item.locator("input:not([type=hidden]), textarea").first
        textbox.fill(value)
        if textbox.input_value() != value:
            raise RuntimeError(f"入力後の検査に失敗しました: {label}")

    prefecture_item = _question(page, "住所：都道府県")
    prefecture_listbox = prefecture_item.get_by_role("listbox")
    prefecture_listbox.click()
    page.get_by_role("option", name=applicant.prefecture, exact=True).click()
    if applicant.prefecture not in prefecture_listbox.inner_text():
        raise RuntimeError("都道府県の選択後検査に失敗しました")

    consent = _question(
        page, "ご注意事項をご確認いただき、いずれかにチェックしてください。"
    ).get_by_role("radio", name="注意事項に同意します。", exact=True)
    consent.click()
    if consent.get_attribute("aria-checked") != "true":
        raise RuntimeError("注意事項への同意を選択できませんでした")

    robot = _question(page, "私はロボットではありません。").get_by_role(
        "radio", name="はい", exact=True
    )
    robot.click()
    if robot.get_attribute("aria-checked") != "true":
        raise RuntimeError("『私はロボットではありません』を選択できませんでした")

    submit = page.get_by_role("button", name="送信", exact=True)
    if submit.count() != 1 or not submit.is_visible():
        raise RuntimeError("送信直前の画面を確認できません")
    # 重要: submit.click() は実装しない。最終送信は必ずユーザーが行う。


def _launch_context(playwright, profile_dir: Path) -> "BrowserContext":
    """既存Chrome、Edge、Playwright Chromiumの順に起動を試す。"""

    errors: list[str] = []
    for channel in ("chrome", "msedge", None):
        try:
            kwargs = {
                "user_data_dir": str(profile_dir),
                "headless": False,
                "no_viewport": True,
                "args": ["--start-maximized"],
            }
            if channel:
                kwargs["channel"] = channel
            return playwright.chromium.launch_persistent_context(**kwargs)
        except Exception as exc:  # noqa: BLE001 - 起動候補ごとに次へ進む
            errors.append(f"{channel or 'chromium'}: {exc}")
    raise RuntimeError("ブラウザを起動できません。" + " | ".join(errors))


def open_and_fill_forms(
    decisions: list[PriceDecision],
    cases_by_model: dict[str, LotteryCase],
    applicant: Applicant,
    *,
    base_dir: Path,
    audit: LocalAuditLog,
) -> int:
    """対象フォームを別タブで準備し、ユーザーが送信し終えるまで待つ。"""

    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "ブラウザ部品が未導入です。READMEのPC版セットアップを実行してください"
        ) from exc

    eligible = [item for item in decisions if item.should_fill]
    if not eligible:
        print("\n入力対象はありません。ブラウザは開きません。")
        return 0

    prepared_pages: list[Page] = []
    profile_dir = base_dir / "pc_browser_profile"
    with sync_playwright() as playwright:
        context = _launch_context(playwright, profile_dir)
        try:
            # persistent contextが自動生成した空タブは後で閉じる。
            original_pages = list(context.pages)
            for decision in eligible:
                case = cases_by_model[decision.model_name]
                page = context.new_page()
                try:
                    fill_google_form(page, case, applicant)
                except Exception as exc:  # noqa: BLE001 - 1機種だけ隔離する
                    audit.write(
                        "form_fill_error",
                        model_name=decision.model_name,
                        error=repr(exc),
                    )
                    print(f"[入力失敗] {decision.model_name}: {exc}")
                    page.close()
                    continue
                prepared_pages.append(page)
                audit.write("form_prepared", model_name=decision.model_name)
                print(f"[送信直前まで完了] {decision.model_name}")

            for page in original_pages:
                if page not in prepared_pages and page.url == "about:blank":
                    page.close()

            if not prepared_pages:
                raise RuntimeError("すべてのフォーム入力に失敗しました。ログを確認してください")

            prepared_pages[0].bring_to_front()
            print("\nブラウザの各タブを確認し、問題なければ自分で『送信』を押してください。")
            print("この黒い画面を先に閉じるとブラウザも閉じます。")
            input("全フォームの送信が終わったら、ここでEnterキーを押してください: ")
        finally:
            context.close()
    return len(prepared_pages)


def main() -> int:
    # Windowsの英語環境など、コンソールが日本語を表現できない場合でも
    # 起動そのものは止めない。日本語Windowsでは通常どおり表示される。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")

    args = _parser().parse_args()
    if args.self_test:
        assert len(MODEL_JANS) == 6
        assert all(re.fullmatch(r"\d{13}", jan) for jan in MODEL_JANS.values())
        print("セルフテスト正常")
        return 0

    base_dir = args.config.resolve().parent
    audit = LocalAuditLog(base_dir / "pc_logs")
    try:
        applicant, overrides = load_config(args.config, force_setup=args.setup)
        now = datetime.now(JST)
        ricoh = RicohOnlineStoreProvider(
            user_agent_contact=os.getenv("MONITOR_USER_AGENT_CONTACT")
        )
        cases, diagnostics = ricoh.fetch_cases()
        for diagnostic in diagnostics:
            audit.write(
                "source_fetch",
                url=diagnostic.url,
                ok=diagnostic.ok,
                detail=diagnostic.detail,
            )

        open_cases = [case for case in cases if is_case_open(case, now)]
        if not open_cases:
            print("現在受付中のRICOH抽選フォームはありません。")
            audit.write("no_open_cases")
            return 0

        rudeya = RudeyaClient(
            user_agent_contact=os.getenv("MONITOR_USER_AGENT_CONTACT")
        )
        decisions: list[PriceDecision] = []
        for case in open_cases:
            quote: RudeyaQuote | None = None
            error: str | None = None
            try:
                quote = rudeya.fetch_quote(case.model_name)
            except (RudeyaPriceError, requests.RequestException) as exc:
                error = f"買取ルデヤ取得失敗のため要確認: {exc}"
            decision = decide_price(
                case,
                quote,
                purchase_price_override=overrides.get(case.model_name),
                quote_error=error,
            )
            decisions.append(decision)
            audit.write(
                "price_decision",
                model_name=decision.model_name,
                purchase_price_yen=decision.purchase_price_yen,
                buyback_price_yen=decision.buyback_price_yen,
                gross_difference_yen=decision.gross_difference_yen,
                should_fill=decision.should_fill,
                reason=decision.reason,
                quote_url=decision.quote_url,
            )

        print_decisions(decisions)
        if args.dry_run:
            print(f"\n価格判定だけで終了しました。ログ: {audit.path}")
            return 0

        count = open_and_fill_forms(
            decisions,
            {case.model_name: case for case in open_cases},
            applicant,
            base_dir=base_dir,
            audit=audit,
        )
        print(f"\n完了: {count}件を送信直前まで準備しました。ログ: {audit.path}")
        return 0
    except KeyboardInterrupt:
        print("\n中断しました。送信は行っていません。")
        audit.write("cancelled_by_user")
        return 130
    except Exception as exc:  # noqa: BLE001 - PC画面で原因を説明して止める
        audit.write("run_failed", error=repr(exc))
        print(f"\n【停止】{exc}")
        print(f"ログ: {audit.path}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
