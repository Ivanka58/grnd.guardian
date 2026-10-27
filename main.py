import asyncio
import html
import logging
import os
from datetime import datetime, timedelta, timezone

import aiohttp
from aiohttp import web
from aiogram import Bot, Dispatcher
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from database import (
    db_initialize, db_all_users, db_fetch_user, db_update_user,
    db_complaint_seen, db_mark_complaint, db_log,
    db_due_reminders, db_mark_reminder_delivered,
)
from handlers import router
from keyboards import complaint_actions, main_menu
from parser import (
    check_session, fetch_complaints, InvalidSession,
    SiteError, SiteStructureError,
)
from utils import (
    env_value, decrypt_cookies, parse_settings, serialize_settings,
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("grnd_monitor")


def build_notice(item: dict, settings: dict) -> str:
    mode = settings.get("content_mode", "all")
    title = (
        f"<b>Новая жалоба #{html.escape(item['site_id'])}</b>\n"
        f"От кого: {html.escape(item['author'])}\n"
        f"На кого: {html.escape(item['target'])}"
    )
    body = html.escape(item["text"][:2500])
    evidence = "\n".join(
        f'<a href="{html.escape(url, quote=True)}">Доказательство {index}</a>'
        for index, url in enumerate(item.get("evidence", [])[:5], 1)
    )
    if mode == "title":
        return title
    if mode == "text":
        return body or title
    if mode == "evidence":
        return f"{title}\n\n{body}\n\n{evidence or 'Доказательства отсутствуют'}"
    return (
        f"{title}\n\n{body}\n\n"
        f"{evidence or 'Доказательства отсутствуют'}\n\n"
        f'<a href="{html.escape(item["url"], quote=True)}">Открыть на сайте</a>'
    )


def passes_filters(item: dict, user: dict, settings: dict, blacklist: set[str]) -> bool:
    if settings.get("notifications_paused"):
        return False
    if user["is_paused"] or user["is_banned"] or not user["is_active"]:
        return False

    author = item["author"].casefold()
    target = item["target"].casefold()
    if author in blacklist or target in blacklist:
        return False

    level = item.get("admin_level")
    if settings.get("skip_admins") and level is not None:
        return False
    if level in settings.get("excluded_levels", []):
        return False
    if settings.get("only_older") and level is not None and level >= user["lvl"]:
        return False
    return True


async def scan_user(bot: Bot, user: dict, bootstrap: bool = False) -> None:
    tg_id = user["tg_id"]
    try:
        cookies = decrypt_cookies(user["cookies_enc"])
        profile = await check_session(cookies)
        await db_update_user(
            tg_id,
            nickname=profile["nickname"],
            lvl=profile["lvl"],
        )
        items = await fetch_complaints(cookies)
    except ValueError:
        await bot.send_message(
            tg_id,
            "Не удалось расшифровать cookies. Используйте «Обновить куки».",
            reply_markup=main_menu(),
        )
        return
    except InvalidSession:
        await bot.send_message(
            tg_id,
            "Сессия grnd.gg истекла или недействительна. Обновите куки.",
            reply_markup=main_menu(),
        )
        return
    except (SiteError, SiteStructureError) as exc:
        logger.warning("Site scan failed for %s: %s", tg_id, exc)
        return
    except Exception:
        logger.exception("Unexpected scan failure for %s", tg_id)
        return

    settings = parse_settings(user["settings_json"])
    blacklist = {
        name.casefold()
        for name in await __import__("database").db_blacklist(tg_id)
    }

    for item in items:
        already_seen = await db_complaint_seen(item["site_id"], tg_id)
        if not already_seen:
            await db_mark_complaint(item["site_id"], tg_id, False)
            if not bootstrap and passes_filters(item, user, settings, blacklist):
                try:
                    await bot.send_message(
                        tg_id,
                        build_notice(item, settings),
                        reply_markup=complaint_actions(
                            item["site_id"], item["target"]
                        ),
                        disable_web_page_preview=True,
                    )
                    await db_mark_complaint(item["site_id"], tg_id, True)
                    await db_log(tg_id, f"complaint:sent:{item['site_id']}")
                except TelegramForbiddenError:
                    logger.info("User %s blocked the bot", tg_id)
                except TelegramRetryAfter as exc:
                    await asyncio.sleep(exc.retry_after)
                except Exception:
                    logger.exception("Complaint notification failed for %s", tg_id)


async def complaint_watchdog(bot: Bot) -> None:
    bootstrapped: set[int] = set()
    while True:
        try:
            users = await db_all_users()
            for user in users:
                if not user["is_active"] or user["is_banned"]:
                    continue
                await scan_user(
                    bot,
                    user,
                    bootstrap=user["tg_id"] not in bootstrapped,
                )
                bootstrapped.add(user["tg_id"])
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Complaint watcher failed")
        await asyncio.sleep(45)


async def reminder_worker(bot: Bot) -> None:
    while True:
        try:
            now = datetime.now(timezone.utc).isoformat()
            reminders = await db_due_reminders(now)
            for reminder in reminders:
                try:
                    await bot.send_message(
                        reminder["tg_id"],
                        reminder["message"],
                        reply_markup=main_menu(),
                    )
                except TelegramForbiddenError:
                    pass
                except Exception:
                    logger.exception("Reminder delivery failed")
                finally:
                    await db_mark_reminder_delivered(reminder["id"])
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Reminder worker failed")
        await asyncio.sleep(10)


async def daily_audit(bot: Bot) -> None:
    while True:
        now = datetime.now(timezone.utc)
        tomorrow = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        await asyncio.sleep(max(1, (tomorrow - now).total_seconds()))

        try:
            users = await db_all_users()
            for user in users:
                if not user["is_active"] or user["is_banned"]:
                    continue
                try:
                    cookies = decrypt_cookies(user["cookies_enc"])
                    profile = await check_session(cookies)
                    await db_update_user(
                        user["tg_id"],
                        nickname=profile["nickname"],
                        lvl=profile["lvl"],
                    )
                except InvalidSession:
                    await bot.send_message(
                        user["tg_id"],
                        "Сессия grnd.gg истекла. Обновите куки.",
                        reply_markup=main_menu(),
                    )
                except (SiteError, SiteStructureError, ValueError):
                    logger.warning("Daily audit could not verify %s", user["tg_id"])
                except Exception:
                    logger.exception("Daily audit failed for %s", user["tg_id"])
                await asyncio.sleep(0.2)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Daily audit loop failed")


async def health_page(request: web.Request) -> web.Response:
    return web.json_response({
        "status": "ok",
        "service": "grnd-monitor",
        "time": datetime.now(timezone.utc).isoformat(),
    })


async def run_health_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/health", health_page)
    app.router.add_get("/", health_page)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", "10000"))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    return runner


async def main() -> None:
    token = env_value("BOT_TOKEN")
    env_value("OWNER_PASSWORD")
    env_value("FERNET_KEY")
    int(env_value("OWNER_ID"))

    await db_initialize()

    bot = Bot(token=token)
    dispatcher = Dispatcher()
    dispatcher.include_router(router)

    runner = await run_health_server()
    tasks = [
        asyncio.create_task(complaint_watchdog(bot)),
        asyncio.create_task(reminder_worker(bot)),
        asyncio.create_task(daily_audit(bot)),
    ]

    try:
        await dispatcher.start_polling(bot, close_bot_session=False)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await runner.cleanup()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
