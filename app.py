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
ZENITH_MARKET_MOVERS_URL = "https://api.zenithcode.net/stable/market-movers"
TURNOVER_TOP_N = 100
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
    # 一部データソースの「7203.T」形式を証券コードに統一する。
    code = re.sub(r"\.(?:T|JP|TSE)$", "", code, flags=re.I)
    # JPX/PDFや市場データで末尾0が付く通常株式コードを4桁に戻す。
    if len(code) == 5 and code.endswith("0") and code[:4].isdigit():
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


def _row_code(row: dict[str, Any]) -> str | None:
    for key in ("code", "symbol", "ticker", "stockCode", "stock_code", "securityCode", "security_code"):
        value = row.get(key)
        if value is not None and str(value).strip():
            return normalize_code(str(value))
    return None


def _row_turnover(row: dict[str, Any]) -> float | None:
    # ZENITH Codeのmarket-moversが返すturnoverを優先。別名にも対応しておく。
    for key in ("turnover", "tradingValue", "trading_value", "turnoverValue", "turnover_value", "tradeValue", "trade_value", "valueJPY", "value"):
        value = row.get(key)
        if value is None:
            continue
        try:
            if isinstance(value, str):
                value = value.replace(",", "").replace("¥", "").replace("円", "").strip()
            number = float(value)
            if math.isfinite(number) and number > 0:
                return number
        except (TypeError, ValueError):
            continue
    return None


def _find_turnover_rows(payload: dict[str, Any]) -> list[dict[str, Any]]:
    """market-movers JSONから売買代金（most active）ランキング配列を抽出する。

    APIのレスポンス形状の差異に備え、既知のキーを優先し、見つからなければ
    銘柄コードとturnoverを持つ配列を探索する。値上がり/値下がり配列は除外する。
    """
    preferred_names = {
        "mostactive", "most_active", "mostactivebyturnover", "most_active_by_turnover",
        "topturnover", "top_turnover", "turnover", "turnoverranking", "turnover_ranking",
        "turnoverrankings", "turnover_rankings", "tradingvalue", "trading_value",
        "tradingvalueranking", "trading_value_ranking", "mosttraded", "most_traded",
        "active", "activebyvalue", "active_by_value",
    }
    excluded_names = {"gainers", "losers", "topgainers", "toplosers", "top_gainers", "top_losers", "winners", "decliners"}
    candidates: list[tuple[int, int, list[dict[str, Any]]]] = []

    def walk(node: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(node, dict):
            for key, value in node.items():
                key_norm = re.sub(r"[^a-z0-9_]", "", str(key).lower())
                if isinstance(value, list):
                    rows = [item for item in value if isinstance(item, dict) and _row_code(item) and _row_turnover(item) is not None]
                    if rows:
                        path_names = {re.sub(r"[^a-z0-9_]", "", part.lower()) for part in (*path, str(key))}
                        if not (path_names & excluded_names):
                            priority = 2 if key_norm in preferred_names else 1
                            candidates.append((priority, len(rows), rows))
                walk(value, (*path, str(key)))
        elif isinstance(node, list):
            for value in node:
                walk(value, path)

    walk(payload)
    if not candidates:
        return []
    candidates.sort(key=lambda item: (item[0], item[1]), reverse=True)
    return candidates[0][2]


def _response_date(payload: dict[str, Any], response: requests.Response) -> str | None:
    value = response.headers.get("X-Data-As-Of") or response.headers.get("X-Data-as-of")
    if not value:
        for key in ("asOfDate", "as_of_date", "dataDate", "data_date", "date", "priceDate"):
            candidate = payload.get(key)
            if candidate:
                value = str(candidate)
                break
    if not value:
        return None
    match = re.search(r"(\d{4}-\d{2}-\d{2})", str(value))
    if not match:
        return None
    return match.group(1)


def fetch_turnover_universe(
    source_date: str,
    eligible_codes: set[str],
    top_n: int = TURNOVER_TOP_N,
) -> dict[str, dict[str, Any]]:
    """無料のZENITH Code APIから当日の売買代金上位銘柄を取得する。

    Freeプランは月5,000トークン。market-moversは1回10トークンなので、
    1営業日1回の利用なら月あたり約220〜230トークンが目安。
    ZENITH_API_KEYはGitHub Secretsから注入する。
    """
    api_key = os.getenv("ZENITH_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError(
            "ZENITH_API_KEY が設定されていません。ZENITH Codeの無料アカウントでAPIキーを発行し、"
            "GitHub Secretsに登録してください。"
        )
    if not eligible_codes:
        raise ValueError("売買代金ランキングと照合する信用残対象銘柄がありません。")

    headers = {"x-api-key": api_key, "User-Agent": USER_AGENT}
    params = {"limit": str(top_n)}
    response = requests.get(ZENITH_MARKET_MOVERS_URL, headers=headers, params=params, timeout=45)
    if response.status_code in (401, 403):
        raise RuntimeError("ZENITH Code APIの認証に失敗しました。ZENITH_API_KEYを確認してください。")
    if response.status_code == 429:
        raise RuntimeError("ZENITH Code Freeプランの利用枠またはレート上限に達しました。利用状況を確認してください。")
    response.raise_for_status()
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError("ZENITH Code APIからJSON以外の応答が返されました。") from exc
    if not isinstance(payload, dict):
        raise RuntimeError("ZENITH Code APIの応答形式が想定と異なります（JSONオブジェクトではありません）。")

    data_date = _response_date(payload, response)
    if data_date and data_date != source_date:
        raise RuntimeError(
            f"売買代金データの日付（{data_date}）とJPX信用残の申込日（{source_date}）が一致しません。"
            "古いランキングを使わないよう計算を中止しました。APIの更新を待って再実行してください。"
        )

    rows = _find_turnover_rows(payload)
    if not rows:
        top_keys = ", ".join(list(payload.keys())[:20])
        raise RuntimeError(
            "ZENITH Code APIの応答から売買代金ランキングを特定できませんでした。"
            f"応答キー: {top_keys}。APIのmarket-movers仕様を確認する必要があります。"
        )
    if len(rows) < top_n:
        raise RuntimeError(
            f"ZENITH Code APIから売買代金ランキングを{len(rows)}銘柄しか取得できませんでした（必要={top_n}）。"
            "上位100銘柄を取得できたことを確認できないため、誤った順位での配信を防いで計算を中止しました。"
        )

    turnover_by_code: dict[str, float] = {}
    for row in rows:
        code = _row_code(row)
        turnover_value = _row_turnover(row)
        if not code or turnover_value is None or code not in eligible_codes:
            continue
        previous = turnover_by_code.get(code)
        if previous is None or turnover_value > previous:
            turnover_by_code[code] = turnover_value

    ordered = sorted(turnover_by_code.items(), key=lambda item: (-item[1], item[0]))[:top_n]
    if len(ordered) < top_n:
        log.warning(
            "売買代金上位%d銘柄のうち、JPX信用残と照合できた銘柄は%d銘柄です。",
            top_n, len(ordered),
        )
    if len(ordered) < 20:
        raise RuntimeError(
            f"売買代金上位{top_n}銘柄とJPX信用残を照合できたのは{len(ordered)}銘柄のみです。"
            "銘柄コードの形式やデータ更新状況を確認してください。"
        )

    universe: dict[str, dict[str, Any]] = {}
    for rank, (code, value) in enumerate(ordered, start=1):
        universe[code] = {"turnover_rank": rank, "turnover_value": value}
    log.info(
        "ZENITH Code売買代金ランキング取得: 日付=%s / APIランキング行=%d / 信用残と照合=%d銘柄",
        data_date or source_date, len(rows), len(universe),
    )
    return universe


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


def calculate_report(
    snapshots: list[dict[str, Any]],
    turnover_universe: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    if not snapshots:
        raise ValueError("スコア計算に必要なデータがありません。")
    if not turnover_universe:
        raise ValueError("売買代金上位銘柄を取得できなかったため、ランキングを作成しません。")

    # 当日売買代金上位100銘柄を、信用残データが存在する普通株式に限定して選定。
    universe_codes = set(turnover_universe.keys())
    scored_snapshots = []
    for snap in snapshots:
        records = snap["records"]
        selected_records = {code: records[code] for code in universe_codes if code in records}
        scored_snapshots.append({
            "date": snap["date"],
            "url": snap["url"],
            "scored": compute_snapshot_score(selected_records),
        })
    current = scored_snapshots[0]
    previous_days = scored_snapshots[:3]
    all_codes = set(current["scored"].keys())
    scored = []

    for code in all_codes:
        rec = dict(current["scored"][code])
        turnover = turnover_universe.get(code, {})
        rec["turnover_rank"] = turnover.get("turnover_rank")
        rec["turnover_value"] = turnover.get("turnover_value")
        trend_parts = []
        for snap in previous_days:
            item = snap["scored"].get(code)
            if item is not None:
                # 当日の売買代金上位ユニバースを3日間共通で使い、時系列の比較を揃える。
                trend_parts.append(item["base_score"] / 0.85)
        trend_score = sum(trend_parts) / len(trend_parts) if trend_parts else 0.0
        final_score = rec["base_score"] + WEIGHT_3DAY_TREND * trend_score
        rec["trend_score"] = round(trend_score, 2)
        rec["score"] = max(-100, min(100, int(round(final_score))))
        rec["source_date"] = current["date"]
        rec["trend_days_used"] = len(trend_parts)
        scored.append(rec)

    # スコア同値時は、買い残の縮小率が大きい順、その次に売買代金順位・コード順。
    scored.sort(key=lambda r: (-r["score"], -(r["buy_change_pct"] or 0), r.get("turnover_rank") or 9999, r["code"]))
    top10 = scored[:10]
    worst10 = sorted(scored, key=lambda r: (r["score"], r.get("turnover_rank") or 9999, r["code"]))[:10]
    return {
        "app_name": "本日の需給状況ニュース",
        "source_name": "JPX 銘柄別信用取引残高 + ZENITH Code 売買代金ランキング",
        "source_page": JPX_PAGE,
        "source_date": current["date"],
        "source_url": current["url"],
        "turnover_source": "ZENITH Code API / market-movers (Free plan)",
        "turnover_date": current["date"],
        "turnover_universe_size": len(turnover_universe),
        "turnover_top_n": TURNOVER_TOP_N,
        "calculated_at": datetime.now(TZ).isoformat(timespec="seconds"),
        "formula_version": "v1.2_zenith_free_turnover_top100",
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
    current_date = snapshots[0]["date"]
    eligible_codes = set(snapshots[0]["records"].keys())
    log.info("%sの売買代金上位%d銘柄をZENITH Code Free APIから取得します。", current_date, TURNOVER_TOP_N)
    turnover_universe = fetch_turnover_universe(current_date, eligible_codes, top_n=TURNOVER_TOP_N)
    report = calculate_report(snapshots, turnover_universe)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    temp = LATEST_REPORT_PATH.with_suffix(".json.tmp")
    temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(LATEST_REPORT_PATH)
    log.info("ランキング保存完了: 対象日=%s / 売買代金上位=%d銘柄 / スコア対象=%d銘柄", report["source_date"], report["turnover_universe_size"], report["universe_count"])


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
        f"対象：売買代金上位{report.get('turnover_top_n', TURNOVER_TOP_N)}銘柄から信用残を照合できた普通株式{report.get('turnover_universe_size', 0)}銘柄",
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
