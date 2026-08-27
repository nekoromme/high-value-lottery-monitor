# 高額抽選販売モニター

RICOH公式オンラインストアのGRシリーズ抽選販売を、GitHub Actionsで毎時監視するプログラムです。
日程が発表された時と、応募用Googleフォームが実際に公開された時をDiscordへ通知します。

トレカ監視とは別リポジトリにする前提で作っています。トレカは多数店舗の在庫変化を広く拾う監視、こちらは「当選後の利益を読みやすい高額商材」だけを厳選する監視で、追加基準も障害対応も違うためです。今後ほかの商材を加える場合も、このリポジトリへ監視先ごとの部品を追加できます。

## 監視するもの

- RICOH公式オンラインストアの抽選スケジュール
- 各GR商品ページの応募期間、表示価格、GoogleフォームURL
- 日程を初めて発見した時のDiscord通知
- 応募フォームを初めて発見した時のDiscord通知
- 応募開始時刻のGoogle Calendar予定（設定した場合のみ）
- 公式ページ取得に3回連続で失敗した時の異常通知と、復旧通知

GR SPACE東京の抽選は標準監視から外しています。応募期間中の本人確認と、当選後の店頭受取のために東京へ2回行く必要があり、岩手から使う監視としては割に合わないためです。

GitHub Actionsの監視自体は自動応募をしません。応募フォームをDiscordからすぐ開けるところまでです。

別途、Windows PC用の「RICOH抽選応募準備ツール」を用意しています。このPC版は、受付中の全商品を自動発見し、RICOH公式の表示価格と買取ルデヤの新品・完品買取価格を比較します。購入価格に対する粗利益率が5%未満の商品はフォーム入力せず、5%以上の商品だけを入力済み・送信直前のタブにします。最終送信は必ず本人が行います。

## Windows PC版：価格判定から送信直前まで

### 簡単な使い方

1. GitHubの `Actions` → `Build Windows PC tool` を開きます。
2. 最新の成功した実行を開き、`Artifacts` の `RICOH_PC_Tool` をダウンロードします。
3. ZIPを展開し、`RICOH_Entry_Assistant.exe` をダブルクリックします。
4. 初回だけ、RICOHの注文者情報と同じ氏名・住所等を入力します。
5. 対象になった各タブの内容を確認し、自分で「送信」を押します。

本人情報は展開したフォルダの `pc_config.json` にだけ保存し、GitHub、Discord、実行ログには入れません。設定し直す場合は `PC_RECONFIGURE.bat` を使います。

### 価格判定の安全策

- 買取ルデヤは曖昧な機種名検索ではなく、6機種それぞれの13桁JANコードで照合します。
- 粗利益率は `(買取ルデヤ価格 - RICOH購入価格) ÷ RICOH購入価格 × 100` で計算します。
- 粗利益率が5%以上の商品だけ入力対象です。5%ちょうどは対象、5%未満と赤字は除外します。
- RICOH価格、買取価格、JAN照合のいずれかが不明なら、推測せずその商品を入力対象から外します。
- 通常はRICOH商品ページの公開表示価格を購入価格に使います。実際の会員価格が異なる場合だけ、`pc_config.json` の `purchase_price_overrides` で機種別に実購入額を設定できます。
- 送料や買取手数料などは現時点の判定へ含めません。判定画面には単純差額を表示します。
- ツール内に送信処理は実装していません。入力・選択・入力後検査で停止します。

ソースから実行する場合は、Windowsで `RICOH_PC_START.bat` をダブルクリックします。Python 3が必要ですが、専用環境と必要部品は初回に自動準備します。

## 誤通知を避ける仕組み

- 初回の自動実行は `baseline` になり、その時点で掲載中の案件を通知しません。
- 日程だけ掲載済みでフォームがまだ無い場合は、後からフォームが出た瞬間だけ通知します。
- 同じ抽選の通知済み状態を `monitor-state` ブランチへ保存します。
- 期限切れ案件を遅れて発見しても通知しません。
- 状態JSONが壊れた場合は勝手に初期化せず停止します。全件を新着扱いする事故を防ぐためです。

## GitHubでの設定

### 1. リポジトリを用意する

推奨名は `high-value-lottery-monitor` です。公開リポジトリで構いません。ただし、下記のSecret値はコードへ直接書かないでください。

### 2. Repository secretsを登録する

リポジトリの `Settings` → `Secrets and variables` → `Actions` → `Secrets` へ、次の3件を登録します。

| 名前 | 内容 | 必須 |
|---|---|---|
| `DISCORD_WEBHOOK_URL` | 通知先DiscordのWebhook URL | Discord通知に必須 |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | GoogleサービスアカウントJSONの全文、またはBase64 | Calendarを使う場合 |
| `GOOGLE_CALENDAR_ID` | 予定を入れるカレンダーID | Calendarを使う場合 |

Google Calendarを使う場合は、対象カレンダーをサービスアカウントのメールアドレスへ「予定の変更」権限で共有します。Calendarの2項目を空にすれば、Discordだけで動きます。

### 3. Repository variableを登録する

同じ画面の `Variables` へ次を登録します。

| 名前 | 例 |
|---|---|
| `MONITOR_USER_AGENT_CONTACT` | `https://github.com/nekoromme/high-value-lottery-monitor` |

これはRICOH側のログに、正体不明のbotではなく連絡先付きの監視として表示するためのものです。

### 4. 初回実行を確認する

リポジトリの `Actions` → `RICOH lottery monitor` → `Run workflow` で、`auto` のまま実行します。

初回結果に `"mode": "baseline(auto)"` と出れば正常です。この回は誤通知防止のためDiscord通知もCalendar登録もしません。以後は日本時間の毎時17分ごろに自動実行されます。

GitHub Actionsの予約実行は、混雑時に遅れたり、まれに起動自体が抜けたりします。
その対策として `RICOH monitor automatic recovery` が毎時47分に本体の履歴を
確認します。最後の正常終了から90分を超え、現在実行中でもない場合だけ本体を
自動で再起動します。通常時は履歴を確認するだけなので、監視を二重起動しません。

## 手動実行モード

| モード | 用途 |
|---|---|
| `auto` | 通常はこれ。初回だけbaseline、以後は通常監視 |
| `dry-run` | 公式ページの取得・解析だけ。通知、Calendar、状態更新を一切しない |
| `baseline` | 現在の掲載をすべて既知扱いにする。通知履歴を整理する時だけ使う |
| `run` | 通常監視を即時実行。初期化前は安全のため停止する |

## ログの見方

各実行のActions画面では、最後に次のような集計が出ます。

```json
{
  "mode": "run",
  "detected_cases": 6,
  "new_cases": 0,
  "schedule_notifications": 0,
  "form_notifications": 1,
  "calendar_updates": 1,
  "errors": []
}
```

さらに `Artifacts` の `ricoh-monitor-log-...` を開くと、どのURLを取得できたか、期限切れを抑止したか、通知に失敗したかを1行ずつ確認できます。ログは30日保存します。

Actionsが赤くなった場合は、まず次を確認します。

1. `自動テスト` が失敗していないか
2. `RICOH公式オンラインストアを監視` の末尾にある `errors`
3. Artifacts内のJSONLログ
4. Repository secretsの名前に打ち間違いがないか
5. Google Calendarを使う場合、サービスアカウントへ共有済みか

## ローカルで確認する場合

Python 3.11以上で次を実行します。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -e ".[test]"
python -m pytest
lottery-monitor --mode dry-run
```

`dry-run` はDiscordやCalendarへ送らず、公式ページを読めるかだけを確認します。

## 今後ほかの商材を足す基準

むやみに監視先を増やすより、最低でも次を満たすものだけ追加する設計です。

- 抽選時点で出口となる買取店や相場が十分に見える
- 利幅が送料、手数料、値下がり余地を超える
- 当選後のキャンセル条件や受取条件が現実的
- 規約上、監視や応募が問題にならない
- 応募工数に対して期待値がある

追加先ごとに `src/high_value_lottery_monitor/providers/` へ解析部品を分ければ、RICOHの修正がほかの監視を巻き込まない構成になっています。
