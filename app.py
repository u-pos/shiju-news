    """ランキングをスマートフォンで読みやすい複数行形式に整形する。"""
    lines = [f"【{title}】", ""]
    for index, rec in enumerate(rows, start=1):
        score = int(rec["score"])
        code = rec["code"]
        name = rec["name"]
        lines.extend([
            f"【{index}位】{score:+d}点",
            f"{name}（{code}）",
            f"買残 {format_pct(rec.get('buy_change_pct'))}",
            f"売残 {format_pct(rec.get('sell_change_pct'))}",
            display_ratio(rec),
            "",
        ])
    # 最後に余分な空行が連続しないようにする。
    return "\n".join(lines).rstrip()


def format_report(report: dict[str, Any]) -> str:
    """TOP10とWORST10を1通のLINEメッセージにまとめる。"""
    source_date = report.get("source_date", "不明")
    top_text = format_ranking("需給改善 TOP10", report.get("top10", []))
    worst_text = format_ranking("需給悪化 WORST10", report.get("worst10", []))
    return "\n".join([
        "【本日の需給状況ニュース】",
        f"信用残の申込日：{source_date}",
        "",
        top_text,
        "",
        worst_text,
    ])


def push_line(messages: list[str]) -> None:
    token = os.getenv("LINE_CHANNEL_ACCESS_TOKEN", "").strip()
    to = os.getenv("LINE_TO", "").strip()
    if not token or not to:
        raise RuntimeError(
            "LINE_CHANNEL_ACCESS_TOKEN と LINE_TO が設定されていません。"
            "GitHub Secrets に設定してください。"
        )
    # LINEテキストメッセージは1件あたり最大5000文字。念のため分割して送る。
    payload_messages = []
    for message in messages:
        if len(message) <= 4900:
            payload_messages.append({"type": "text", "text": message})
        else:
            for offset in range(0, len(message), 4800):
                payload_messages.append({"type": "text", "text": message[offset:offset + 4800]})
    if not payload_messages:
        raise RuntimeError("LINEに送信するメッセージが空です。")
    for start in range(0, len(payload_messages), 5):
        batch = payload_messages[start:start + 5]
        response = requests.post(
            LINE_PUSH_URL,
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"to": to, "messages": batch},
            timeout=30,
        )
        if not response.ok:
            raise RuntimeError(f"LINE Messaging API送信失敗: HTTP {response.status_code} / {response.text[:1000]}")
        log.info("LINE通知送信成功 (%d messages)", len(batch))


def notify_from_saved_report() -> None:
    today = datetime.now(TZ).date()
    if not is_jp_market_business_day(today):
        log.info("本日は日本の市場休業日と判定したため、LINE通知をスキップします。")
        return
    if not LATEST_REPORT_PATH.exists():
        raise FileNotFoundError("data/latest_report.json がありません。先に calculate を実行してください。")
    report = json.loads(LATEST_REPORT_PATH.read_text(encoding="utf-8"))
    calculated_at = datetime.fromisoformat(report["calculated_at"])
    age = datetime.now(TZ) - calculated_at.astimezone(TZ)
    if age > timedelta(days=5):
        raise RuntimeError(f"保存済みランキングが古すぎるため通知を中止します（{age.days}日経過）。最新データを再計算してください。")

    message = format_report(report)
    note = (
        "\n\n※スコアは信用残の相対比較で、翌日上昇・下落を保証するものではありません。"
        "寄り付き前の監視候補としてご利用ください。"
    )
    # TOP10とWORST10をまとめ、1通のLINEメッセージとして送信する。
    push_line([message + note])


def main(argv: list[str]) -> int:
    if len(argv) != 2 or argv[1] not in {"calculate", "notify"}:
        print("Usage: python app.py calculate|notify", file=sys.stderr)
        return 2
    try:
        if argv[1] == "calculate":
            calculate_and_save()
        else:
            notify_from_saved_report()
    except Exception:
        log.exception("処理に失敗しました。ランキング誤配信を避けるため通知は停止します。")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
