# 本日の需給状況ニュース

JPX公式「銘柄別信用取引残高」のPDFを毎営業日取得し、信用残変化をもとに銘柄別の需給スコア（-100〜+100点）を算出、需給改善TOP10と需給悪化WORST10をLINEへ通知するGitHub Actionsプロジェクトです。

## 動作スケジュール

- **18:15（日本時間、月〜金）**：JPXページから最新3日分のPDFを取得し、ランキングを計算。`data/latest_report.json` をリポジトリに保存します。
- **翌営業日の8:30（日本時間、月〜金）**：保存したTOP10・WORST10をLINEへ通知します。
- 土日・日本の祝日・12月31日〜1月3日は処理をスキップします。
- GitHub ActionsのscheduleはUTC基準で、実行時刻に遅れが生じる場合があります。

JPXは信用残データを16:00ごろに公開しますが、申込日付は公表日の前営業日です。LINE通知にもPDFの申込日を明記します。したがって、通知は「その朝の最新公表済み信用残」についての情報であり、発表されていない当日分の信用残を推定するものではありません。

## スコアの考え方（初期版）

各項目は銘柄間の相対順位を -100〜+100 に正規化し、指定した配点で合成します。

| 項目 | 配点 | スコアが高くなる変化 |
|---|---:|---|
| 信用買い残の変化 | 35点 | 買い残が減少 |
| 信用売り残の変化 | 25点 | 売り残が増加 |
| 信用倍率の変化 | 25点 | 信用倍率が低下 |
| 直近3日分の需給傾向 | 15点 | 改善傾向が継続 |

- 表示例は `+95点`、`-79点` のように符号付きです。
- 信用残の小さい銘柄で変化率が極端にならないよう、変化率の分母に最低残高を設けます。
- 通常の「普通株式」行を対象とし、ETF・ETNなどの受益証券系は初期版では除外します。
- スコアは検証前の仮説的な需給指標です。翌日の上昇確率を直接推定した値ではありません。

## LINE通知に必要な設定

LINE Notifyは終了しているため、LINE公式アカウントのMessaging APIを使用します。

GitHubリポジトリの **Settings → Secrets and variables → Actions → New repository secret** から次の2つを登録してください。

| Secret名 | 値 |
|---|---|
| `LINE_CHANNEL_ACCESS_TOKEN` | LINE Developersで発行したMessaging APIのチャネルアクセストークン |
| `LINE_TO` | 通知先のユーザーID、またはBotが参加しているグループID |

通知先が個人の場合、そのユーザーが公式アカウントを友だち追加している必要があります。LINEのチャネルアクセストークンやユーザーIDをソースコードに直接書かないでください。

## GitHubへの導入手順

1. GitHubで新しいリポジトリ（例：`shiju-news`）を作成します。
2. このZIP内のファイルを展開し、`app.py`、`requirements.txt`、`.github/workflows/shiju-news.yml`、`data/` などをリポジトリ直下へアップロードします。
3. リポジトリの **Settings → Actions → General → Workflow permissions** を開き、Actionsによる書き込みを許可します（通常はWorkflow内の `permissions: contents: write` と併せて設定します）。
4. 上記のLINE Secretsを登録します。
5. **Actions** タブから「本日の需給状況ニュース」を選び、`Run workflow` でまず `calculate` を実行します。
6. `data/latest_report.json` が生成され、エラーがないことを確認したら `notify` を手動実行してLINE通知をテストします。
7. 設定が済めば、スケジュール実行されます。

## ローカル実行

Python 3.12推奨です。

```bash
pip install -r requirements.txt
python app.py calculate
# LINE_CHANNEL_ACCESS_TOKEN と LINE_TO を環境変数に設定後
python app.py notify
```

Windows PowerShellで一時的に環境変数を設定する例：

```powershell
$env:LINE_CHANNEL_ACCESS_TOKEN = "チャネルアクセストークン"
$env:LINE_TO = "通知先ID"
python app.py notify
```

## 注意事項・失敗時

- PDFのリンク名・表構成が変更された場合、誤ったスコアを送らずに処理をエラー終了する設計です。
- JPXの公開タイミングに遅延があると、ランキング計算時点で最新PDFがまだ掲載されていないことがあります。Actionsの実行ログで取得対象日を確認してください。
- JPXが同一日付の訂正版PDF（例：`_01.pdf`）を載せた場合は、末尾の改訂番号が大きいファイルを優先します。
- 日本の祝日判定には `jpholiday` を使いますが、JPXの臨時休場などすべての市場カレンダーを網羅するものではありません。
- TOP10・WORST10は需給の変化を相対比較した監視候補です。売買推奨、翌日上昇の保証、空売り残高の直接測定ではありません。
