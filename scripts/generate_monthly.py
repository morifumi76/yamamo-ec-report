"""
月次集計スクリプト

data/daily/YYYY-MM-*.json を集計し、設計書 v1.4 §4 の archive/YYYY-MM.json 構造に整形して
data/archive/YYYY-MM.json と（必要なら）data/latest.json に書き出す。

使い方:
    python scripts/generate_monthly.py                        # 前月分（デフォルト・月締め用）
    python scripts/generate_monthly.py --month 2026-04
    python scripts/generate_monthly.py --month 2026-04 --with-ai      # AIコメント込み
    python scripts/generate_monthly.py --month 2026-04 --force        # archive上書き
    python scripts/generate_monthly.py --month 2026-04 --no-latest    # latest.json更新せず
    python scripts/generate_monthly.py --month 2026-05 --force --no-ai  # 当月途中の集計

当月（=今日と同じ月）を対象にした場合は inProgress=true として書き出す。
inProgress=true のときは aiComment は空のままにする（AIコメントは月締め後のみ）。
"""

from __future__ import annotations

import argparse
import calendar
import json
import math
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

# JST固定（設計書 §6）
JST = timezone(timedelta(hours=9))

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DAILY_DIR = PROJECT_ROOT / "data" / "daily"
ARCHIVE_DIR = PROJECT_ROOT / "data" / "archive"
LATEST_PATH = PROJECT_ROOT / "data" / "latest.json"
MONTHS_INDEX_PATH = PROJECT_ROOT / "data" / "months.json"

# Y軸スケール自動計算の切り上げ単位（設計書 §4）
LEFT_AXIS_UNIT = 5000      # 月次：日別売上（円）
RIGHT_AXIS_UNIT = 50000    # 月次：累積売上（円）
LEFT_AXIS_MIN = 5000
RIGHT_AXIS_MIN = 50000

# 年次グラフ用（月別）
FISCAL_LEFT_AXIS_UNIT = 50000     # 年次：月別売上
FISCAL_RIGHT_AXIS_UNIT = 500000   # 年次：年累積売上
FISCAL_LEFT_AXIS_MIN = 50000
FISCAL_RIGHT_AXIS_MIN = 500000

# 会計年度の開始月（4月始まり）
FISCAL_START_MONTH = 4
# 会計年度の最終月（=3月）。この月の確定時に年次AIコメントを生成する（v1.5）
FISCAL_FINAL_MONTH = FISCAL_START_MONTH - 1 if FISCAL_START_MONTH > 1 else 12


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="月次集計＋latest.json生成")
    parser.add_argument(
        "--month",
        type=str,
        default=None,
        help="対象月 YYYY-MM。省略時は前月（JST）。",
    )
    parser.add_argument(
        "--with-ai",
        action="store_true",
        help="AIコメントを生成して aiComment に埋め込む（GITHUB_TOKEN 必要）。",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="data/archive/YYYY-MM.json を上書きする。",
    )
    parser.add_argument(
        "--no-latest",
        action="store_true",
        help="data/latest.json を更新しない（archiveのみ書き出し）。",
    )
    parser.add_argument(
        "--no-ai",
        action="store_true",
        help="--with-ai より優先で AIコメント生成をスキップ。当月の途中集計向け。",
    )
    return parser.parse_args()


def determine_target_month(arg_month: str | None) -> str:
    """対象月（YYYY-MM）を決定する。省略時は JST 基準の前月。"""
    if arg_month:
        # 形式チェック
        datetime.strptime(arg_month, "%Y-%m")
        return arg_month
    today = datetime.now(JST).date()
    first_of_this_month = today.replace(day=1)
    last_of_prev_month = first_of_this_month - timedelta(days=1)
    return last_of_prev_month.strftime("%Y-%m")


def is_current_month(target_month: str) -> bool:
    """target_month が JST の今月（=月途中）かどうか。"""
    today = datetime.now(JST).date()
    return target_month == f"{today.year:04d}-{today.month:02d}"


def compute_days_covered(daily_data: list[dict]) -> int:
    """daily_data 内の最大日付（1-31）を返す。空なら 0。

    単純な len() ではなく max(day) を使うことで、間に欠損日があっても
    「N日まで観測済」という意味で daysCovered を扱える。
    """
    days: list[int] = []
    for d in daily_data:
        try:
            days.append(int(d["date"].split("-")[2]))
        except (KeyError, ValueError, IndexError):
            continue
    return max(days, default=0)


def load_daily_files(target_month: str) -> list[dict]:
    """data/daily/YYYY-MM-*.json を全件読み込む。空ファイルでも含める。"""
    files = sorted(DAILY_DIR.glob(f"{target_month}-*.json"))
    return [json.loads(f.read_text(encoding="utf-8")) for f in files]


def collect_all_orders(daily_data: list[dict]) -> list[dict]:
    """日次データを月内の全注文配列にフラット化する。"""
    orders: list[dict] = []
    for d in daily_data:
        orders.extend(d.get("orders", []))
    return orders


def compute_summary(
    orders: list[dict],
    target_month: str,
    in_progress: bool,
    days_covered: int,
) -> dict:
    """月次サマリ（4枚カード分）を算出する。

    in_progress=True のときは前月比を「同日数比較」（前月の1〜N日と比較）にする。
    """
    total_sales = sum(o["totalAmount"] for o in orders)
    order_count = len(orders)
    average = round(total_sales / order_count) if order_count else 0
    mom_pct, mom_basis = compute_mom_pct(target_month, total_sales, in_progress, days_covered)
    return {
        "totalSales": total_sales,
        "orderCount": order_count,
        "averageOrderValue": average,
        "monthOverMonthPct": mom_pct,
        "monthOverMonthBasis": mom_basis,
    }


def _prev_year_month(target_month: str) -> tuple[int, int]:
    year, month = map(int, target_month.split("-"))
    if month == 1:
        return year - 1, 12
    return year, month - 1


def _sum_prev_month_first_n_days(target_month: str, n: int) -> int | None:
    """前月の 1〜N日の totalSales を daily/*.json から合算する。

    前月の N日分のうち1ファイルでも欠けていれば None を返す（不公平な比較を避ける）。
    """
    if n <= 0:
        return None
    prev_year, prev_month = _prev_year_month(target_month)
    total = 0
    for day in range(1, n + 1):
        path = DAILY_DIR / f"{prev_year:04d}-{prev_month:02d}-{day:02d}.json"
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return None
        total += int(data.get("totalSales", 0))
    return total


def compute_mom_pct(
    target_month: str,
    this_total: int,
    in_progress: bool,
    days_covered: int,
) -> tuple[float | None, str | None]:
    """前月比% と算出根拠（"full" / "sameDayCount" / None）を返す。

    - in_progress=True: 前月1〜N日 vs 当月1〜N日で比較（sameDayCount）
    - in_progress=False: 前月の archive と当月合計で比較（full）
    - 前月データが無いか合計が0なら None / None
    """
    if in_progress:
        prev_total = _sum_prev_month_first_n_days(target_month, days_covered)
        if not prev_total:
            return None, None
        return round((this_total - prev_total) / prev_total * 100, 1), "sameDayCount"

    prev_year, prev_month = _prev_year_month(target_month)
    prev_path = ARCHIVE_DIR / f"{prev_year:04d}-{prev_month:02d}.json"
    if not prev_path.exists():
        return None, None
    prev_data = json.loads(prev_path.read_text(encoding="utf-8"))
    prev_total = prev_data.get("summary", {}).get("totalSales", 0)
    if not prev_total:
        return None, None
    return round((this_total - prev_total) / prev_total * 100, 1), "full"


def compute_daily_sales(daily_data: list[dict], target_month: str) -> list[dict]:
    """その月の各日について {day, sales} の配列を組み立てる。"""
    year, month = map(int, target_month.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]

    by_day: dict[int, int] = {}
    for d in daily_data:
        try:
            day = int(d["date"].split("-")[2])
        except (KeyError, ValueError, IndexError):
            continue
        by_day[day] = d.get("totalSales", 0)

    return [
        {"day": d, "sales": by_day.get(d, 0)}
        for d in range(1, days_in_month + 1)
    ]


def compute_chart_scale(daily_sales: list[dict], total_sales: int) -> dict:
    """Y軸最大値を自動計算する（設計書 §4 のルール）。"""
    max_daily = max((d["sales"] for d in daily_sales), default=0)
    left_max = max(
        LEFT_AXIS_MIN,
        math.ceil(max_daily * 1.2 / LEFT_AXIS_UNIT) * LEFT_AXIS_UNIT,
    )
    right_max = max(
        RIGHT_AXIS_MIN,
        math.ceil(total_sales * 1.1 / RIGHT_AXIS_UNIT) * RIGHT_AXIS_UNIT,
    )
    return {"leftMax": left_max, "rightMax": right_max}


def compute_product_ranking(orders: list[dict], total_sales: int) -> list[dict]:
    """商品名でグルーピングし、売上降順のランキングを作る。

    sharePct = 商品売上 / 月間売上合計 × 100（小数1桁）。
    送料を含む totalSales が分母になるため、足し合わせは100%未満になり得る。
    """
    by_name: dict[str, dict] = {}
    for o in orders:
        for item in o.get("items", []):
            name = item.get("name") or "(名称不明)"
            qty = int(item.get("quantity") or 0)
            unit_price = int(item.get("unitPrice") or 0)
            sales = qty * unit_price
            if name not in by_name:
                by_name[name] = {"quantity": 0, "sales": 0}
            by_name[name]["quantity"] += qty
            by_name[name]["sales"] += sales

    sorted_items = sorted(by_name.items(), key=lambda kv: kv[1]["sales"], reverse=True)

    ranking: list[dict] = []
    for rank, (name, data) in enumerate(sorted_items, start=1):
        share = (
            round(data["sales"] / total_sales * 100, 1) if total_sales else 0.0
        )
        ranking.append({
            "rank": rank,
            "name": name,
            "quantity": data["quantity"],
            "sales": data["sales"],
            "sharePct": share,
        })
    return ranking


def compute_recent_orders(orders: list[dict], limit: int = 10) -> list[dict]:
    """月内の注文を新しい順に最大 limit 件返す。"""
    sorted_orders = sorted(
        orders,
        key=lambda o: o.get("orderedAt", ""),
        reverse=True,
    )
    result: list[dict] = []
    for o in sorted_orders[:limit]:
        items = o.get("items", []) or []
        # 商品が複数あるときは「先頭商品 ほかN点」と表記
        if len(items) == 1:
            product_name = items[0].get("name", "")
        elif len(items) > 1:
            product_name = f"{items[0].get('name', '')} ほか{len(items) - 1}点"
        else:
            product_name = ""
        total_qty = sum(int(item.get("quantity") or 0) for item in items)

        date_label = ""
        ordered_at = o.get("orderedAt", "")
        if ordered_at:
            try:
                dt = datetime.fromisoformat(ordered_at)
                date_label = f"{dt.month}/{dt.day}"
            except ValueError:
                pass

        result.append({
            "date": date_label,
            "orderNumber": o.get("orderId", ""),
            "productName": product_name,
            "quantity": total_qty,
            "amount": int(o.get("totalAmount") or 0),
            "shippingArea": o.get("shippingArea", ""),
        })
    return result


def format_month_label(target_month: str) -> str:
    year, month = target_month.split("-")
    return f"{year}年{int(month)}月"


def write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def determine_fiscal_year(target_month: str) -> int:
    """対象月から所属する会計年度（4月始まり）を求める。

    例: 2026-04 → 2026年度、2027-03 → 2026年度、2027-04 → 2027年度
    """
    year, month = map(int, target_month.split("-"))
    return year if month >= FISCAL_START_MONTH else year - 1


def fiscal_year_months(fiscal_year: int) -> list[str]:
    """その会計年度に含まれる12ヶ月の YYYY-MM を 4月始まり順で返す。"""
    result: list[str] = []
    for offset in range(12):
        m = FISCAL_START_MONTH + offset
        if m <= 12:
            result.append(f"{fiscal_year:04d}-{m:02d}")
        else:
            result.append(f"{fiscal_year + 1:04d}-{m - 12:02d}")
    return result


def aggregate_fiscal(fiscal_year: int) -> dict:
    """会計年度（4月〜翌3月）のアーカイブを月次archive群から組み立てる。

    存在する月だけ取り込み、無い月は売上0として扱う。
    v1.5: 既存の fiscal-YYYY.json に aiComment があれば引き継ぐ
    （年度途中の再構築で年次AIコメントが消えるのを防ぐ）。
    """
    existing_comment = ""
    existing_path = ARCHIVE_DIR / f"fiscal-{fiscal_year}.json"
    if existing_path.exists():
        try:
            existing_comment = json.loads(
                existing_path.read_text(encoding="utf-8")
            ).get("aiComment", "")
        except json.JSONDecodeError:
            existing_comment = ""
    months_in_fy = fiscal_year_months(fiscal_year)
    monthly_archives: dict[str, dict] = {}
    for ym in months_in_fy:
        path = ARCHIVE_DIR / f"{ym}.json"
        if path.exists():
            monthly_archives[ym] = json.loads(path.read_text(encoding="utf-8"))

    # 月別売上配列（12ヶ月固定）
    monthly_sales: list[dict] = []
    for ym in months_in_fy:
        m = int(ym.split("-")[1])
        sales = monthly_archives.get(ym, {}).get("summary", {}).get("totalSales", 0)
        monthly_sales.append({
            "month": ym,
            "monthLabel": f"{m}月",
            "sales": sales,
        })

    # サマリ
    total_sales = sum(a.get("summary", {}).get("totalSales", 0) for a in monthly_archives.values())
    order_count = sum(a.get("summary", {}).get("orderCount", 0) for a in monthly_archives.values())
    avg = round(total_sales / order_count) if order_count else 0

    # 前年同期比（前年度のfiscalアーカイブが既にあれば計算）
    yoy_pct = None
    prev_fiscal_path = ARCHIVE_DIR / f"fiscal-{fiscal_year - 1}.json"
    if prev_fiscal_path.exists():
        prev_total = json.loads(prev_fiscal_path.read_text(encoding="utf-8")).get(
            "summary", {}
        ).get("totalSales", 0)
        if prev_total:
            yoy_pct = round((total_sales - prev_total) / prev_total * 100, 1)

    # 商品ランキング：12ヶ月の月次productRankingを商品名でマージ
    product_map: dict[str, dict] = {}
    for arc in monthly_archives.values():
        for p in arc.get("productRanking", []):
            name = p["name"]
            if name not in product_map:
                product_map[name] = {"quantity": 0, "sales": 0}
            product_map[name]["quantity"] += int(p.get("quantity", 0))
            product_map[name]["sales"] += int(p.get("sales", 0))

    sorted_products = sorted(product_map.items(), key=lambda kv: kv[1]["sales"], reverse=True)
    product_ranking: list[dict] = []
    for rank, (name, data) in enumerate(sorted_products, start=1):
        share = round(data["sales"] / total_sales * 100, 1) if total_sales else 0.0
        product_ranking.append({
            "rank": rank,
            "name": name,
            "quantity": data["quantity"],
            "sales": data["sales"],
            "sharePct": share,
        })

    # チャートスケール
    max_monthly = max((m["sales"] for m in monthly_sales), default=0)
    left_max = max(
        FISCAL_LEFT_AXIS_MIN,
        math.ceil(max_monthly * 1.2 / FISCAL_LEFT_AXIS_UNIT) * FISCAL_LEFT_AXIS_UNIT,
    )
    right_max = max(
        FISCAL_RIGHT_AXIS_MIN,
        math.ceil(total_sales * 1.1 / FISCAL_RIGHT_AXIS_UNIT) * FISCAL_RIGHT_AXIS_UNIT,
    )

    return {
        "fiscalYear": fiscal_year,
        "fiscalLabel": f"{fiscal_year}年度",
        "period": {
            "start": months_in_fy[0],
            "end": months_in_fy[-1],
        },
        "generatedAt": datetime.now(JST).date().isoformat(),
        "summary": {
            "totalSales": total_sales,
            "orderCount": order_count,
            "averageOrderValue": avg,
            "yearOverYearPct": yoy_pct,
        },
        "monthlySales": monthly_sales,
        "chartScale": {"leftMax": left_max, "rightMax": right_max},
        "productRanking": product_ranking,
        "aiComment": existing_comment,
    }


def rebuild_fiscal_archive(fiscal_year: int) -> Path:
    """fiscal-YYYY.json を再構築する。"""
    payload = aggregate_fiscal(fiscal_year)
    path = ARCHIVE_DIR / f"fiscal-{fiscal_year}.json"
    write_json(path, payload)
    return path


def rebuild_months_index() -> dict:
    """data/archive/ をスキャンして data/months.json を作り直す。

    index.html の月プルダウン＋年度プルダウン用。
    GitHub Pages（静的サイト）ではディレクトリ列挙ができないため、
    利用可能な月＋年度の一覧を別ファイルとして公開する必要がある。

    v1.4: archive 内の inProgress=true な月を inProgressMonth として追記する。
    """
    months: list[str] = []
    fiscals: list[int] = []
    in_progress_month: str | None = None
    for f in ARCHIVE_DIR.glob("*.json"):
        name = f.stem
        if name.startswith("fiscal-"):
            try:
                fiscals.append(int(name.removeprefix("fiscal-")))
            except ValueError:
                continue
        else:
            try:
                datetime.strptime(name, "%Y-%m")
            except ValueError:
                continue
            months.append(name)
            try:
                arc = json.loads(f.read_text(encoding="utf-8"))
                if arc.get("inProgress") is True:
                    # 同時に2つ inProgress があれば新しい方を採用
                    if in_progress_month is None or name > in_progress_month:
                        in_progress_month = name
            except (json.JSONDecodeError, OSError):
                continue
    months.sort()
    fiscals.sort()
    payload = {
        "available": months,
        "latest": months[-1] if months else None,
        "inProgressMonth": in_progress_month,
        "fiscalAvailable": fiscals,
        "fiscalLatest": fiscals[-1] if fiscals else None,
    }
    write_json(MONTHS_INDEX_PATH, payload)
    return payload


def latest_month_on_disk() -> str | None:
    """既存 latest.json の対象月（YYYY-MM）を返す。無ければ None。"""
    if not LATEST_PATH.exists():
        return None
    try:
        return json.loads(LATEST_PATH.read_text(encoding="utf-8")).get("month")
    except json.JSONDecodeError:
        return None


def main() -> int:
    args = parse_args()

    try:
        target_month = determine_target_month(args.month)
    except ValueError:
        print(f"エラー: --month は YYYY-MM 形式で指定してください。受け取り: {args.month}")
        return 1

    daily_data = load_daily_files(target_month)
    if not daily_data:
        print(f"エラー: data/daily/{target_month}-*.json が見つかりません。")
        print("  fetch_daily.py で日次データを取得してください。")
        return 1

    in_progress = is_current_month(target_month)
    days_covered = compute_days_covered(daily_data)
    year, month = map(int, target_month.split("-"))
    days_in_month = calendar.monthrange(year, month)[1]

    print(f"対象月: {target_month}{' (途中経過)' if in_progress else ''}")
    print(f"日次ファイル: {len(daily_data)} 件 / daysCovered={days_covered} / daysInMonth={days_in_month}")

    # 集計
    orders = collect_all_orders(daily_data)
    summary = compute_summary(orders, target_month, in_progress, days_covered)
    daily_sales = compute_daily_sales(daily_data, target_month)
    chart_scale = compute_chart_scale(daily_sales, summary["totalSales"])
    product_ranking = compute_product_ranking(orders, summary["totalSales"])
    recent_orders = compute_recent_orders(orders, limit=10)

    payload = {
        "month": target_month,
        "monthLabel": format_month_label(target_month),
        "generatedAt": datetime.now(JST).date().isoformat(),
        "inProgress": in_progress,
        "daysCovered": days_covered,
        "daysInMonth": days_in_month,
        "summary": summary,
        "dailySales": daily_sales,
        "chartScale": chart_scale,
        "productRanking": product_ranking,
        "recentOrders": recent_orders,
        "aiComment": "",
    }

    # AIコメント（任意）
    # v1.4: 月途中（inProgress=true）または --no-ai 指定時はスキップ
    if args.with_ai and not args.no_ai and not in_progress:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ai_comment import generate_comment  # noqa: E402

        print("\nAIコメントを生成中...")
        try:
            payload["aiComment"] = generate_comment(payload)
            print(f"  → 生成完了（{len(payload['aiComment'])}字）")
        except Exception as e:
            print(f"  → 失敗: {e}")
            print("  → aiComment は空のままにします")
    elif args.with_ai and in_progress:
        print("\n[skip] 月途中（inProgress=true）のため AIコメント生成をスキップします。")
    elif args.with_ai and args.no_ai:
        print("\n[skip] --no-ai 指定により AIコメント生成をスキップします。")

    # archive 書き出し（既存なら --force 必須）
    archive_path = ARCHIVE_DIR / f"{target_month}.json"
    if archive_path.exists() and not args.force:
        print(f"\nエラー: {archive_path} はすでに存在します。--force で上書きしてください。")
        return 1
    write_json(archive_path, payload)

    # latest.json 書き出し
    # v1.4: 月途中の archive は latest.json を上書きしない
    #       （latest.json は「最新の確定月」のスナップショット。月途中は archive 経由で表示する）
    # v1.5: 過去月の再生成（workflow_dispatch等）で latest.json が巻き戻るのを防ぐ。
    #       既存 latest.json の月より古い対象月では更新しない。
    latest_written = False
    if not args.no_latest and not in_progress:
        existing_latest_month = latest_month_on_disk()
        if existing_latest_month and existing_latest_month > target_month:
            print(
                f"\n[skip] latest.json は {existing_latest_month} を指しているため、"
                f"過去月 {target_month} では更新しません（archive のみ更新）。"
            )
        else:
            write_json(LATEST_PATH, payload)
            latest_written = True
    elif in_progress and not args.no_latest:
        print("\n[skip] 月途中のため latest.json は更新しません（archive のみ更新）。")

    # 該当年度の fiscal アーカイブを再構築（年間タブ用）
    fiscal_year = determine_fiscal_year(target_month)
    fiscal_path = rebuild_fiscal_archive(fiscal_year)

    # v1.5: 年度最終月（3月）の確定時は、年次AIコメントを生成して fiscal に埋め込む
    if args.with_ai and not args.no_ai and not in_progress and month == FISCAL_FINAL_MONTH:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ai_comment import generate_fiscal_comment  # noqa: E402

        print("\n年次AIコメントを生成中...")
        fiscal_payload = json.loads(fiscal_path.read_text(encoding="utf-8"))
        try:
            fiscal_payload["aiComment"] = generate_fiscal_comment(fiscal_payload)
            write_json(fiscal_path, fiscal_payload)
            print(f"  → 生成完了（{len(fiscal_payload['aiComment'])}字）")
        except Exception as e:
            print(f"  → 失敗: {e}")
            print("  → fiscal の aiComment は空のままにします")

    # months.json を再構築（プルダウン用インデックス）
    months_index = rebuild_months_index()

    # サマリ表示
    print("\n保存しました:")
    print(f"  {archive_path}{' (inProgress=true)' if in_progress else ''}")
    if latest_written:
        print(f"  {LATEST_PATH}")
    print(f"  {fiscal_path} （{fiscal_year}年度集計）")
    ipm = months_index.get("inProgressMonth")
    ipm_part = f" / inProgressMonth={ipm}" if ipm else ""
    print(
        f"  {MONTHS_INDEX_PATH} "
        f"（月: {len(months_index['available'])}件 / 年度: {len(months_index['fiscalAvailable'])}件{ipm_part}）"
    )
    print(f"  売上合計: ¥{summary['totalSales']:,}")
    print(f"  注文件数: {summary['orderCount']}")
    print(f"  平均単価: ¥{summary['averageOrderValue']:,}")
    mom = summary["monthOverMonthPct"]
    basis = summary["monthOverMonthBasis"]
    basis_label = {"full": "前月フル", "sameDayCount": f"前月1〜{days_covered}日"}.get(basis, "—")
    print(f"  前月比: {f'{mom}% ({basis_label})' if mom is not None else '— (前月データなし)'}")
    print(f"  商品ランキング: {len(product_ranking)} 種")
    print(f"  注文詳細: {len(recent_orders)} 件")
    return 0


if __name__ == "__main__":
    sys.exit(main())
