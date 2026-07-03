"""
AI分析コメント生成スクリプト（GitHub Models 連携）

generate_monthly.py から `--with-ai` 経由で呼ばれることを想定。
ローカルで単体テストもできる:
    python scripts/ai_comment.py --input data/latest.json

GITHUB_TOKEN 環境変数が必要:
- GitHub Actions では `${{ secrets.GITHUB_TOKEN }}` で自動付与される
- ローカル実行時は GitHub Personal Access Token（models:read 権限）を export しておく

設計書 §5「AIコメントの立て付け（4部構成・300字±50字）」に準拠。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import requests

# GitHub Models（OpenAI互換エンドポイント）
# v1.5: 旧 models.inference.ai.azure.com は非推奨のため新APIへ移行。モデルIDは publisher 付き。
MODELS_API_URL = "https://models.github.ai/inference/chat/completions"
MODEL_ID = "openai/gpt-4o-mini"

REQUEST_TIMEOUT_SEC = 60


def format_pct_with_direction(pct: float | None, compare_label: str) -> str:
    """前月比/前年比を「+12.3%（前月より増加）」のように符号と向きを明示して整形する。

    符号なしの生数値だけを渡すと、AIが増減の向きを取り違えることがある
    （2026-06 レポートで +98.9% の増収を「微減」と誤記した実例あり）。
    """
    if pct is None:
        return f"—（{compare_label}データなし）"
    if pct > 0:
        return f"+{pct}%（{compare_label}より増加）"
    if pct < 0:
        return f"{pct}%（{compare_label}より減少）"
    return f"±0%（{compare_label}と同水準）"


def build_prompt(payload: dict) -> str:
    """設計書 §5 のプロンプト骨子に従ってユーザープロンプトを組み立てる。"""
    summary = payload.get("summary", {})
    total_sales = summary.get("totalSales", 0)
    order_count = summary.get("orderCount", 0)
    avg = summary.get("averageOrderValue", 0)
    mom = summary.get("monthOverMonthPct")
    mom_text = format_pct_with_direction(mom, "前月")

    top5 = payload.get("productRanking", [])[:5]
    if top5:
        top5_text = "\n".join(
            f"  {p['rank']}位: {p['name']} ({p['quantity']}個・¥{p['sales']:,}・構成比{p['sharePct']}%)"
            for p in top5
        )
    else:
        top5_text = "  （販売実績なし）"

    daily_sales = payload.get("dailySales", [])
    peak_day = max(daily_sales, key=lambda d: d["sales"], default=None)
    peak_text = (
        f"{peak_day['day']}日（¥{peak_day['sales']:,}）"
        if peak_day and peak_day["sales"] > 0
        else "突出した日なし"
    )
    sales_days = sum(1 for d in daily_sales if d["sales"] > 0)

    return (
        f"あなたは森田醤油醸造元（家業の醤油蔵）のEC売上を毎月分析するアナリストです。\n"
        f"以下のデータをもとに、家業オーナー向けの月次振り返りコメントを書いてください。\n\n"
        f"【月】{payload.get('monthLabel', '')}\n"
        f"【売上総額】¥{total_sales:,}\n"
        f"【注文件数】{order_count}件\n"
        f"【平均単価】¥{avg:,}\n"
        f"【前月比】{mom_text}\n"
        f"【売上があった日数】{sales_days}日\n"
        f"【ピーク日】{peak_text}\n"
        f"【商品ランキングTOP5】\n{top5_text}\n\n"
        "## 出力ルール\n"
        "- 文字数: 300字 ±50字（必ず守る）\n"
        "- 構成: 以下の4部を順に書く（小見出しは付けず、1つの自然な文章に繋げる）\n"
        "  1. 今月の振り返り（事実）約80字 — 売上・注文件数・前月比・トップ商品\n"
        "  2. 好調だった点（考察）約60字 — 何が売上に貢献したかの分析\n"
        "  3. 課題・反省点 約60字 — 伸び悩んだ点や改善余地\n"
        "  4. 来月への提案 約100字 — 具体施策を1〜2個\n"
        "- トーン: 穏やかだが前向き、家業への敬意を感じさせる\n"
        "- 過度に楽観/悲観な表現や、根拠のない予測は避ける\n"
        "- 注文件数が極端に少ない月でも、励ましつつ建設的な提案を入れる\n"
    )


def build_fiscal_prompt(payload: dict) -> str:
    """年次（年度）コメント用プロンプト。設計書 §5「年次コメントの立て付け」に準拠。"""
    summary = payload.get("summary", {})
    total_sales = summary.get("totalSales", 0)
    order_count = summary.get("orderCount", 0)
    avg = summary.get("averageOrderValue", 0)
    yoy = summary.get("yearOverYearPct")
    yoy_text = (
        format_pct_with_direction(yoy, "前年度")
        if yoy is not None
        else "—（前年データなし・初年度）"
    )

    monthly = payload.get("monthlySales", [])
    monthly_text = "\n".join(
        f"  {m['monthLabel']}: ¥{m['sales']:,}" for m in monthly
    ) if monthly else "  （データなし）"

    top5 = payload.get("productRanking", [])[:5]
    if top5:
        top5_text = "\n".join(
            f"  {p['rank']}位: {p['name']} ({p['quantity']}個・¥{p['sales']:,}・構成比{p['sharePct']}%)"
            for p in top5
        )
    else:
        top5_text = "  （販売実績なし）"

    sales_months = sum(1 for m in monthly if m["sales"] > 0)
    peak = max(monthly, key=lambda m: m["sales"], default=None)
    peak_text = (
        f"{peak['monthLabel']}（¥{peak['sales']:,}）"
        if peak and peak["sales"] > 0 else "突出した月なし"
    )

    return (
        "あなたは森田醤油醸造元（家業の醤油蔵）のEC売上を年次で振り返るアナリストです。\n"
        "以下のデータをもとに、家業オーナー向けの年度総括コメントを書いてください。\n\n"
        f"【年度】{payload.get('fiscalLabel', '')}（{payload.get('period', {}).get('start', '')}〜{payload.get('period', {}).get('end', '')}）\n"
        f"【年間売上総額】¥{total_sales:,}\n"
        f"【年間注文件数】{order_count}件\n"
        f"【平均単価】¥{avg:,}\n"
        f"【前年同期比】{yoy_text}\n"
        f"【売上があった月数】{sales_months}/12ヶ月\n"
        f"【ピーク月】{peak_text}\n"
        f"【月別売上推移】\n{monthly_text}\n"
        f"【商品ランキングTOP5（年間）】\n{top5_text}\n\n"
        "## 出力ルール\n"
        "- 文字数: 400字 ±50字（必ず守る）\n"
        "- 構成: 以下の4部を順に書く（小見出しは付けず、自然な文章に繋げる）\n"
        "  1. 年度の総括 約100字 — 年間売上・件数・前年比・トップ商品\n"
        "  2. 季節トレンド・伸びた商品 約80字 — 月別推移から見える季節性、好調カテゴリ\n"
        "  3. 積み残し・課題 約80字 — 年度通して伸び悩んだ点、改善余地、構造的な課題\n"
        "  4. 来年度の方向性 約140字 — 重点施策、新商品検討、季節別キャンペーンの全体設計（具体施策2〜3個）\n"
        "- トーン: 穏やかだが前向き、年度全体を俯瞰した「経営者目線」の語り口\n"
        "- 過度に楽観/悲観な表現や、根拠のない予測は避ける\n"
        "- 売上が少ない初年度でも、励ましつつ建設的な提案を入れる\n"
    )


def _call_models_api(prompt: str, max_tokens: int) -> str:
    """GitHub Models API を共通の形で呼ぶ。"""
    token = os.getenv("GITHUB_TOKEN", "").strip()
    if not token:
        raise RuntimeError(
            "GITHUB_TOKEN が環境変数にありません。\n"
            "  - GitHub Actions では secrets.GITHUB_TOKEN が自動付与されます\n"
            "  - ローカル実行時は models:read 権限を持つ Personal Access Token を export してください"
        )
    response = requests.post(
        MODELS_API_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        json={
            "model": MODEL_ID,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.7,
            "max_tokens": max_tokens,
        },
        timeout=REQUEST_TIMEOUT_SEC,
    )
    if response.status_code != 200:
        raise RuntimeError(
            f"GitHub Models API エラー: HTTP {response.status_code}\n"
            f"レスポンス: {response.text[:500]}"
        )
    data = response.json()
    try:
        return data["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError) as e:
        raise RuntimeError(f"レスポンス形式が想定外です: {data}") from e


def generate_comment(payload: dict) -> str:
    """月次AIコメントを生成して返す。"""
    return _call_models_api(build_prompt(payload), max_tokens=800)


def generate_fiscal_comment(payload: dict) -> str:
    """年次（年度）AIコメントを生成して返す。"""
    return _call_models_api(build_fiscal_prompt(payload), max_tokens=1000)


def main() -> int:
    parser = argparse.ArgumentParser(description="AIコメント生成（単体テスト用）")
    parser.add_argument(
        "--input",
        default="data/latest.json",
        help="入力JSONファイル（デフォルト: data/latest.json）",
    )
    parser.add_argument(
        "--fiscal",
        action="store_true",
        help="年次（年度）コメントを生成する。--input に fiscal-YYYY.json を指定。",
    )
    args = parser.parse_args()

    input_path = Path(args.input)
    if not input_path.exists():
        print(f"エラー: {input_path} が見つかりません。")
        return 1

    payload = json.loads(input_path.read_text(encoding="utf-8"))
    try:
        comment = (
            generate_fiscal_comment(payload) if args.fiscal else generate_comment(payload)
        )
    except Exception as e:
        print(f"失敗: {e}")
        return 1

    print(comment)
    print(f"\n（{len(comment)}字）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
