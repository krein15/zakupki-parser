"""Command line: ``python -m zkparser fetch --region 72 --date 2026-09-30``.

* ``fetch`` searches the site and keeps the XML of every notice found in the local cache.
* ``match`` picks the notices that fit one or more profiles (TOML files) and says why each one fits.
* ``export`` lists the notices of regions in a price range that still take applications, no keywords needed.
* ``show`` prints a notice as parsed from its XML (downloading it if it is not cached yet).
* ``regions`` lists the regions ``--region`` accepts; ``templates`` lists niche templates and makes profiles of them.
* ``monitor``, ``telegram`` and ``schedule`` run the checks for new notices, see monitor_cli.py.
"""

from __future__ import annotations

import argparse
import logging
import sys
import textwrap
from datetime import date, datetime
from pathlib import Path

from . import monitor_cli
from .cache import NoticeCache
from .config import load_telegram_settings
from .display import days_left, format_match, format_notice, money, price_range, regions_count
from .excel import build_file_name, export_file_name, export_open_notices, export_profile
from .export import LOOKBACK_DAYS, ExportQuery, ExportResult, export_open
from .fetch import CACHED, STOPPED, FetchReport, fetch_notices
from .notice_xml import NoticeFormatError, parse_notice
from .pipeline import ProfileResult, run_profiles
from .profiles import ProfileError, from_template, load_profile, load_templates, save_profile
from .regions import REGIONS, resolve_region
from .settings import default_cache_dir, default_output_dir, profiles_dir
from .website.client import SiteClient, SiteError
from .website.notice import download_notice
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
    add_period(fetch)
    fetch.add_argument("-q", "--query", default="", help="слова для поиска на сайте ЕИС, с учётом словоформ")
    fetch.add_argument("--price-from", type=int, metavar="РУБ", help="начальная цена от")
    fetch.add_argument("--price-to", type=int, metavar="РУБ", help="начальная цена до")
    fetch.add_argument("--all-stages", action="store_true", help="любой этап, а не только «Подача заявок»")
    add_cache_dir(fetch)

    match = commands.add_parser("match", help="отобрать закупки по профилям (словарям ниш или клиентов)")
    match.add_argument("profiles", nargs="+", type=Path, metavar="ПРОФИЛЬ", help="файл профиля .toml")
    add_period(match)
    match.add_argument(
        "--out", type=Path, metavar="ПАПКА", help="куда сохранить Excel (по умолчанию Документы\\Zakupki Parser)"
    )
    match.add_argument("--no-excel", action="store_true", help="только вывод в консоль, без Excel")
    add_cache_dir(match)

    export = commands.add_parser(
        "export", help="открытые закупки регионов в диапазоне цены — без ключевых слов, сразу в Excel"
    )
    export.add_argument(
        "-r", "--region", action="append", required=True, metavar="РЕГИОН",
        help="код (72) или часть названия (тюмен); можно указать несколько раз",
    )
    export.add_argument("--price-from", type=int, metavar="РУБ", help="начальная цена от")
    export.add_argument("--price-to", type=int, metavar="РУБ", help="начальная цена до")
    export.add_argument("-q", "--query", default="", help="необязательно: слова для поиска на сайте ЕИС")
    export.add_argument(
        "--details", action="store_true",
        help="скачать каждое извещение: настоящий заказчик, ИНН, позиции (дольше — запрос на закупку)",
    )
    export.add_argument("--out", type=Path, metavar="ПАПКА", help="куда сохранить Excel")
    export.add_argument("--no-excel", action="store_true", help="только вывод в консоль, без Excel")
    add_cache_dir(export)

    show = commands.add_parser("show", help="показать разобранное извещение")
    show.add_argument("number", help="номер закупки, например 0167200003426008040")
    add_cache_dir(show)

    commands.add_parser("regions", help="коды регионов для --region")

    templates = commands.add_parser(
        "templates", help="шаблоны ниш; с названием и регионом — создать из шаблона профиль"
    )
    templates.add_argument("name", nargs="?", help="название шаблона или его начало, например «канц»")
    templates.add_argument("-r", "--region", action="append", default=[], metavar="РЕГИОН", help="регион профиля")
    templates.add_argument(
        "-o", "--out", type=Path, metavar="ФАЙЛ", help="куда сохранить профиль (по умолчанию в папку profiles)"
    )
    monitor_cli.add_commands(commands, add_cache_dir)
    return parser


def add_period(command: argparse.ArgumentParser) -> None:
    command.add_argument("-d", "--date", type=parse_day, help="дата размещения (по умолчанию сегодня)")
    command.add_argument("--from", dest="date_from", type=parse_day, metavar="ДАТА", help="начало периода размещения")
    command.add_argument("--to", dest="date_to", type=parse_day, metavar="ДАТА", help="конец периода размещения")


def add_cache_dir(command: argparse.ArgumentParser) -> None:
    command.add_argument(
        "--cache-dir", type=Path, help="папка кэша (по умолчанию %%LOCALAPPDATA%%\\ZakupkiParser\\cache)"
    )


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


def format_period(start: date, end: date) -> str:
    return f"{start:%d.%m.%Y}" if start == end else f"{start:%d.%m.%Y}–{end:%d.%m.%Y}"


def describe(query: SearchQuery) -> str:
    regions = [REGIONS[code] for code in query.regions]
    parts = [", ".join(regions) if len(regions) <= 3 else regions_count(len(regions))]
    parts.append(format_period(query.published_from, query.published_to))
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


def print_download(number: int, total: int, hit: SearchHit, status: str) -> None:
    if status != CACHED:  # cached notices take no time: only downloads show progress
        print_progress(number, total, hit, status)


def print_region(region: str, report: FetchReport) -> None:
    failed = f", ошибок {len(report.failed)}" if report.failed else ""
    print(f"{REGIONS[region]}: найдено {report.found}, скачано {report.downloaded}, из кэша {report.cached}{failed}")


def write_reports(results: list[ProfileResult], start: date, end: date, out_dir: Path) -> list[Path]:
    """One Excel file per profile; profiles with the same name get numbered files."""
    generated_at = datetime.now().replace(microsecond=0)
    paths = []
    for result in results:
        name = build_file_name(result.profile, generated_at)
        path = out_dir / name
        number = 2
        while path in paths or path.exists():
            path = out_dir / f"{Path(name).stem} ({number}).xlsx"
            number += 1
        paths.append(export_profile(path, result, start, end, generated_at))
    return paths


def run_match(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    start, end = period(args, parser)
    try:
        profiles = [load_profile(path) for path in args.profiles]
    except ProfileError as error:
        parser.error(str(error))
    client = SiteClient()
    print(f"Профили: {', '.join(profile.name for profile in profiles)} · {format_period(start, end)}")
    with NoticeCache(args.cache_dir or default_cache_dir()) as cache:
        try:
            result = run_profiles(
                client, cache, profiles, start, end, progress=print_download, region_done=print_region
            )
        except ProfileError as error:
            parser.error(str(error))
        except KeyboardInterrupt:
            print("\nОстановлено. Скачанное сохранено в кэше.")
            return 130

    now = datetime.now().astimezone()
    for profile_result in result.profiles:
        print(f"\n«{profile_result.profile.name}»: подошло {len(profile_result.matches)} из {profile_result.checked}")
        for found in profile_result.matches:
            print("\n" + textwrap.indent(format_match(found.notice, found.verdict.reasons, found.stage(now)), "  "))
    for reg_number, reason in result.broken:
        print(f"\nНе удалось разобрать {reg_number}: {reason}")
    if not args.no_excel:
        print()
        for path in write_reports(result.profiles, start, end, args.out or default_output_dir()):
            print(f"Отчёт: {path}")
    print(f"\nЗапросов к сайту {client.stats.requests}, просьб подождать (429) {client.stats.throttled}.")
    if result.error:
        print(f"Скачивание остановлено: {result.error}")
        print("Отобрано из того, что успело скачаться. Повторный запуск продолжит с того же места.")
    return 1 if result.error else 0


MAX_PRINTED = 30


def run_export(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    try:
        regions = tuple(dict.fromkeys(resolve_region(value) for value in args.region))
    except ValueError as error:
        parser.error(str(error))
    if args.price_from is not None and args.price_to is not None and args.price_from > args.price_to:
        parser.error("цена «от» больше цены «до»")
    query = ExportQuery(regions, args.price_from, args.price_to, args.query.strip(), args.details)
    client = SiteClient()
    now = datetime.now().astimezone()
    names = ", ".join(REGIONS[code] for code in regions) if len(regions) <= 3 else regions_count(len(regions))
    conditions = [names, price_range(query.price_from, query.price_to) or "любая цена"]
    if query.text:
        conditions.append(f"«{query.text}»")
    print(f"Открытые закупки: {' · '.join(conditions)} · размещены за {LOOKBACK_DAYS} дней")
    with NoticeCache(args.cache_dir or default_cache_dir()) as cache:
        try:
            result = export_open(client, cache, query, now, listing=print_listing, progress=print_download)
        except KeyboardInterrupt:
            print("\nОстановлено.")
            return 130

    if sys.stdout.isatty():
        print("\r" + " " * 70 + "\r", end="")
    for code, count in result.regions.items():
        cut = " — сайт отдал не всё (больше 5 000), сузьте цену" if count.truncated else ""
        print(f"{REGIONS[code]}: с этапом «Подача заявок» {count.listed}, из них открыто {count.open}{cut}")
    print(f"Открыто {len(result.notices)}, срок подачи уже прошёл у {result.closed} из {result.listed}.\n")
    print_open(result)
    for reg_number, reason in result.broken:
        print(f"Не удалось разобрать {reg_number}: {reason}")
    if not args.no_excel:
        generated_at = now.replace(microsecond=0, tzinfo=None)
        out_dir = args.out or default_output_dir()
        path = export_open_notices(out_dir / export_file_name(result, generated_at), result, generated_at)
        print(f"\nОтчёт: {path}")
    print(f"Запросов к сайту {client.stats.requests}, просьб подождать (429) {client.stats.throttled}.")
    if result.error and result.error != STOPPED:
        print(f"Выгрузка остановлена: {result.error}")
    return 1 if result.error else 0


def print_listing(region: str, collected: int, total: int) -> None:
    """One line updated in place: only in a terminal, a log file would get a line per page."""
    if sys.stdout.isatty():
        print(f"\r{REGIONS[region][:40]}: собрано {collected} из {total}   ", end="", flush=True)


def print_open(result: ExportResult) -> None:
    for item in result.notices[:MAX_PRINTED]:
        hit = item.hit
        deadline = f"до {hit.deadline:%d.%m.%Y}" if hit.deadline else "срок не указан"
        days = item.days_left(result.today)
        left = f" ({days_left(days)})" if days is not None else ""
        title = hit.title if len(hit.title) <= 90 else hit.title[:89] + "…"
        print(f"  {hit.reg_number} · {money(hit.price)} · {deadline}{left}\n    {title}")
    if len(result.notices) > MAX_PRINTED:
        print(f"  …и ещё {len(result.notices) - MAX_PRINTED} — в отчёте Excel")


def run_show(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    number = args.number.strip().lstrip("№").strip()
    if not number.isdigit():
        parser.error(f"номер закупки состоит из цифр: «{args.number}»")
    with NoticeCache(args.cache_dir or default_cache_dir()) as cache:
        xml = cache.load(number)
        if xml is None:
            try:
                xml = download_notice(SiteClient(), number)
            except SiteError as error:
                print(error, file=sys.stderr)
                return 1
            cache.store(SearchHit(reg_number=number, url=""), xml)
    try:
        print(format_notice(parse_notice(xml)))
    except NoticeFormatError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def run_templates(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    templates = load_templates()
    if not args.name:
        for template in templates:
            print(f"{template.name}: {', '.join(template.keywords[:3])}… · ОКПД2 {', '.join(template.okpd2)}")
        return 0
    needle = args.name.casefold()
    found = [template for template in templates if template.name.casefold().startswith(needle)]
    if len(found) != 1:
        parser.error(f"шаблон «{args.name}» не найден или неоднозначен; список: python -m zkparser templates")
    if not args.region:
        parser.error("укажите регион профиля: --region 72")
    try:
        regions = tuple(dict.fromkeys(resolve_region(value) for value in args.region))
    except ValueError as error:
        parser.error(str(error))
    profile = from_template(found[0], regions)
    path = args.out or profiles_dir() / f"{found[0].name}.toml"
    if path.exists():
        parser.error(f"файл {path} уже есть — укажите другой: --out")
    save_profile(profile, path)
    print(f"Профиль «{profile.name}» сохранён: {path}")
    return 0


def run_regions() -> int:
    for code, name in sorted(REGIONS.items(), key=lambda item: item[1]):
        print(f"{code[:2]}  {name}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(message)s")
    for noisy in ("urllib3", "requests", "pymorphy3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    monitor_cli.protect_logs(load_telegram_settings().token)
    if args.command in ("monitor", "telegram", "schedule"):
        return monitor_cli.run(args, parser)
    if args.command == "templates":
        return run_templates(args, parser)
    if args.command == "regions":
        return run_regions()
    if args.command == "show":
        return run_show(args, parser)
    if args.command == "match":
        return run_match(args, parser)
    if args.command == "export":
        return run_export(args, parser)
    return run_fetch(args, parser)


def capture_console() -> None:
    """pythonw.exe (Task Scheduler) has no console and drops output and tracebacks: keep them in a file instead."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    from .settings import app_data_dir

    path = app_data_dir() / "logs" / "console.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    stream = path.open("a", encoding="utf-8", buffering=1)
    stream.write(f"\n--- {datetime.now():%Y-%m-%d %H:%M:%S} {' '.join(sys.argv)}\n")
    sys.stdout = sys.stdout or stream
    sys.stderr = sys.stderr or stream


if __name__ == "__main__":
    capture_console()
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    sys.exit(main())
