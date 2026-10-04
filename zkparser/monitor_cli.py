"""Monitoring commands: ``monitor`` (one check), ``telegram`` (the bot and its chats), ``schedule`` (hourly checks).

A monitoring run started by Task Scheduler has no console, so it logs to ``logs/monitor.log`` in the program's data
folder. Every log handler cuts the bot token out of what it writes.
"""

from __future__ import annotations

import argparse
import logging
import sys
from collections.abc import Callable
from datetime import datetime, time
from logging.handlers import RotatingFileHandler
from pathlib import Path

from . import scheduler
from .cache import NoticeCache
from .config import CHAT_KEY, load_telegram_settings, save_env_value
from .fetch import FetchReport
from .monitor import MonitorResult, MonitorState, monitor
from .profiles import ProfileError, load_profile
from .regions import REGIONS
from .scheduler import MOSCOW, SchedulerError
from .settings import app_data_dir, default_cache_dir, default_output_dir
from .telegram import SecretFilter, TelegramBot, TelegramError
from .website.client import SiteClient

log = logging.getLogger("zkparser.monitor")

LOG_FORMAT = "%(asctime)s %(levelname)s %(message)s"
TEST_MESSAGE = "Zakupki Parser на связи: сюда будут приходить новые подходящие закупки."


def parse_time(value: str) -> time:
    try:
        return time.fromisoformat(value.zfill(2) if value.isdigit() else value.zfill(5))
    except ValueError as error:
        raise argparse.ArgumentTypeError(f"неверное время «{value}», нужно ЧЧ:ММ") from error


def add_commands(
    commands: argparse._SubParsersAction, add_cache_dir: Callable[[argparse.ArgumentParser], None]
) -> None:
    check = commands.add_parser("monitor", help="проверить новые закупки по профилям и сообщить в Telegram")
    check.add_argument("profiles", nargs="+", type=Path, metavar="ПРОФИЛЬ", help="файл профиля .toml")
    check.add_argument("--out", type=Path, metavar="ПАПКА", help="куда сохранять отчёт дня")
    check.add_argument("--no-excel", action="store_true", help="без отчёта Excel")
    check.add_argument("--no-telegram", action="store_true", help="без сообщений в Telegram")
    add_cache_dir(check)

    bot = commands.add_parser("telegram", help="проверить бота, узнать номер чата, отправить тестовое сообщение")
    bot.add_argument("--save", metavar="ЧАТ", help="сделать чат чатом по умолчанию (TELEGRAM_CHAT_ID в .env)")
    bot.add_argument("--test", nargs="?", const="", metavar="ЧАТ", help="отправить тестовое сообщение")

    schedule = commands.add_parser("schedule", help="проверки по расписанию через Планировщик Windows")
    actions = schedule.add_subparsers(dest="action", required=True)
    on = actions.add_parser("on", help="включить; с профилями — создать или пересоздать задачу")
    on.add_argument("profiles", nargs="*", type=Path, metavar="ПРОФИЛЬ")
    on.add_argument("--from", dest="start", type=parse_time, default=time(8), metavar="ЧЧ:ММ",
                    help="первая проверка, по Москве (по умолчанию 08:00)")
    on.add_argument("--to", dest="end", type=parse_time, default=time(20), metavar="ЧЧ:ММ",
                    help="последняя проверка, по Москве (по умолчанию 20:00)")
    on.add_argument("--every", type=int, default=60, metavar="МИН", help="интервал в минутах (по умолчанию 60)")
    actions.add_parser("off", help="выключить, сохранив настройки")
    actions.add_parser("status", help="состояние и время следующей проверки")
    actions.add_parser("remove", help="удалить задачу из Планировщика")


def protect_logs(*secrets: str) -> None:
    """Attach the secret filter to every handler of the root logger."""
    secret_filter = SecretFilter(*secrets)
    for handler in logging.getLogger().handlers:
        handler.addFilter(secret_filter)


def log_to_file(*secrets: str) -> Path:
    path = app_data_dir() / "logs" / "monitor.log"
    path.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(path, maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    handler.setFormatter(logging.Formatter(LOG_FORMAT, "%Y-%m-%d %H:%M:%S"))
    handler.addFilter(SecretFilter(*secrets))
    logging.getLogger().addHandler(handler)
    return path


def run(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    if args.command == "monitor":
        return run_monitor(args, parser)
    if args.command == "telegram":
        return run_telegram(args)
    return run_schedule(args)


def run_monitor(args: argparse.Namespace, parser: argparse.ArgumentParser) -> int:
    settings = load_telegram_settings()
    log_path = log_to_file(settings.token)
    try:
        profiles = [load_profile(path) for path in args.profiles]
    except ProfileError as error:
        log.error("%s", error)
        parser.exit(2, f"{error}\n")

    bot = None
    if not args.no_telegram:
        if not settings.token:
            log.warning("В .env нет TELEGRAM_BOT_TOKEN: сообщения не отправляются")
        else:
            try:
                bot = TelegramBot(settings.token)
            except TelegramError as error:
                log.error("%s", error)
        if bot and not settings.chat and any(not profile.telegram_chat for profile in profiles):
            log.warning("Не задан чат по умолчанию: python -m zkparser telegram --save <номер чата>")

    now = datetime.now().astimezone()
    names = ", ".join(profile.name for profile in profiles)
    with (
        NoticeCache(args.cache_dir or default_cache_dir()) as cache,
        MonitorState(app_data_dir() / "monitor.sqlite3") as state,
    ):
        try:
            result = monitor(
                SiteClient(),
                cache,
                state,
                profiles,
                today=now.astimezone(MOSCOW).date(),
                now=now,
                bot=bot,
                default_chat=settings.chat,
                out_dir=None if args.no_excel else args.out or default_output_dir(),
                region_done=_log_region,
            )
        except ProfileError as error:
            log.error("%s", error)
            return 2
        except Exception:
            log.exception("Мониторинг «%s» упал", names)
            raise
    _log_result(result)
    log.info("Журнал: %s", log_path)
    failed = result.run.error or any(outcome.error for outcome in result.outcomes)
    return 1 if failed else 0


def _log_region(region: str, report: FetchReport) -> None:
    log.info(
        "%s: найдено %d, скачано %d, из кэша %d%s",
        REGIONS[region], report.found, report.downloaded, report.cached,
        f", ошибок {len(report.failed)}" if report.failed else "",
    )


def _log_result(result: MonitorResult) -> None:
    period = f"{result.start:%d.%m.%Y}" if result.start == result.end else f"{result.start:%d.%m}–{result.end:%d.%m.%Y}"
    for outcome in result.outcomes:
        expired = f", с истёкшим сроком пропущено {outcome.expired}" if outcome.expired else ""
        log.info(
            "«%s» за %s: подошло %d, новых %d, отправлено в Telegram %d%s",
            outcome.profile.name, period, outcome.matches, outcome.new, outcome.sent, expired,
        )
        if outcome.report:
            log.info("Отчёт: %s", outcome.report)
        if outcome.error:
            log.warning("«%s»: Telegram — %s", outcome.profile.name, outcome.error)
    if result.run.error:
        log.warning("Скачивание остановлено: %s. Следующая проверка продолжит.", result.run.error)


def run_telegram(args: argparse.Namespace) -> int:
    settings = load_telegram_settings()
    protect_logs(settings.token)
    if not settings.token:
        print("В .env нет TELEGRAM_BOT_TOKEN. Создайте бота у @BotFather и впишите его токен в .env.")
        return 1
    try:
        bot = TelegramBot(settings.token)
        print(f"Бот: @{bot.username()}")
        chats = bot.chats()
        if chats:
            print("Писали боту:")
            for chat_id, kind, name in chats:
                print(f"  {chat_id}  {kind}  {name}")
        else:
            print("Боту пока никто не писал: напишите ему любое сообщение из нужного чата и повторите команду.")
        default = settings.chat
        if args.save:
            save_env_value(Path.cwd() / ".env", CHAT_KEY, args.save)
            default = args.save
            print(f"Чат {args.save} записан в .env как чат по умолчанию.")
        print(f"Чат по умолчанию: {default or 'не задан'}")
        if args.test is not None:
            chat = args.test or default or (str(chats[0][0]) if len(chats) == 1 else "")
            if not chat:
                print("Укажите чат: python -m zkparser telegram --test <номер чата>")
                return 1
            bot.send_message(chat, TEST_MESSAGE)
            print(f"Тестовое сообщение отправлено в чат {chat}.")
    except TelegramError as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def run_schedule(args: argparse.Namespace) -> int:
    try:
        if args.action == "on":
            if args.profiles:
                for path in args.profiles:
                    load_profile(path)  # a broken profile should fail now, not at 8 in the morning
                scheduler.create(args.profiles, args.start, args.end, args.every)
                print(f"Задача создана: с {args.start:%H:%M} до {args.end:%H:%M} по Москве, каждые {args.every} мин.")
            else:
                scheduler.set_enabled(True)
        elif args.action == "off":
            scheduler.set_enabled(False)
        elif args.action == "remove":
            scheduler.remove()
            print("Задача удалена.")
            return 0
        print_status(scheduler.status())
    except (SchedulerError, ProfileError) as error:
        print(error, file=sys.stderr)
        return 1
    return 0


def print_status(status: scheduler.TaskStatus) -> None:
    if not status.exists:
        print("Мониторинг по расписанию не настроен: python -m zkparser schedule on <профили>")
        return
    print(f"Мониторинг по расписанию: {'включён' if status.enabled else 'выключен'}")
    if status.enabled and status.next_run:
        print(f"Следующая проверка: {status.next_run} (время компьютера)")
    if status.last_run:
        print(f"Последняя проверка: {status.last_run} — {status.last_result}")
    print(f"Журнал: {app_data_dir() / 'logs' / 'monitor.log'}")
