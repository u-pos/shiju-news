#!/usr/bin/env python3
"""本日の需給状況ニュース

JPX 銘柄別信用取引残高 PDF を読み取り、信用残の変化から相対スコアを計算し、
GitHub Actions 経由で LINE Messaging API にランキングを通知します。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import pdfplumber
import requests
from bs4 import BeautifulSoup

try:
    import jpholiday
except ImportError:  # テスト・ローカル実行時の防御
    jpholiday = None

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
LATEST_REPORT_PATH = DATA_DIR / "latest_report.json"
JPX_PAGE = "https://www.jpx.co.jp/markets/statistics-equities/margin/01.html"
LINE_PUSH_URL = "https://api.line.me/v2/bot/message/push"
TZ = ZoneInfo("Asia/Tokyo")
USER_AGENT = "Mozilla/5.0 (compatible; ShijuNewsBot/1.0; +https://github.com/)"

# 総合スコアの配点（合計100点）
WEIGHT_BUY_REDUCTION = 0.35
WEIGHT_SELL_INCREASE = 0.25
WEIGHT_RATIO_IMPROVEMENT = 0.25
WEIGHT_3DAY_TREND = 0.15

# 残高が極端に小さい銘柄の変化率が過大にならないようにする最低分母（株）
MIN_BALANCE_DENOMINATOR = 10_000
# 株数データだけを対象にする。ETF/ETN等は通常「普通株式」以外の表記なので対象外。
COMMON_SHARE_MARKER = "普通株式"
# 株式の表示用コードにはシステム都合で末尾0が付く場合がある。
CODE_ISIN_RE = re.compile(r"(?P<code>[0-9A-Z]{4,5})\s+(?P<isin>JP[0-9A-Z]{10})")
DATE_FILE_RE = re.compile(r"(?P<date>\d{8})_mtall(?:_(?P<revision>\d+))?\.pdf$", re.I)
NUMBER_RE = re.compile(r"(?:▲|▼|△)\s*[\d,]+(?:\.\d+)?|[−–-]\s*[\d,]+(?:\.\d+)?|[\d,]+(?:\.\d+)?%?|\*")

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("shiju-news")


def is_jp_market_business_day(day: date) -> bool:
    """日本の一般祝日と東証の年末年始休業を考慮した簡易営業日判定。"""
    if day.weekday() >= 5:
        return False
    if (day.month, day.day) in {(1, 2), (1, 3), (12, 31)}:
        return False
    if jpholiday is not None and jpholiday.is_holiday(day):
        return False
    return True


def normalize_code(raw: str) -> str:
    code = raw.strip().upper()
    # JPXの内部表示で普通株式コードの末尾に0が付く場合は、4桁コードに戻す。
    if len(code) == 5 and code.endswith("0"):
        code = code[:-1]
    return code


def parse_number_token(token: str) -> int | float | None:
    """PDF表の数値を変換。▲/▼/△は減少として負数に変換する。"""
    token = token.strip().replace(",", "").replace("％", "%")
    if not token or token == "*" or token == "-":
        return None
    negative = token.startswith(("▲", "▼", "△", "−", "–", "-"))
    token = re.sub(r"^[▲▼△−–-]\s*", "", token)
    token = token.rstrip("%")
    try:
        value = float(token) if "." in token else int(token)
    except ValueError:
        return None
    if negative:
        value = -value
    return value


def group_words_into_lines(words: list[dict[str, Any]], tolerance: float = 2.5) -> list[str]:
    """PDF内の単語を縦位置で行にまとめ、表の一銘柄行を復元する。"""
    if not words:
        return []
    words = sorted(words, key=lambda w: (float(w["top"]), float(w["x0"])))
    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    center_y: float | None = None
    for word in words:
        y = float(word["top"])
        if center_y is None or abs(y - center_y) <= tolerance:
            current.append(word)
            center_y = y if center_y is None else (center_y * (len(current) - 1) + y) / len(current)
        else:
            groups.append(current)
            current = [word]
            center_y = y
    if current:
        groups.append(current)
    return [" ".join(str(w["text"]) for w in sorted(group, key=lambda w: float(w["x0"]))) for group in groups]


def name_from_row(line: str, stock_type_index: int) -> str:
    prefix = line[:stock_type_index].strip()
    tokens = prefix.split()
    flags = {"規", "日", "監", "株", "喚", "○", "＊", "*"}
    unit_codes = {"A", "J", "K", "B", "M", "C", "T", "F"}
    while tokens and (tokens[0] in flags or tokens[0] in unit_codes):
        tokens.pop(0)
    return " ".join(tokens).strip()


def parse_pdf(pdf_bytes: bytes, source_date: str, source_url: str) -> dict[str, dict[str, Any]]:
    """JPXの『銘柄別信用取引残高』PDFから普通株式の株数行を抽出する。

    新フォーマットの列順:
      売残高, 売残前日比, 上場比, 買残高, 買残前日比, 上場比, ...
    その後の一般信用/制度信用の内訳列は使用しません。
    """
    if not pdf_bytes.startswith(b"%PDF"):
        raise ValueError(f"PDFではない応答を受信しました: {source_url}")

    result: dict[str, dict[str, Any]] = {}
    with pdfplumber.open(__import__("io").BytesIO(pdf_bytes)) as pdf:
        for page_number, page in enumerate(pdf.pages, start=1):
            words = page.extract_words(x_tolerance=1.5, y_tolerance=2, keep_blank_chars=False)
            for line in group_words_into_lines(words):
                # 会社の普通株式・株数行だけを対象にし、金額行や集計行を除外。
                stock_type_index = line.find(COMMON_SHARE_MARKER)
                if stock_type_index < 0:
                    continue
                marker_match = re.search(r"株数|Shs\.?", line, re.I)
                if not marker_match:
                    continue
                code_match = CODE_ISIN_RE.search(line)
                if not code_match:
                    log.debug("コード/ISINを特定できない行をスキップ: %s", line[:240])
                    continue
                code = normalize_code(code_match.group("code"))
                name = name_from_row(line, stock_type_index)
                if not name:
                    continue

                tail = line[marker_match.end():]
                raw_values = NUMBER_RE.findall(tail)
                if len(raw_values) < 6:
                    log.debug("数値列が不足している行をスキップ (page %d): %s", page_number, line[:240])
                    continue
                values = [parse_number_token(token) for token in raw_values[:6]]
                sell_balance, sell_change, _sell_ratio, buy_balance, buy_change, _buy_ratio = values
                if any(v is None for v in (sell_balance, sell_change, buy_balance, buy_change)):
                    continue

                record = {
                    "date": source_date,
                    "code": code,
                    "name": name,
                    "sell_balance": int(sell_balance),
                    "sell_change": int(sell_change),
                    "buy_balance": int(buy_balance),
                    "buy_change": int(buy_change),
                    "source_url": source_url,
                }
                # PDF上で同一普通株式が重複した場合は同じコードを一件にする。
                result[code] = record

    if not result:
        raise ValueError(
            f"PDFから普通株式の信用残データを1件も抽出できませんでした。"
            f"PDFのレイアウト変更の可能性があります: {source_url}"
        )
    log.info("%s: %d銘柄を抽出", source_date, len(result))
    return result


def find_latest_pdf_links(html: str, page_url: str = JPX_PAGE, limit_dates: int = 3) -> list[tuple[str, str]]:
    """JPXのページから最新の各申込日PDFを探す。訂正版は改訂番号の大きい方を優先。"""
    soup = BeautifulSoup(html, "html.parser")
    by_date: dict[str, tuple[int, str]] = {}
    for anchor in soup.find_all("a", href=True):
        href = urljoin(page_url, anchor["href"].strip())
        path_name = Path(urlparse(href).path).name
        match = DATE_FILE_RE.fullmatch(path_name)
        if not match:
            continue
        day = datetime.strptime(match.group("date"), "%Y%m%d").date().isoformat()
        revision = int(match.group("revision") or 0)
        old = by_date.get(day)
        if old is None or revision > old[0]:
            by_date[day] = (revision, href)
    links = [(day, pair[1]) for day, pair in by_date.items()]
    links.sort(key=lambda item: item[0], reverse=True)
    links = links[:limit_dates]
    if not links:
        raise ValueError("JPX公式ページから mtall PDF のリンクを検出できませんでした。ページ構成が変わった可能性があります。")
    return links


def fetch_latest_snapshots(limit_dates: int = 3) -> list[dict[str, Any]]:
    session = requests.Session()
    session.headers.update({"User-Agent": USER_AGENT})
    page_response = session.get(JPX_PAGE, timeout=30)
    page_response.raise_for_status()
    links = find_latest_pdf_links(page_response.text, limit_dates=limit_dates)
    snapshots = []
    for source_date, url in links:
        response = session.get(url, timeout=90)
        response.raise_for_status()
        records = parse_pdf(response.content, source_date, url)
        snapshots.append({"date": source_date, "url": url, "records": records})
        time.sleep(0.4)
    return snapshots


def clamp(value: float, low: float = -1.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def signed_percentile(values: dict[str, float | None]) -> dict[str, float]:
    """指標値を銘柄間の相対順位（-100～+100）へ変換。同値は平均順位。"""
    valid = [(code, value) for code, value in values.items() if value is not None and math.isfinite(value)]
    output = {code: 0.0 for code in values}
    if len(valid) <= 1:
        return output
    valid.sort(key=lambda pair: pair[1])
    n = len(valid)
    i = 0
    while i < n:
        j = i + 1
        while j < n and valid[j][1] == valid[i][1]:
            j += 1
        # 0-based平均順位を[-100,+100]へ変換
        avg_rank = (i + (j - 1)) / 2
        score = -100.0 + 200.0 * (avg_rank / (n - 1))
        if all(valid[k][1] == valid[0][1] for k in range(n)):
            score = 0.0
        for k in range(i, j):
            output[valid[k][0]] = score
        i = j
    return output


def compute_snapshot_score(records: dict[str, dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """一日分の信用残変化について、買残減・売残増・倍率低下の相対順位を算出。"""
    raw_buy: dict[str, float | None] = {}
    raw_sell: dict[str, float | None] = {}
    raw_ratio: dict[str, float | None] = {}
    metrics_by_code: dict[str, dict[str, Any]] = {}

    for code, rec in records.items():
        sell = int(rec["sell_balance"])
        sell_delta = int(rec["sell_change"])
        buy = int(rec["buy_balance"])
        buy_delta = int(rec["buy_change"])
        prev_sell = sell - sell_delta
        prev_buy = buy - buy_delta
        if sell == buy == sell_delta == buy_delta == 0:
            continue

        # 正なら需給改善方向。最低分母で小さい残高の大きな変化率を抑制。
        buy_reduction = clamp((-buy_delta) / max(prev_buy, MIN_BALANCE_DENOMINATOR))
        sell_increase = clamp(sell_delta / max(prev_sell, MIN_BALANCE_DENOMINATOR))

        current_ratio = (buy / sell) if sell > 0 else None
        previous_ratio = (prev_buy / prev_sell) if prev_sell > 0 else None
        if current_ratio is not None and previous_ratio is not None and previous_ratio > 0:
            ratio_improvement = clamp((previous_ratio - current_ratio) / max(previous_ratio, 0.1))
        else:
            ratio_improvement = None

        raw_buy[code] = buy_reduction
        raw_sell[code] = sell_increase
        raw_ratio[code] = ratio_improvement
        metrics_by_code[code] = {
            **rec,
            "previous_sell_balance": prev_sell,
            "previous_buy_balance": prev_buy,
            "current_ratio": current_ratio,
            "previous_ratio": previous_ratio,
            "buy_change_pct": (buy_delta / prev_buy * 100) if prev_buy > 0 else None,
            "sell_change_pct": (sell_delta / prev_sell * 100) if prev_sell > 0 else None,
            "ratio_change_pct": ((current_ratio / previous_ratio - 1) * 100)
            if current_ratio is not None and previous_ratio is not None and previous_ratio > 0 else None,
        }

    buy_ranks = signed_percentile(raw_buy)
    sell_ranks = signed_percentile(raw_sell)
    ratio_ranks = signed_percentile(raw_ratio)
    base_scores: dict[str, float] = {}
    for code in metrics_by_code:
        base_scores[code] = (
            WEIGHT_BUY_REDUCTION * buy_ranks.get(code, 0.0)
            + WEIGHT_SELL_INCREASE * sell_ranks.get(code, 0.0)
            + WEIGHT_RATIO_IMPROVEMENT * ratio_ranks.get(code, 0.0)
        )
    # base score is bounded to +/-85 because the trend component occupies 15 points.
    for code, rec in metrics_by_code.items():
        rec["buy_reduction_rank"] = round(buy_ranks.get(code, 0.0), 2)
        rec["sell_increase_rank"] = round(sell_ranks.get(code, 0.0), 2)
        rec["ratio_improvement_rank"] = round(ratio_ranks.get(code, 0.0), 2)
        rec["base_score"] = base_scores.get(code, 0.0)
    return metrics_by_code


def calculate_report(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    if not snapshots:
        raise ValueError("スコア計算に必要なデータがありません。")
    scored_snapshots = []
    for snap in snapshots:
        scored_snapshots.append({
            "date": snap["date"],
            "url": snap["url"],
            "scored": compute_snapshot_score(snap["records"]),
        })
    current = scored_snapshots[0]
    previous_days = scored_snapshots[:3]
    all_codes = set(current["scored"].keys())
    scored = []

    for code in all_codes:
        rec = dict(current["scored"][code])
        trend_parts = []
        for snap in previous_days:
            item = snap["scored"].get(code)
            if item is not None:
                # 同日内のスコアを[-100,+100]に換算（基本3指標は合計85点）。
                trend_parts.append(item["base_score"] / 0.85)
        trend_score = sum(trend_parts) / len(trend_parts) if trend_parts else 0.0
        final_score = rec["base_score"] + WEIGHT_3DAY_TREND * trend_score
        rec["trend_score"] = round(trend_score, 2)
        rec["score"] = max(-100, min(100, int(round(final_score))))
        rec["source_date"] = current["date"]
        rec["trend_days_used"] = len(trend_parts)
        scored.append(rec)

    # スコア同値時は、買い残の縮小率が大きい順、その次に銘柄コード順。
    scored.sort(key=lambda r: (-r["score"], -(r["buy_change_pct"] or 0), r["code"]))
    top10 = scored[:10]
    worst10 = sorted(scored, key=lambda r: (r["score"], r["code"]))[:10]
    return {
        "app_name": "本日の需給状況ニュース",
        "source_name": "JPX 銘柄別信用取引残高",
        "source_page": JPX_PAGE,
        "source_date": current["date"],
        "source_url": current["url"],
        "calculated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "formula_version": "v1.0",
        "score_formula": {
            "buy_reduction": 35,
            "sell_increase": 25,
            "credit_ratio_improvement": 25,
            "three_day_trend": 15,
        },
        "history_dates": [snap["date"] for snap in previous_days],
        "universe_count": len(scored),
        "top10": top10,
        "worst10": worst10,
    }


def calculate_and_save() -> None:
    today = datetime.now(TZ).date()
    if not is_jp_market_business_day(today):
        log.info("本日は日本の市場休業日と判定したため、ランキング計算をスキップします。")
        return
    log.info("JPX公式ページから最新PDFを取得します。")
    snapshots = fetch_latest_snapshots(limit_dates=3)
    report = calculate_report(snapshots)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = LATEST_REPORT_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(LATEST_REPORT_PATH)
    log.info("ランキング保存完了: 対象日=%s / 対象銘柄=%d", report["source_date"], report["universe_count"])


def format_num(value: Any) -> str:
    if value is None:
        return "—"
    try:
        return f"{int(value):+,}"
    except (ValueError, TypeError):
        return "—"


def format_pct(value: Any) -> str:
    if value is None:
        return "—"
    return f"{float(value):+.1f}%"


def display_ratio(rec: dict[str, Any]) -> str:
    old = rec.get("previous_ratio")
    new = rec.get("current_ratio")
    if old is None or new is None:
        return "倍率 —"
    return f"倍率 {old:.2f}→{new:.2f}倍"


def format_ranking(title: str, rows: list[dict[str, Any]]) -> str:
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
