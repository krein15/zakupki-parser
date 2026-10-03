"""Command line: ``python -m zkparser fetch --region 72 --date 2026-09-30``.

* ``fetch`` searches the site and keeps the XML of every notice found in the local cache.
* ``regions`` lists the regions ``--region`` accepts.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime
from pathlib import Path

from .cache import NoticeCache
from .fetch import FetchReport, fetch_notices
from .regions import REGIONS, resolve_region
from .settings import default_cache_dir
from .website.client import SiteClient
from .website.search import SearchHit, SearchQuery, Stage

DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y")


def parse_day(value: str) -> date:
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            pass
    raise argparse.ArgumentTypeError(f"неверная дата «{value}», нужна ГГГГ-ММ-ДД или ДД.ММ.ГГГГ")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="zkparser", description="Извещения о закупках 44-ФЗ с zakupki.gov.ru")
    parser.add_argument("-v", "--verbose", action="store_true", help="подробный журнал")
    commands = parser.add_subparsers(dest="command", required=True)

    fetch = commands.add_parser("fetch", help="найти закупки и сохранить их XML в кэш")
    fetch.add_argument(
        "-r", "--region", action="append", required=True, metavar="РЕГИОН",
        help="код (72) или часть названия (тюмен); можно указать несколько раз",
    )
    fetch.add_argument("-d", "--date", type=parse_day, help="дата размещения (по умолчанию сегодня)")
    fetch.add_argument("--from", dest="date_from", type=parse_day, metavar="ДАТА", help="начало периода размещения")
    fetch.add_argument("--to", dest="date_to", type=parse_day, metavar="ДАТА", help="конец периода размещения")
    fetch.add_argument("-q", "--query", default="", help="слова для поиска на сайте ЕИС, с учётом словоформ")
    fetch.add_argument("--price-from", type=int, metavar="РУБ", help="начальная цена от")
    fetch.add_argument("--price-to", type=int, metavar="РУБ", help="начальная цена до")
    fetch.add_argument("--all-stages", action="store_true", help="любой этап, а не только «Подача заявок»")
    fetch.add_argument(
        "--cache-dir", type=Path, help="папка кэша (по умолчанию %%LOCALAPPDATA%%\\ZakupkiParser\\cache)"
    )

    commands.add_parser("regions", help="коды регионов для --region")
    return parser


def period(args: argparse.Namespace, parser: argparse.ArgumentParser) -> tuple[date, date]:
    if args.date and (args.date_from or args.date_to):
        parser.error("укажите либо --date, либо --from/--to")
    if args.date:
        return args.date, args.date
    start = args.date_from or args.date_to or date.today()
    end = args.date_to or (date.today() if args.date_from else start)
    if start > end:
        parser.error("начало периода позже конца")
    return start, end


def build_query(args: argparse.Namespace, parser: argparse.ArgumentParser) -> SearchQuery:
    try:
        regions = tuple(dict.fromkeys(resolve_region(value) for value in args.region))
    except ValueError as error:
        parser.error(str(error))
    start, end = period(args, parser)
    return SearchQuery(
        regions=regions,
        published_from=start,
        published_to=end,
        text=args.query.strip(),
        price_from=args.price_from,
        price_to=args.price_to,
        stages=() if args.all_stages else (Stage.APPLICATIONS,),
    )


def describe(query: SearchQuery) -> str:
    regions = [REGIONS[code] for code in query.regions]
    parts = [", ".join(regions) if len(regions) <= 3 else f"{len(regions)} регионов"]
    days = f"{query.published_from:%d.%m.%Y}"
    if query.published_to != query.published_from:
        days += f"–{query.published_to:%d.%m.%Y}"
    parts.append(days)
    if query.text:
        parts.append(f"«{query.text}»")
    if query.price_from is not None or query.price_to is not None:
        parts.append(f"цена {query.price_from or 0}–{query.price_to if query.price_to is not None else '…'} ₽")
    parts.append("подача заявок" if query.stages else "все этапы")
    return " · ".join(parts)


def print_progress(number: int, total: int, hit: SearchHit, status: str) -> None:
    width = len(str(total))
    print(f"[{number:>{width}}/{total}] {hit.reg_number} {status}")


def print_summary(report: FetchReport, client: SiteClient, cache_dir: Path) -> None:
    print(
        f"\nНайдено {report.found}: скачано {report.downloaded}, из кэша {report.cached}, "
        f"ошибок {len(report.failed)}."
    )
    for reg_number, reason in report.failed:
        print(f"  {reg_number}: {reason}")
    if report.truncated:
        print("Сайт отдаёт не больше 5 000 закупок на запрос, часть не попала: сузьте период или регионы.")
    print(f"Запросов к сайту {client.stats.requests}, просьб подождать (429) {client.stats.throttled}.")
    print(f"Кэш: {cache_dir}")
    if report.error:
        print(f"\nЗапуск остановлен: {report.error}")
        print("Скачанное сохранено, повторный запуск продолжит с того же места.")


def run_fetch(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    query = build_query(args, parser)
    cache_dir = args.cache_dir or default_cache_dir()
    client = SiteClient()
    print(f"Поиск: {describe(query)}")
    with NoticeCache(cache_dir) as cache:
        try:
            report = fetch_notices(client, cache, query, progress=print_progress)
        except KeyboardInterrupt:
            print("\nОстановлено. Скачанное сохранено в кэше.")
            return 130
    print_summary(report, client, cache_dir)
    return 1 if report.error else 0


def run_regions() -> int:
    for code, name in sorted(REGIONS.items(), key=lambda item: item[1]):
        print(f"{code[:2]}  {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    for noisy in ("urllib3", "requests"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    if args.command == "regions":
        return run_regions()
    return run_fetch(args, parser)


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
