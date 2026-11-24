#!/usr/bin/env python3
"""aiprice.io 每日价格快照 -> CSV 历史 + README。

数据源是本站公开快照 https://aiprice.io/data/radar.json（每小时刷新一次）。
本脚本每天跑一次，把当天快照固化成 CSV，并重写 README 的表格部分，
同时生成当日变动摘要 data/changes/<日期>.md。

只发布聚合口径：不发布店铺名、不发布单条报价标题和链接。
"""
from __future__ import annotations

import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CST = timezone(timedelta(hours=8))

RADAR_URL = os.environ.get("RADAR_URL", "https://aiprice.io/data/radar.json")
SITE = "https://aiprice.io"
CH = "gh-data"  # 归因码：所有外链都带它，aiprice.io 自己按来源分列

PRODUCT_COLS = [
    "date", "snapshot_id", "name", "slug", "platform", "type",
    "offers_total", "in_stock", "stock_rate", "lowest", "cheap5_high", "spread",
]
PLATFORM_COLS = [
    "date", "platform", "products", "offers", "in_stock", "stock_rate",
    "lowest_product", "lowest",
]
TYPE_COLS = ["date", "type", "products", "offers", "in_stock", "stock_rate"]
SUMMARY_COLS = [
    "date", "snapshot_id", "generated_at", "products", "offers", "in_stock",
    "stock_rate", "free_products", "changed",
]


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": "aiprice-data/1.0"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)


def num(v):
    """CSV 里的空值 / None 统一成 None，其余转 float。"""
    if v is None or v == "" or v == "None":
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def money(v: float) -> str:
    if v is None:
        return "—"
    if v == 0:
        return "免费"
    return f"¥{v:,.2f}" if v >= 100 else f"¥{v:g}"


def pct(a: float, b: float) -> str:
    return f"{a / b * 100:.1f}%" if b else "—"


def build_rows(snap: dict) -> tuple[list[dict], list[dict], list[dict]]:
    date = datetime.fromisoformat(
        snap["generated_at"].replace("Z", "+00:00")
    ).astimezone(CST).strftime("%Y-%m-%d")
    sid = snap["snapshot_id"]

    products, platforms, types = [], {}, {}
    for p in snap["products"]:
        prices = sorted(o["price"] for o in p["offers"] if o.get("price") is not None)
        # 没有在售报价的商品，最低价无意义（上游会给 0），一律记空
        cheapest = p["lowest"] if p["offers_total"] else None
        high = prices[-1] if prices else None
        products.append({
            "date": date,
            "snapshot_id": sid,
            "name": p["name"],
            "slug": p["slug"],
            "platform": p["platform"],
            "type": p["type"],
            "offers_total": p["offers_total"],
            "in_stock": p["in_stock"],
            "stock_rate": round(p["in_stock"] / p["offers_total"], 4) if p["offers_total"] else 0,
            "lowest": cheapest,
            "cheap5_high": high,
            "spread": round(high / cheapest, 2) if cheapest and high else None,
        })

        pf = platforms.setdefault(p["platform"], {
            "date": date, "platform": p["platform"], "products": 0,
            "offers": 0, "in_stock": 0, "lowest_product": None, "lowest": None,
        })
        pf["products"] += 1
        pf["offers"] += p["offers_total"]
        pf["in_stock"] += p["in_stock"]
        if cheapest is not None and (pf["lowest"] is None or cheapest < pf["lowest"]):
            pf["lowest"] = cheapest
            pf["lowest_product"] = p["name"]

        tp = types.setdefault(p["type"], {
            "date": date, "type": p["type"], "products": 0, "offers": 0, "in_stock": 0,
        })
        tp["products"] += 1
        tp["offers"] += p["offers_total"]
        tp["in_stock"] += p["in_stock"]

    for row in list(platforms.values()) + list(types.values()):
        row["stock_rate"] = round(row["in_stock"] / row["offers"], 4) if row["offers"] else 0

    products.sort(key=lambda r: (r["lowest"] is None, r["lowest"]))
    platform_rows = sorted(platforms.values(), key=lambda r: -r["offers"])
    type_rows = sorted(types.values(), key=lambda r: -r["offers"])
    return date, products, platform_rows, type_rows


def write_csv(path: Path, cols: list[str], rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def read_csv(path: Path) -> list[dict]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def load_previous_products(date: str) -> tuple[str | None, list[dict]]:
    hist = sorted(DATA.glob("history/products-*.csv"))
    prev = [p for p in hist if p.stem.split("products-")[1] < date]
    if not prev:
        return None, []
    last = prev[-1]
    return last.stem.split("products-")[1], read_csv(last)


def diff(prev_date: str | None, prev: list[dict], cur: list[dict]) -> dict:
    old = {r["slug"]: r for r in prev}
    new = {r["slug"]: r for r in cur}
    moves, stock = [], []
    for slug, row in new.items():
        o = old.get(slug)
        if not o:
            continue
        a, b = num(o["lowest"]), num(row["lowest"])
        if a is None or b is None:
            continue
        if a != b:
            moves.append({
                "name": row["name"], "slug": slug, "from": a, "to": b,
                "pct": (b - a) / a * 100 if a else 0,
            })
        da = int(row["in_stock"]) - int(o["in_stock"])
        if abs(da) >= 20:
            stock.append({
                "name": row["name"], "slug": slug,
                "from": int(o["in_stock"]), "to": int(row["in_stock"]), "d": da,
            })
    drops = sorted([m for m in moves if m["to"] < m["from"]], key=lambda m: m["pct"])[:6]
    rises = sorted([m for m in moves if m["to"] > m["from"]], key=lambda m: -m["pct"])[:6]
    stock.sort(key=lambda s: -abs(s["d"]))
    added = [r for s, r in new.items() if s not in old]
    gone = [r for s, r in old.items() if s not in new]
    return {
        "prev_date": prev_date, "drops": drops, "rises": rises,
        "stock": stock[:6], "added": added, "gone": gone,
        "moves": len(moves),
    }


def render_changes(date: str, d: dict, cur: list[dict]) -> str:
    out = [f"# {date} 价格变动", ""]
    if not d["prev_date"]:
        out += ["首次快照，没有前一日可比数据。从明天起这里会记录每日的最低价与库存变动。", ""]
    else:
        out += [f"对比基准：{d['prev_date']}。当日共有 {d['moves']} 个商品的在售最低价发生变化。", ""]
        out += ["## 降价", ""]
        out += ["| 商品 | 前一日 | 当日 | 幅度 |", "| --- | --- | --- | --- |"] if d["drops"] else ["无。"]
        for m in d["drops"]:
            out.append(f"| [{m['name']}]({SITE}/stock/{m['slug']}/?ch={CH}) | {money(m['from'])} | {money(m['to'])} | {m['pct']:.1f}% |")
        out += ["", "## 涨价", ""]
        out += ["| 商品 | 前一日 | 当日 | 幅度 |", "| --- | --- | --- | --- |"] if d["rises"] else ["无。"]
        for m in d["rises"]:
            out.append(f"| [{m['name']}]({SITE}/stock/{m['slug']}/?ch={CH}) | {money(m['from'])} | {money(m['to'])} | +{m['pct']:.1f}% |")
        out += ["", "## 库存变动（当日有货报价数，变动 ≥20 条）", ""]
        out += ["| 商品 | 前一日 | 当日 | 变化 |", "| --- | --- | --- | --- |"] if d["stock"] else ["无。"]
        for s in d["stock"]:
            out.append(f"| [{s['name']}]({SITE}/stock/{s['slug']}/?ch={CH}) | {s['from']} | {s['to']} | {s['d']:+d} |")
        if d["added"] or d["gone"]:
            out += ["", "## 商品池变化", ""]
            for r in d["added"]:
                out.append(f"- 新增：{r['name']}")
            for r in d["gone"]:
                out.append(f"- 下架：{r['name']}")
    out += ["", "---", "", "口径：最低价只算有货报价；库存指有货报价条数；数据每小时采集一次，本文件取当日固定快照。"]
    return "\n".join(out) + "\n"


def render_readme(snap: dict, date: str, products: list[dict], platforms: list[dict],
                  types: list[dict], d: dict) -> str:
    ts = datetime.fromisoformat(snap["generated_at"].replace("Z", "+00:00")).astimezone(CST)
    offers = sum(p["offers_total"] for p in products)
    stock = sum(p["in_stock"] for p in products)
    free = [p for p in products if p["lowest"] == 0]
    priced = [p for p in products if p["lowest"]]

    L = []
    L.append("# AI 订阅与账号价格数据（aiprice.io）")
    L.append("")
    L.append(f"[aiprice.io]({SITE}/?ch={CH}) 每小时采集一次 AI 相关虚拟商品的在售价格："
             f"ChatGPT、Claude、Gemini、Grok 的成品号、代充、会员车位，以及接码、邮箱、虚拟卡等配套商品。"
             f"本仓库每天把当日快照固化一份 CSV，所以同一口径的历史价格可以直接下载对比。")
    L.append("")
    L.append(f"**数据时间：{ts:%Y-%m-%d %H:%M}（北京时间）** · 快照 `{snap['snapshot_id']}` · "
             f"{len(products)} 个商品 / {offers:,} 条报价 / {stock:,} 条有货")
    L.append("")
    L.append("## 当日概览")
    L.append("")
    L.append("| 指标 | 数值 |")
    L.append("| --- | --- |")
    L.append(f"| 在比价商品 | {len(products)} |")
    L.append(f"| 报价条数 | {offers:,} |")
    L.append(f"| 有货报价 | {stock:,} |")
    L.append(f"| 整体有货率 | {pct(stock, offers)} |")
    L.append(f"| 0 元商品 | {len(free)} |")
    L.append(f"| 最低价中位数（不含 0 元） | {money(sorted(p['lowest'] for p in priced)[len(priced)//2])} |")
    L.append("")
    L.append("## 各平台最低价（当日）")
    L.append("")
    L.append("| 平台 | 商品数 | 报价 | 有货 | 有货率 | 当日最低价 |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for r in platforms:
        low = f"{money(r['lowest'])}（{r['lowest_product']}）" if r["lowest"] is not None else "—"
        L.append(f"| {r['platform']} | {r['products']} | {r['offers']:,} | {r['in_stock']:,} | "
                 f"{r['stock_rate']*100:.1f}% | {low} |")
    L.append("")
    L.append("## 商品类型分布")
    L.append("")
    L.append("| 类型 | 商品数 | 报价 | 有货 | 有货率 |")
    L.append("| --- | --- | --- | --- | --- |")
    for r in types:
        L.append(f"| {r['type']} | {r['products']} | {r['offers']:,} | {r['in_stock']:,} | "
                 f"{r['stock_rate']*100:.1f}% |")
    L.append("")
    L.append("## 当日最低价榜（前 15）")
    L.append("")
    L.append("| 商品 | 平台 | 类型 | 最低价 | 有货率 | 低价带（前 5 条上下限） |")
    L.append("| --- | --- | --- | --- | --- | --- |")
    for p in products[:15]:
        band = f"{money(p['lowest'])} – {money(p['cheap5_high'])}" if p["cheap5_high"] else money(p["lowest"])
        L.append(f"| [{p['name']}]({SITE}/stock/{p['slug']}/?ch={CH}) | {p['platform']} | {p['type']} | "
                 f"{money(p['lowest'])} | {p['stock_rate']*100:.0f}% | {band} |")
    L.append("")
    L.append("## 当天变动")
    L.append("")
    if d["prev_date"]:
        L.append(f"对比 {d['prev_date']}，{d['moves']} 个商品的在售最低价发生变化。"
                 f"完整明细见 [data/changes/{date}.md](data/changes/{date}.md)。")
        L.append("")
        if d["drops"]:
            L.append("**降价**：" + "、".join(
                f"{m['name']} {money(m['from'])}→{money(m['to'])}" for m in d["drops"][:2]))
        if d["rises"]:
            L.append("")
            L.append("**涨价**：" + "、".join(
                f"{m['name']} {money(m['from'])}→{money(m['to'])}" for m in d["rises"][:2]))
        if d["stock"]:
            L.append("")
            L.append("**库存变化**：" + "、".join(
                f"{s['name']} {s['d']:+d}" for s in d["stock"][:3]))
        L.append("")
    else:
        L.append("首次快照，明天起这里会出现每日最低价与库存变动摘要。")
        L.append("")
    L.append("## 数据文件")
    L.append("")
    L.append("| 文件 | 内容 |")
    L.append("| --- | --- |")
    L.append("| [data/latest/products.csv](data/latest/products.csv) | 当日每个商品的报价数、有货率、最低价、低价带上下限 |")
    L.append("| [data/latest/platforms.csv](data/latest/platforms.csv) | 当日按平台聚合 |")
    L.append("| [data/latest/types.csv](data/latest/types.csv) | 当日按商品类型聚合 |")
    L.append("| [data/history/](data/history/) | 每天一份的 products / platforms 快照，文件名带日期，只增不改 |")
    L.append("| [data/history/summary.csv](data/history/summary.csv) | 逐日一行：商品数、报价数、有货率、0 元商品数 |")
    L.append("| [data/changes/](data/changes/) | 每日变动明细（降价、涨价、库存、商品池增删） |")
    L.append("")
    L.append("## 口径说明")
    L.append("")
    L.append("- **最低价**只统计有货报价，缺货的不算；0 元表示商家在做免费赠送，表里标为「免费」。")
    L.append("- **有货率** = 有货报价条数 ÷ 该商品全部在售报价条数。")
    L.append("- **低价带**是当日最便宜的 5 条报价的价格区间，用来判断最低价是孤例还是整片都便宜。")
    L.append("- 仓库只发布聚合口径，不含店铺名、单条报价标题和商家链接；实时逐条报价看站内商品页。")
    L.append("- 采集每小时一次，若上游当次快照被标记为 stale，本仓库跳过该次、保留上一份数据。")
    L.append("")
    L.append("## 常见问题")
    L.append("")
    L.append("**这个仓库和网站是什么关系？** 仓库是 [aiprice.io](%s/?ch=%s) 的每日数据存档，"
             "网站上是小时级实时数据，仓库是为了让人能拿到成批的、可对比的历史。" % (SITE, CH))
    L.append("")
    L.append("**为什么不给店铺名？** 只做价格统计，不做店铺背书；哪家店靠不靠谱需要单独判断，"
             "本站只负责把同一件商品的全网报价摆在一起。")
    L.append("")
    L.append("**最低价能直接当成交价吗？** 不能。最低价往往是限量、无质保或需要自己处理售后的档位，"
             "下单前要看交付方式、质保期和库存状态。")
    L.append("")
    L.append("**数据可以引用吗？** 可以，按 CC BY 4.0 转载，注明来源并链回 aiprice.io。")
    L.append("")
    L.append("## 说明")
    L.append("")
    L.append("本站只做价格比较与导航，不销售任何商品、不参与交易。价格与库存随时变化，"
             "以商家页面实际显示为准。")
    L.append("")
    return "\n".join(L)


def main() -> int:
    snap = fetch(RADAR_URL)
    if snap.get("stale"):
        print("upstream snapshot marked stale, skip", file=sys.stderr)
        return 3
    if len(snap.get("products", [])) < 20:
        print("too few products, skip", file=sys.stderr)
        return 3

    date, products, platforms, types = build_rows(snap)
    prev_date, prev = load_previous_products(date)
    d = diff(prev_date, prev, products)

    write_csv(DATA / "latest/products.csv", PRODUCT_COLS, products)
    write_csv(DATA / "latest/platforms.csv", PLATFORM_COLS, platforms)
    write_csv(DATA / "latest/types.csv", TYPE_COLS, types)
    write_csv(DATA / f"history/products-{date}.csv", PRODUCT_COLS, products)
    write_csv(DATA / f"history/platforms-{date}.csv", PLATFORM_COLS, platforms)

    # summary.csv：同一天只保留一行（重跑覆盖）
    rows = [r for r in read_csv(DATA / "history/summary.csv") if r["date"] != date]
    rows.append({
        "date": date,
        "snapshot_id": snap["snapshot_id"],
        "generated_at": snap["generated_at"],
        "products": len(products),
        "offers": sum(p["offers_total"] for p in products),
        "in_stock": sum(p["in_stock"] for p in products),
        "stock_rate": round(sum(p["in_stock"] for p in products) /
                            max(sum(p["offers_total"] for p in products), 1), 4),
        "free_products": sum(1 for p in products if p["lowest"] == 0),
        "changed": d["moves"],
    })
    rows.sort(key=lambda r: r["date"])
    write_csv(DATA / "history/summary.csv", SUMMARY_COLS, rows)

    (DATA / "changes").mkdir(parents=True, exist_ok=True)
    (DATA / f"changes/{date}.md").write_text(render_changes(date, d, products), encoding="utf-8")
    (ROOT / "README.md").write_text(
        render_readme(snap, date, products, platforms, types, d), encoding="utf-8")

    print(f"{date} products={len(products)} offers={rows[-1]['offers']} "
          f"in_stock={rows[-1]['in_stock']} changed={d['moves']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
