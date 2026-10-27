import html
import os
import asyncio
import secrets
from datetime import datetime, timedelta, timezone

from aiogram import Router, F
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import Message, CallbackQuery
from aiogram.exceptions import TelegramBadRequest

from database import (
    db_fetch_user, db_save_user, db_update_user, db_all_users,
    db_log, db_recent_logs, db_blacklist, db_blacklist_add,
    db_blacklist_remove,
)
from keyboards import (
    main_menu, settings_menu, level_menu, content_menu,
    complaint_list, complaint_actions, admin_menu, confirm_menu,
)
from parser import (
    check_session, fetch_complaints, InvalidSession, SiteError,
    SiteStructureError, GrndSession,
)
from utils import (
    env_value, encrypt_cookies, decrypt_cookies, parse_settings,
    serialize_settings, clean_text, parse_positive_int, safe_nickname,
)


router = Router()
admin_passwords: dict[int, str] = {}


class UserFlow(StatesGroup):
    waiting_cookies = State()
    waiting_blacklist = State()
    waiting_reminder = State()


class AdminFlow(StatesGroup):
    waiting_password = State()
    waiting_target = State()
    waiting_level = State()
    waiting_broadcast = State()
    waiting_broadcast_confirm = State()


def owner_id() -> int:
    return int(env_value("OWNER_ID"))


def is_owner(tg_id: int) -> bool:
    try:
        return tg_id == owner_id()
    except (RuntimeError, ValueError):
        return False


async def reply_home(message: Message, user: dict) -> None:
    await message.answer(
        f"Добро пожаловать, <b>{html.escape(user['nickname'])}</b>.\n"
        f"Уровень: <b>{user['lvl']}</b>",
        reply_markup=main_menu(),
        parse_mode="HTML",
    )


async def edit_or_answer(callback: CallbackQuery, text: str, markup=None) -> None:
    try:
        await callback.message.edit_text(text, reply_markup=markup)
    except TelegramBadRequest:
        await callback.message.answer(text, reply_markup=markup)


@router.message(Command("start"))
async def start_entry(message: Message, state: FSMContext):
    user = await db_fetch_user(message.from_user.id)
    if user:
        if user["is_banned"] or not user["is_active"]:
            await message.answer("Доступ к боту ограничен.")
            return
        if user["is_paused"]:
            await message.answer("Ваш доступ временно приостановлен.")
            return
        await state.clear()
        await reply_home(message, user)
        return

    await state.set_state(UserFlow.waiting_cookies)
    await message.answer(
        "Пришлите Cookie-строку авторизованной сессии grnd.gg.\n"
        "Не отправляйте пароль от сайта. Cookies дают доступ к вашей сессии."
    )


@router.message(Command("cancel"))
async def cancel_flow(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Действие отменено.")


@router.message(UserFlow.waiting_cookies)
async def accept_cookies(message: Message, state: FSMContext):
    cookies = (message.text or "").strip()
    if len(cookies) < 10 or "\n" in cookies:
        await message.answer("Cookie-строка выглядит некорректно. Пришлите её одной строкой.")
        return

    status = await message.answer("Проверяю сессию сайта…")
    try:
        profile = await check_session(cookies)
    except InvalidSession:
        await status.edit_text("Сессия недействительна или истекла. Получите свежие cookies и попробуйте снова.")
        return
    except (SiteError, SiteStructureError) as exc:
        await status.edit_text(f"Не удалось проверить сайт: {html.escape(str(exc))}")
        return
    except Exception:
        await status.edit_text("Не удалось проверить профиль. Попробуйте позже.")
        return

    if profile["lvl"] < 3:
        await state.clear()
        await status.edit_text(
            "Доступ только с 3 уровня. Если ошибка — писать @Ivanka58"
        )
        return

    try:
        encrypted = encrypt_cookies(cookies)
    except Exception:
        await status.edit_text("Ошибка конфигурации шифрования. Обратитесь к владельцу бота.")
        return

    await db_save_user(
        message.from_user.id,
        profile["nickname"],
        profile["lvl"],
        encrypted,
    )
    await db_log(message.from_user.id, "registration")
    await state.clear()
    user = await db_fetch_user(message.from_user.id)
    await status.delete()
    await reply_home(message, user)


@router.callback_query(F.data == "home")
async def home_callback(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.clear()
    user = await db_fetch_user(callback.from_user.id)
    if not user or not user["is_active"] or user["is_banned"]:
        await callback.message.answer("Нет активного доступа. Используйте /start.")
        return
    await edit_or_answer(
        callback,
        f"Главное меню · {html.escape(user['nickname'])} · lvl {user['lvl']}",
        main_menu(),
    )


@router.callback_query(F.data == "help")
async def help_callback(callback: CallbackQuery):
    await callback.answer()
    await edit_or_answer(
        callback,
        "Помощь\n\n"
        "Активные жалобы — загрузка текущего списка с сайта.\n"
        "Настройки — фильтры, контент и чёрный список.\n"
        "Обновить куки — повторная авторизация.\n"
        "Если сессия истекла, обновите cookies.",
        main_menu(),
    )


@router.callback_query(F.data == "cookies_refresh")
async def refresh_cookies(callback: CallbackQuery, state: FSMContext):
    user = await db_fetch_user(callback.from_user.id)
    if not user or user["is_banned"]:
        await callback.answer("Нет доступа.", show_alert=True)
        return
    await state.set_state(UserFlow.waiting_cookies)
    await callback.answer()
    await callback.message.answer("Пришлите новую Cookie-строку одной строкой.")


async def load_user_complaints(tg_id: int) -> list[dict]:
    user = await db_fetch_user(tg_id)
    if not user:
        raise InvalidSession("Пользователь не найден")
    cookies = decrypt_cookies(user["cookies_enc"])
    return await fetch_complaints(cookies)


@router.callback_query(F.data == "complaints")
async def complaints_callback(callback: CallbackQuery):
    await callback.answer()
    user = await db_fetch_user(callback.from_user.id)
    if not user or user["is_banned"] or not user["is_active"]:
        await callback.message.answer("Нет доступа.")
        return
    try:
        items = await load_user_complaints(callback.from_user.id)
    except ValueError:
        await callback.message.answer("Не удалось расшифровать cookies. Обновите их.")
        return
    except InvalidSession:
        await callback.message.answer("Сессия истекла. Нажмите «Обновить куки».")
        return
    except SiteError as exc:
        await callback.message.answer(f"Ошибка сайта: {html.escape(str(exc))}")
        return
    except Exception:
        await callback.message.answer("Не удалось загрузить жалобы. Попробуйте позже.")
        return

    settings = parse_settings(user["settings_json"])
    blocked = {item.casefold() for item in await db_blacklist(callback.from_user.id)}
    visible = []
    for item in items:
        if item["target"].casefold() in blocked or item["author"].casefold() in blocked:
            continue
        if settings["skip_admins"] and item.get("admin_level") is not None:
            continue
        if item.get("admin_level") in settings["excluded_levels"]:
            continue
        if settings["only_older"] and item.get("admin_level") is not None:
            if item["admin_level"] >= user["lvl"]:
                continue
        visible.append(item)

    if not visible:
        await edit_or_answer(callback, "Активных жалоб по вашим фильтрам нет.", main_menu())
        return

    await edit_or_answer(
        callback,
        f"Найдено жалоб: {len(visible)}. Показаны первые 10.",
        complaint_list(visible),
    )


@router.callback_query(F.data.startswith("complaint:"))
async def complaint_details(callback: CallbackQuery):
    await callback.answer()
    site_id = callback.data.split(":", 1)[1]
    try:
        items = await load_user_complaints(callback.from_user.id)
    except ValueError:
        await callback.message.answer("Ошибка расшифровки cookies. Обновите куки.")
        return
    except InvalidSession:
        await callback.message.answer("Сессия истекла. Обновите куки.")
        return
    except Exception:
        await callback.message.answer("Не удалось загрузить жалобу.")
        return

    item = next((entry for entry in items if entry["site_id"] == site_id), None)
    if not item:
        await callback.message.answer("Жалоба не найдена.")
        return

    await edit_or_answer(
        callback,
        f"<b>Жалоба #{html.escape(site_id)}</b>\n"
        f"От кого: {html.escape(item['author'])}\n"
        f"На кого: {html.escape(item['target'])}\n\n"
        f"{html.escape(item['text'][:2500])}\n\n"
        f"<a href=\"{html.escape(item['url'], quote=True)}\">Открыть на сайте</a>",
        complaint_actions(site_id, item["target"]),
    )


@router.callback_query(F.data.startswith("showtext:"))
async def show_complaint_text(callback: CallbackQuery):
    await callback.answer()
    site_id = callback.data.split(":", 1)[1]
    try:
        items = await load_user_complaints(callback.from_user.id)
        item = next((entry for entry in items if entry["site_id"] == site_id), None)
    except Exception:
        item = None
    if not item:
        await callback.message.answer("Не удалось получить текст жалобы.")
        return
    await callback.message.answer(
        f"<b>Жалоба #{html.escape(site_id)}</b>\n{html.escape(item['text'][:3500])}",
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("showproof:"))
async def show_complaint_proof(callback: CallbackQuery):
    await callback.answer()
    site_id = callback.data.split(":", 1)[1]
    try:
        items = await load_user_complaints(callback.from_user.id)
        item = next((entry for entry in items if entry["site_id"] == site_id), None)
    except Exception:
        item = None
    if not item:
        await callback.message.answer("Не удалось получить доказательства.")
        return
    if not item["evidence"]:
        await callback.message.answer("Ссылки на доказательства не найдены.")
        return
    for url in item["evidence"][:10]:
        await callback.message.answer(
            f"<a href=\"{html.escape(url, quote=True)}\">Открыть доказательство</a>",
            parse_mode="HTML",
        )


@router.callback_query(F.data.startswith("open:"))
async def open_complaint(callback: CallbackQuery):
    await callback.answer()
    site_id = callback.data.split(":", 1)[1]
    url = f"https://grnd.gg/admin/complaints/{site_id}"
    await callback.message.answer(
        f"<a href=\"{url}\">Открыть жалобу #{html.escape(site_id)}</a>",
        parse_mode="HTML",
    )


@router.callback_query(F.data == "settings")
async def settings_callback(callback: CallbackQuery):
    await callback.answer()
    user = await db_fetch_user(callback.from_user.id)
    if not user:
        return
    settings = parse_settings(user["settings_json"])
    await edit_or_answer(callback, "Настройки уведомлений", settings_menu(settings))


@router.callback_query(F.data.startswith("setting:"))
async def setting_toggle(callback: CallbackQuery):
    user = await db_fetch_user(callback.from_user.id)
    if not user:
        await callback.answer("Нет доступа.", show_alert=True)
        return

    settings = parse_settings(user["settings_json"])
    key = callback.data.split(":", 1)[1]

    if key in ("skip_admins", "only_older"):
        settings[key] = not settings[key]
    elif key == "pause":
        settings["notifications_paused"] = not settings["notifications_paused"]
    elif key == "levels":
        await callback.answer()
        await edit_or_answer(callback, "Выберите уровни, жалобы на которые скрывать:", level_menu(settings["excluded_levels"]))
        return
    elif key == "content":
        await callback.answer()
        await edit_or_answer(callback, "Выберите формат уведомлений:", content_menu(settings["content_mode"]))
        return

    await db_update_user(
        callback.from_user.id,
        settings_json=serialize_settings(settings),
    )
    await db_log(callback.from_user.id, f"setting:{key}")
    await callback.answer("Сохранено")
    await edit_or_answer(callback, "Настройки уведомлений", settings_menu(settings))


@router.callback_query(F.data.startswith("level:"))
async def toggle_level(callback: CallbackQuery):
    user = await db_fetch_user(callback.from_user.id)
    if not user:
        await callback.answer("Нет доступа.")
        return
    settings = parse_settings(user["settings_json"])
    level = int(callback.data.split(":", 1)[1])
    selected = set(settings["excluded_levels"])
    if level in selected:
        selected.remove(level)
    else:
        selected.add(level)
    settings["excluded_levels"] = sorted(selected)
    await db_update_user(
        callback.from_user.id,
        settings_json=serialize_settings(settings),
    )
    await callback.answer()
    await edit_or_answer(callback, "Выберите уровни для исключения:", level_menu(settings["excluded_levels"]))


@router.callback_query(F.data == "levels:done")
async def levels_done(callback: CallbackQuery):
    user = await db_fetch_user(callback.from_user.id)
    settings = parse_settings(user["settings_json"]) if user else {}
    await callback.answer("Сохранено")
    await edit_or_answer(callback, "Настройки уведомлений", settings_menu(settings))


@router.callback_query(F.data.startswith("content:"))
async def set_content_mode(callback: CallbackQuery):
    user = await db_fetch_user(callback.from_user.id)
    if not user:
        await callback.answer("Нет доступа.")
        return
    mode = callback.data.split(":", 1)[1]
    if mode not in {"title", "text", "evidence", "all"}:
        await callback.answer("Неизвестный режим.")
        return
    settings = parse_settings(user["settings_json"])
    settings["content_mode"] = mode
    await db_update_user(
        callback.from_user.id,
        settings_json=serialize_settings(settings),
    )
    await callback.answer("Сохранено")
    await edit_or_answer(callback, "Настройки уведомлений", settings_menu(settings))


@router.callback_query(F.data == "blacklist")
async def blacklist_menu(callback: CallbackQuery):
    await callback.answer()
    items = await db_blacklist(callback.from_user.id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    for nickname in items[:40]:
        builder.button(
            text=f"Удалить: {nickname[:35]}",
            callback_data=f"blackdel:{nickname[:60]}",
        )
    builder.button(text="Добавить игрока", callback_data="blackadd_prompt")
    builder.button(text="Назад", callback_data="settings")
    builder.adjust(1)
    await edit_or_answer(
        callback,
        "Чёрный список пуст." if not items else "Игроки в чёрном списке:",
        builder.as_markup(),
    )


@router.callback_query(F.data == "blackadd_prompt")
async def blacklist_add_prompt(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    await state.set_state(UserFlow.waiting_blacklist)
    await callback.message.answer("Введите ник игрока для добавления в чёрный список.")


@router.message(UserFlow.waiting_blacklist)
async def blacklist_add_input(message: Message, state: FSMContext):
    nickname = safe_nickname(message.text or "")
    if not nickname or len(nickname) > 100:
        await message.answer("Некорректный ник.")
        return
    await db_blacklist_add(message.from_user.id, nickname)
    await db_log(message.from_user.id, "blacklist:add")
    await state.clear()
    await message.answer(f"Добавлен в чёрный список: {html.escape(nickname)}")


@router.callback_query(F.data.startswith("blackdel:"))
async def blacklist_remove_item(callback: CallbackQuery):
    nickname = callback.data.split(":", 1)[1]
    await db_blacklist_remove(callback.from_user.id, nickname)
    await callback.answer("Удалено")
    items = await db_blacklist(callback.from_user.id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    for entry in items[:40]:
        builder.button(text=f"Удалить: {entry[:35]}", callback_data=f"blackdel:{entry[:60]}")
    builder.button(text="Добавить игрока", callback_data="blackadd_prompt")
    builder.button(text="Назад", callback_data="settings")
    builder.adjust(1)
    await edit_or_answer(callback, "Чёрный список обновлён.", builder.as_markup())


@router.callback_query(F.data.startswith("blackadd:"))
async def blacklist_add_from_complaint(callback: CallbackQuery):
    nickname = safe_nickname(callback.data.split(":", 1)[1])
    if nickname:
        await db_blacklist_add(callback.from_user.id, nickname)
        await db_log(callback.from_user.id, "blacklist:add_from_complaint")
    await callback.answer("Добавлено в чёрный список", show_alert=True)


@router.callback_query(F.data.startswith("remind:"))
async def reminder_prompt(callback: CallbackQuery, state: FSMContext):
    site_id = callback.data.split(":", 1)[1]
    await state.set_state(UserFlow.waiting_reminder)
    await state.update_data(site_id=site_id)
    await callback.answer()
    await callback.message.answer("Через сколько минут напомнить? Введите число от 1 до 10080.")


@router.message(UserFlow.waiting_reminder)
async def reminder_input(message: Message, state: FSMContext):
    from database import db_add_reminder
    minutes = parse_positive_int(message.text or "")
    if not minutes:
        await message.answer("Введите целое число от 1 до 10080.")
        return
    data = await state.get_data()
    site_id = str(data["site_id"])
    remind_at = (datetime.now(timezone.utc) + timedelta(minutes=minutes)).isoformat()
    await db_add_reminder(
        message.from_user.id,
        site_id,
        remind_at,
        f"Напоминание о жалобе #{site_id}",
    )
    await state.clear()
    await message.answer(f"Напоминание установлено через {minutes} мин.")


@router.message(Command("admin"))
async def admin_entry(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    await state.set_state(AdminFlow.waiting_password)
    await message.answer("Введите пароль администратора.")


@router.message(AdminFlow.waiting_password)
async def admin_password_input(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    expected = env_value("OWNER_PASSWORD")
    supplied = message.text or ""
    if not secrets.compare_digest(supplied, expected):
        await state.clear()
        await message.answer("Неверный пароль.")
        return
    await state.clear()
    await db_log(message.from_user.id, "admin:login")
    await message.answer("Административная панель", reply_markup=admin_menu())


@router.callback_query(F.data.startswith("adm:"))
async def admin_actions(callback: CallbackQuery, state: FSMContext):
    if not is_owner(callback.from_user.id):
        return
    action = callback.data.split(":", 1)[1]
    if action == "close":
        await callback.answer()
        await callback.message.delete()
        return
    if action == "users":
        users = await db_all_users()
        if not users:
            text = "Зарегистрированных пользователей нет."
        else:
            lines = [
                f"{html.escape(u['nickname'])} | {u['tg_id']} | lvl {u['lvl']} | "
                f"{'бан' if u['is_banned'] else 'активен' if u['is_active'] else 'неактивен'} | "
                f"{'пауза' if u['is_paused'] else 'работает'}"
                for u in users[:50]
            ]
            text = "\n".join(lines)
        await callback.answer()
        await callback.message.answer(text, reply_markup=admin_menu())
        return
    if action == "manage":
        await state.set_state(AdminFlow.waiting_target)
        await callback.answer()
        await callback.message.answer("Введите Telegram ID пользователя для управления.")
        return
    if action == "broadcast":
        await state.set_state(AdminFlow.waiting_broadcast)
        await callback.answer()
        await callback.message.answer("Введите текст рассылки.")
        return
    if action == "logs":
        logs = await db_recent_logs()
        text = "\n".join(
            f"{entry['timestamp']} | {entry['tg_id']} | {html.escape(entry['action'])}"
            for entry in logs
        ) or "Логи пусты."
        await callback.answer()
        await callback.message.answer(text[:4000], reply_markup=admin_menu())
        return
    await callback.answer("Неизвестное действие.")


@router.message(AdminFlow.waiting_target)
async def admin_target_input(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    try:
        target_id = int((message.text or "").strip())
    except ValueError:
        await message.answer("Введите корректный Telegram ID.")
        return
    user = await db_fetch_user(target_id)
    if not user:
        await message.answer("Пользователь не найден.")
        return
    await state.update_data(target_id=target_id)
    from aiogram.utils.keyboard import InlineKeyboardBuilder
    builder = InlineKeyboardBuilder()
    for action, label in [
        ("ban", "Бан/разбан"),
        ("pause", "Приостановить/возобновить доступ"),
        ("level", "Изменить уровень"),
        ("inspect", "Просмотр ЧС и настроек"),
    ]:
        builder.button(text=label, callback_data=f"manage:{action}:{target_id}")
    builder.adjust(1)
    await state.clear()
    await message.answer(
        f"Пользователь: {html.escape(user['nickname'])}, lvl {user['lvl']}",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data.startswith("manage:"))
async def admin_manage_user(callback: CallbackQuery, state: FSMContext):
    if not is_owner(callback.from_user.id):
        return
    _, action, raw_id = callback.data.split(":", 2)
    target_id = int(raw_id)
    user = await db_fetch_user(target_id)
    if not user:
        await callback.answer("Пользователь не найден.")
        return
    if action == "ban":
        await db_update_user(target_id, is_banned=1 - int(user["is_banned"]))
        await db_log(callback.from_user.id, f"user:{target_id}:ban_toggle")
        await callback.answer("Статус бана обновлён")
        return
    if action == "pause":
        await db_update_user(target_id, is_paused=1 - int(user["is_paused"]))
        await db_log(callback.from_user.id, f"user:{target_id}:pause_toggle")
        await callback.answer("Доступ обновлён")
        return
    if action == "level":
        await state.set_state(AdminFlow.waiting_level)
        await state.update_data(target_id=target_id)
        await callback.answer()
        await callback.message.answer("Введите новый уровень от 0 до 100.")
        return
    if action == "inspect":
        settings = parse_settings(user["settings_json"])
        blacklist = await db_blacklist(target_id)
        text = (
            f"ID: {target_id}\n"
            f"Ник: {html.escape(user['nickname'])}\n"
            f"lvl: {user['lvl']}\n"
            f"Настройки: {html.escape(str(settings))}\n"
            f"Чёрный список: {html.escape(', '.join(blacklist) or 'пуст')}"
        )
        await callback.answer()
        await callback.message.answer(text[:4000])
        return
    await callback.answer()


@router.message(AdminFlow.waiting_level)
async def admin_level_input(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    level = parse_positive_int(message.text or "", 100)
    if level is None:
        if (message.text or "").strip() != "0":
            await message.answer("Введите уровень от 0 до 100.")
            return
        level = 0
    data = await state.get_data()
    target_id = int(data["target_id"])
    await db_update_user(target_id, lvl=level)
    await db_log(message.from_user.id, f"user:{target_id}:level={level}")
    await state.clear()
    await message.answer(f"Уровень пользователя {target_id} изменён на {level}.")


@router.message(AdminFlow.waiting_broadcast)
async def broadcast_preview(message: Message, state: FSMContext):
    if not is_owner(message.from_user.id):
        return
    text = clean_text(message.text, 3500)
    if not text:
        await message.answer("Текст рассылки не может быть пустым.")
        return
    await state.update_data(broadcast_text=text)
    await state.set_state(AdminFlow.waiting_broadcast_confirm)
    await message.answer(
        f"Предпросмотр рассылки:\n\n{html.escape(text)}",
        reply_markup=confirm_menu("broadcast"),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("broadcast:"))
async def broadcast_confirm(callback: CallbackQuery, state: FSMContext):
    if not is_owner(callback.from_user.id):
        return
    if callback.data.endswith(":no"):
        await state.clear()
        await callback.answer("Отменено")
        await callback.message.answer("Рассылка отменена.", reply_markup=admin_menu())
        return
    data = await state.get_data()
    text = data.get("broadcast_text", "")
    users = await db_all_users()
    sent = 0
    blocked = 0
    failed = 0
    await callback.answer("Рассылка запущена")
    for user in users:
        if not user["is_active"] or user["is_banned"]:
            continue
        try:
            await callback.bot.send_message(user["tg_id"], text)
            sent += 1
        except Exception as exc:
            if "blocked" in str(exc).lower() or "deactivated" in str(exc).lower():
                blocked += 1
            else:
                failed += 1
        await asyncio.sleep(0.04)
    await db_log(callback.from_user.id, f"broadcast:sent={sent}:blocked={blocked}:failed={failed}")
    await state.clear()
    await callback.message.answer(
        f"Рассылка завершена.\nОтправлено: {sent}\nБот заблокирован: {blocked}\nДругие ошибки: {failed}",
        reply_markup=admin_menu(),
    )
