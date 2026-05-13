from __future__ import annotations

import argparse
import csv
from collections import Counter, defaultdict
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

import requests

from config import BotConfig


DOMA_EXPLORER_API_URL = "https://explorer.doma.xyz/api/v2"
DOMA_BADGES_CONTRACT = "0x1C674A5F576EA929Efb5de2e1508896eB168437b"
EXPLORER_PAGE_SIZE = 50


@dataclass
class BadgeHolderRow:
    wallet: str
    token_id: str
    balance: int


def _print_section(title: str) -> None:
    print()
    print(title)
    print("-" * len(title))


def _resolve_output_path(raw_path: str) -> Path:
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = Path.cwd() / path
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _build_proxies(cfg: BotConfig) -> Optional[Dict[str, str]]:
    proxy = ""
    if cfg.proxy_candidates:
        proxy = cfg.proxy_candidates[0].strip()
    elif cfg.http_proxy.strip():
        proxy = cfg.http_proxy.strip()
    elif cfg.https_proxy.strip():
        proxy = cfg.https_proxy.strip()
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _get_json(path: str, params: Optional[dict], proxies: Optional[Dict[str, str]]) -> dict:
    resp = requests.get(
        f"{DOMA_EXPLORER_API_URL}/{path.lstrip('/')}",
        params=params or None,
        proxies=proxies,
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _paginate(path: str, base_params: Optional[dict], proxies: Optional[Dict[str, str]]) -> Iterable[dict]:
    params = dict(base_params or {})
    while True:
        data = _get_json(path, params, proxies)
        items = data.get("items") or []
        for item in items:
            yield item
        next_page_params = data.get("next_page_params")
        if not next_page_params:
            break
        params = dict(next_page_params)


def fetch_badge_instances(proxies: Optional[Dict[str, str]]) -> Dict[str, str]:
    names_by_token_id: Dict[str, str] = {}
    for item in _paginate(f"tokens/{DOMA_BADGES_CONTRACT}/instances", None, proxies):
        token_id = str(item.get("id") or "").strip()
        if not token_id:
            continue
        metadata = item.get("metadata") or {}
        name = str(metadata.get("name") or item.get("title") or token_id).strip()
        names_by_token_id[token_id] = name
    return names_by_token_id


def fetch_badge_holders(proxies: Optional[Dict[str, str]]) -> List[BadgeHolderRow]:
    rows: List[BadgeHolderRow] = []
    for item in _paginate(
        f"tokens/{DOMA_BADGES_CONTRACT}/holders",
        {"items_count": EXPLORER_PAGE_SIZE},
        proxies,
    ):
        address = ((item.get("address") or {}).get("hash") or "").strip()
        token_id = str(item.get("token_id") or "").strip()
        raw_value = str(item.get("value") or "0").strip()
        if not address or not token_id:
            continue
        try:
            balance = int(raw_value)
        except ValueError:
            continue
        if balance <= 0:
            continue
        rows.append(BadgeHolderRow(wallet=address.lower(), token_id=token_id, balance=balance))
    return rows


def filter_wallets(
    wallet_totals: Dict[str, int],
    min_badges: int,
    limit_wallets: Optional[int],
) -> List[Tuple[str, int]]:
    filtered_wallets = [
        (wallet, count)
        for wallet, count in sorted(wallet_totals.items(), key=lambda x: (-x[1], x[0]))
        if count >= min_badges
    ]
    if limit_wallets is not None:
        filtered_wallets = filtered_wallets[:limit_wallets]
    return filtered_wallets


def group_wallets_by_badge_count(
    filtered_wallets: List[Tuple[str, int]],
) -> List[Tuple[int, List[Tuple[str, int]]]]:
    grouped: Dict[int, List[Tuple[str, int]]] = defaultdict(list)
    for wallet, count in filtered_wallets:
        grouped[count].append((wallet, count))
    return sorted(grouped.items(), key=lambda x: -x[0])


def print_wallet_details(
    filtered_wallets: List[Tuple[str, int]],
    wallet_badges: Dict[str, Counter[str]],
) -> None:
    _print_section("Wallet details")
    print(f"Shown wallets: {len(filtered_wallets)}")

    for index, (wallet, count) in enumerate(filtered_wallets, start=1):
        print()
        print(f"{index:>4}. {wallet}")
        print(f"      Total badges: {count}")
        for badge_name, qty in sorted(wallet_badges[wallet].items(), key=lambda x: (-x[1], x[0].lower())):
            print(f"      - {badge_name} x{qty}")


def export_csv(
    path: Path,
    filtered_wallets: List[Tuple[str, int]],
    wallet_badges: Dict[str, Counter[str]],
) -> None:
    with path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.writer(fh)
        writer.writerow(["badge_group", "wallet", "total_badges", "badge_name", "badge_quantity"])
        for wallet, total_badges in filtered_wallets:
            for badge_name, qty in sorted(wallet_badges[wallet].items(), key=lambda x: (-x[1], x[0].lower())):
                writer.writerow([total_badges, wallet, total_badges, badge_name, qty])


def export_markdown(
    path: Path,
    filtered_wallets: List[Tuple[str, int]],
    wallet_badges: Dict[str, Counter[str]],
    badge_totals: Counter[str],
    distribution: Counter[int],
    total_wallets: int,
    total_badges: int,
    min_badges: int,
    limit_wallets: Optional[int],
) -> None:
    lines: List[str] = []
    lines.append("# DOMA Badges Report")
    lines.append("")
    lines.append(f"- Badge contract: `{DOMA_BADGES_CONTRACT}`")
    lines.append(f"- Wallets with badges: **{total_wallets}**")
    lines.append(f"- Total badge instances: **{total_badges}**")
    lines.append(f"- Distinct badge types: **{len(badge_totals)}**")
    lines.append(f"- Wallets in report: **{len(filtered_wallets)}**")
    lines.append(f"- Min badges filter: **{min_badges}**")
    lines.append(f"- Wallet limit: **{limit_wallets if limit_wallets is not None else 'none'}**")
    lines.append("")
    lines.append("## Distribution by badge count")
    lines.append("")
    lines.append("| badges | wallets |")
    lines.append("| ---: | ---: |")
    for badge_count in sorted(distribution):
        lines.append(f"| {badge_count} | {distribution[badge_count]} |")
    lines.append("")
    lines.append("## Badge totals")
    lines.append("")
    lines.append("| total | badge |")
    lines.append("| ---: | --- |")
    for badge_name, count in sorted(badge_totals.items(), key=lambda x: (-x[1], x[0].lower())):
        safe_name = badge_name.replace("|", "\\|")
        lines.append(f"| {count} | {safe_name} |")
    lines.append("")
    lines.append("## Wallets grouped by badge count")
    for badge_count, wallets in group_wallets_by_badge_count(filtered_wallets):
        lines.append("")
        lines.append(f"### badges={badge_count}")
        lines.append("")
        lines.append("| wallet | badges | details |")
        lines.append("| --- | ---: | --- |")
        for wallet, total in wallets:
            badge_items: List[str] = []
            for badge_name, qty in sorted(wallet_badges[wallet].items(), key=lambda x: (-x[1], x[0].lower())):
                safe_badge_name = badge_name.replace("|", "\\|")
                badge_items.append(f"{safe_badge_name} x{qty}")
            lines.append(f"| `{wallet}` | {total} | {'<br>'.join(badge_items)} |")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def export_html(
    path: Path,
    filtered_wallets: List[Tuple[str, int]],
    wallet_badges: Dict[str, Counter[str]],
    badge_totals: Counter[str],
    distribution: Counter[int],
    total_wallets: int,
    total_badges: int,
    min_badges: int,
    limit_wallets: Optional[int],
) -> None:
    parts: List[str] = []
    parts.append("<!DOCTYPE html>")
    parts.append("<html lang=\"en\">")
    parts.append("<head>")
    parts.append("<meta charset=\"utf-8\">")
    parts.append("<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">")
    parts.append("<title>DOMA Badges Report</title>")
    parts.append("<style>")
    parts.append("body{font-family:Segoe UI,Arial,sans-serif;margin:24px;background:#f5f7fb;color:#1f2937;}")
    parts.append("h1,h2,h3{margin:0 0 12px 0;} .grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin:16px 0 24px;}")
    parts.append(".card{background:#fff;border:1px solid #dbe3f0;border-radius:12px;padding:16px;box-shadow:0 1px 3px rgba(15,23,42,.06);}")
    parts.append(".muted{color:#667085;font-size:14px;} table{width:100%;border-collapse:collapse;background:#fff;border:1px solid #dbe3f0;border-radius:12px;overflow:hidden;}")
    parts.append("th,td{padding:10px 12px;border-bottom:1px solid #e5e7eb;text-align:left;vertical-align:top;} th{background:#eef4ff;} tr:last-child td{border-bottom:none;}")
    parts.append(".group{margin:24px 0;} .wallet-list{margin:0;padding-left:18px;} code{font-family:Consolas,monospace;font-size:13px;}")
    parts.append("</style>")
    parts.append("</head>")
    parts.append("<body>")
    parts.append("<h1>DOMA Badges Report</h1>")
    parts.append(f"<p class=\"muted\">Badge contract: <code>{escape(DOMA_BADGES_CONTRACT)}</code></p>")
    parts.append("<div class=\"grid\">")
    for label, value in [
        ("Wallets with badges", str(total_wallets)),
        ("Total badge instances", str(total_badges)),
        ("Distinct badge types", str(len(badge_totals))),
        ("Wallets in report", str(len(filtered_wallets))),
        ("Min badges filter", str(min_badges)),
        ("Wallet limit", str(limit_wallets) if limit_wallets is not None else "none"),
    ]:
        parts.append(f"<div class=\"card\"><div class=\"muted\">{escape(label)}</div><div><strong>{escape(value)}</strong></div></div>")
    parts.append("</div>")
    parts.append("<h2>Distribution by badge count</h2>")
    parts.append("<table><thead><tr><th>badges</th><th>wallets</th></tr></thead><tbody>")
    for badge_count in sorted(distribution):
        parts.append(f"<tr><td>{badge_count}</td><td>{distribution[badge_count]}</td></tr>")
    parts.append("</tbody></table>")
    parts.append("<h2 style=\"margin-top:24px;\">Badge totals</h2>")
    parts.append("<table><thead><tr><th>total</th><th>badge</th></tr></thead><tbody>")
    for badge_name, count in sorted(badge_totals.items(), key=lambda x: (-x[1], x[0].lower())):
        parts.append(f"<tr><td>{count}</td><td>{escape(badge_name)}</td></tr>")
    parts.append("</tbody></table>")
    parts.append("<h2 style=\"margin-top:24px;\">Wallets grouped by badge count</h2>")
    for badge_count, wallets in group_wallets_by_badge_count(filtered_wallets):
        parts.append(f"<div class=\"group\"><h3>badges={badge_count}</h3>")
        parts.append("<table><thead><tr><th>#</th><th>wallet</th><th>total badges</th><th>details</th></tr></thead><tbody>")
        for index, (wallet, total) in enumerate(wallets, start=1):
            badge_list = "".join(
                f"<li>{escape(badge_name)} x{qty}</li>"
                for badge_name, qty in sorted(wallet_badges[wallet].items(), key=lambda x: (-x[1], x[0].lower()))
            )
            parts.append(
                f"<tr><td>{index}</td><td><code>{escape(wallet)}</code></td><td>{total}</td><td><ul class=\"wallet-list\">{badge_list}</ul></td></tr>"
            )
        parts.append("</tbody></table></div>")
    parts.append("</body></html>")
    path.write_text("\n".join(parts) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Parse all DOMA badge holders and print wallet/badge distribution."
    )
    parser.add_argument(
        "--show-wallets",
        action="store_true",
        help="Print every wallet with its badge count and badge names.",
    )
    parser.add_argument(
        "--show-badges",
        action="store_true",
        help="Print aggregate counts per badge type.",
    )
    parser.add_argument(
        "--min-badges",
        type=int,
        default=1,
        help="Only show wallets with at least this many badges when using --show-wallets.",
    )
    parser.add_argument(
        "--limit-wallets",
        type=int,
        default=None,
        help="Limit the number of wallets shown when using --show-wallets.",
    )
    parser.add_argument(
        "--export-csv",
        nargs="?",
        const="badge_report.csv",
        default=None,
        help="Export wallet/badge rows to CSV. Optionally pass output path.",
    )
    parser.add_argument(
        "--export-md",
        nargs="?",
        const="badge_report.md",
        default=None,
        help="Export a Markdown report. Optionally pass output path.",
    )
    parser.add_argument(
        "--export-html",
        nargs="?",
        const="badge_report.html",
        default=None,
        help="Export an HTML report grouped by badges=N. Optionally pass output path.",
    )
    args = parser.parse_args()

    cfg = BotConfig()
    proxies = _build_proxies(cfg)

    print(f"Badge contract: {DOMA_BADGES_CONTRACT}")
    print("Loading badge instances...")
    badge_names = fetch_badge_instances(proxies)
    print(f"Loaded badge instances: {len(badge_names)}")

    print("Loading badge holders...")
    holder_rows = fetch_badge_holders(proxies)
    print(f"Loaded holder rows: {len(holder_rows)}")

    wallet_totals: Dict[str, int] = defaultdict(int)
    wallet_badges: Dict[str, Counter[str]] = defaultdict(Counter)
    badge_totals: Counter[str] = Counter()

    for row in holder_rows:
        badge_name = badge_names.get(row.token_id, row.token_id)
        wallet_totals[row.wallet] += row.balance
        wallet_badges[row.wallet][badge_name] += row.balance
        badge_totals[badge_name] += row.balance

    distribution: Counter[int] = Counter(wallet_totals.values())
    total_wallets = len(wallet_totals)
    total_badges = sum(wallet_totals.values())
    min_badges = max(args.min_badges, 1)
    filtered_wallets = filter_wallets(
        wallet_totals=wallet_totals,
        min_badges=min_badges,
        limit_wallets=args.limit_wallets,
    )

    _print_section("Summary")
    print(f"Wallets with badges: {total_wallets}")
    print(f"Total badge instances: {total_badges}")
    print(f"Distinct badge types: {len(badge_totals)}")

    _print_section("Distribution by badge count")
    for badge_count in sorted(distribution):
        wallet_count = distribution[badge_count]
        print(f"{badge_count} badge(s): {wallet_count} wallet(s)")

    if args.show_badges:
        _print_section("Badge totals")
        for badge_name, count in sorted(badge_totals.items(), key=lambda x: (-x[1], x[0].lower())):
            print(f"{count:>6} | {badge_name}")

    if args.show_wallets:
        print_wallet_details(filtered_wallets=filtered_wallets, wallet_badges=wallet_badges)

    if args.export_csv:
        csv_path = _resolve_output_path(args.export_csv)
        export_csv(csv_path, filtered_wallets, wallet_badges)
        print(f"CSV exported: {csv_path}")

    if args.export_md:
        md_path = _resolve_output_path(args.export_md)
        export_markdown(
            path=md_path,
            filtered_wallets=filtered_wallets,
            wallet_badges=wallet_badges,
            badge_totals=badge_totals,
            distribution=distribution,
            total_wallets=total_wallets,
            total_badges=total_badges,
            min_badges=min_badges,
            limit_wallets=args.limit_wallets,
        )
        print(f"Markdown exported: {md_path}")

    if args.export_html:
        html_path = _resolve_output_path(args.export_html)
        export_html(
            path=html_path,
            filtered_wallets=filtered_wallets,
            wallet_badges=wallet_badges,
            badge_totals=badge_totals,
            distribution=distribution,
            total_wallets=total_wallets,
            total_badges=total_badges,
            min_badges=min_badges,
            limit_wallets=args.limit_wallets,
        )
        print(f"HTML exported: {html_path}")


if __name__ == "__main__":
    main()
