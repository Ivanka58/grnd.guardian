from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder


def ui_keyboard(rows: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for label, callback in rows:
        builder.button(text=label, callback_data=callback)
    builder.adjust(1)
    return builder.as_markup()


def main_menu() -> InlineKeyboardMarkup:
    return ui_keyboard([
        ("Активные жалобы", "complaints"),
        ("Настройки", "settings"),
        ("Помощь", "help"),
        ("Обновить куки", "cookies_refresh"),
    ])


def settings_menu(settings: dict) -> InlineKeyboardMarkup:
    return ui_keyboard([
        (
            f"Не присылать на админов: {'Вкл' if settings['skip_admins'] else 'Выкл'}",
            "setting:skip_admins",
        ),
        ("Кастомный фильтр уровней", "setting:levels"),
        (
            f"Только старше меня: {'Вкл' if settings['only_older'] else 'Выкл'}",
            "setting:only_older",
        ),
        ("Что присылать сразу?", "setting:content"),
        ("Черный список игроков", "blacklist"),
        (
            "Возобновить уведомления"
            if settings["notifications_paused"]
            else "Приостановить уведомления",
            "setting:pause",
        ),
        ("Назад", "home"),
    ])


def level_menu(selected: list[int]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for level in range(1, 7):
        marker = "☑" if level in selected else "☐"
        builder.button(
            text=f"{marker} {level} lvl",
            callback_data=f"level:{level}",
        )
    builder.button(text="Готово", callback_data="levels:done")
    builder.button(text="Назад", callback_data="settings")
    builder.adjust(2, 2, 2, 1, 1)
    return builder.as_markup()


def content_menu(selected: str) -> InlineKeyboardMarkup:
    options = [
        ("title", "Только заголовок"),
        ("text", "Только текст"),
        ("evidence", "Текст + доказательства"),
        ("all", "Всё сразу"),
    ]
    return ui_keyboard([
        (f"{'✓ ' if selected == key else ''}{label}", f"content:{key}")
        for key, label in options
    ] + [("Назад", "settings")])


def complaint_list(items: list[dict]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for item in items[:10]:
        builder.button(
            text=f"#{item['site_id']} | {item['author'][:25]} → {item['target'][:25]}",
            callback_data=f"complaint:{item['site_id']}",
        )
    builder.button(text="Назад", callback_data="home")
    builder.adjust(1)
    return builder.as_markup()


def complaint_actions(site_id: str, target: str) -> InlineKeyboardMarkup:
    return ui_keyboard([
        ("Показать текст", f"showtext:{site_id}"),
        ("Показать доказательства", f"showproof:{site_id}"),
        ("Занести нарушителя в ЧС", f"blackadd:{target[:60]}"),
        ("Напомнить позже", f"remind:{site_id}"),
        ("Открыть на сайте", f"open:{site_id}"),
        ("Назад к жалобам", "complaints"),
    ])


def admin_menu() -> InlineKeyboardMarkup:
    return ui_keyboard([
        ("Список пользователей", "adm:users"),
        ("Управление пользователем", "adm:manage"),
        ("Рассылка", "adm:broadcast"),
        ("Последние логи", "adm:logs"),
        ("Закрыть", "adm:close"),
    ])


def confirm_menu(prefix: str) -> InlineKeyboardMarkup:
    return ui_keyboard([
        ("Да", f"{prefix}:yes"),
        ("Нет", f"{prefix}:no"),
    ])
