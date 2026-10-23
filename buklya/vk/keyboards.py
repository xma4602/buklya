"""
Клавиатуры бота ВКонтакте.

Особенности ВКонтакте:
    * главное меню — обычная клавиатура (text-кнопки с payload {"cmd": …});
    * кнопки под сообщениями — inline-клавиатура с callback-кнопками;
      нажатие приходит событием message_event с payload;
    * inline-клавиатура: не больше 10 кнопок и 6 рядов, подпись кнопки — до 40 символов;
    * payload — до 255 байт, поэтому в нём только короткие ключи и id.

Формат payload callback-кнопок: {"a": <раздел>, "op": <действие>, …id}.
"""

from __future__ import annotations

from vkbottle import Callback, Keyboard, KeyboardButtonColor, Text

from buklya.db.repository import AccountInfo
from buklya.mail.models import MessageHeader
from buklya.vk.texts import t

#: Ограничения inline-клавиатур ВКонтакте
MAX_INLINE_BUTTONS = 10
MAX_LABEL = 40

#: Сколько писем помещается на странице списка: номера писем + ряд навигации
#: (◀️ 🔄 ▶️) не должны превысить 10 кнопок
MAX_PAGE_SIZE = MAX_INLINE_BUTTONS - 3
#: Номеров писем в одном ряду
NUMBERS_PER_ROW = 4

#: Команды главного меню: payload cmd -> ключ подписи в локализации
MENU = {
    "last": "menu.last",
    "compose": "menu.compose",
    "accounts": "menu.accounts",
    "add": "menu.add",
    "link": "menu.link",
    "help": "menu.help",
}


def label(text: str) -> str:
    """Подпись кнопки с учётом лимита ВКонтакте."""
    text = " ".join(str(text).split())
    return text if len(text) <= MAX_LABEL else text[: MAX_LABEL - 1] + "…"


def _cb(text: str, color: KeyboardButtonColor | None = None, **payload) -> tuple[Callback, KeyboardButtonColor | None]:
    return Callback(label(text), payload=payload), color


#: Ограничение на число рядов inline-клавиатуры
MAX_INLINE_ROWS = 6


def _inline(rows: list[list[tuple[Callback, KeyboardButtonColor | None]]]) -> str:
    """
    Собирает inline-клавиатуру. Если кнопок слишком много для ограничений
    ВКонтакте, лишние отбрасываются, а при нехватке рядов кнопки раскладываются по две.
    """
    rows = [row for row in rows if row]
    flat = [button for row in rows for button in row][:MAX_INLINE_BUTTONS]
    if len(rows) > MAX_INLINE_ROWS or sum(len(row) for row in rows) > MAX_INLINE_BUTTONS:
        rows = [flat[i : i + 2] for i in range(0, len(flat), 2)]
    kb = Keyboard(inline=True)
    for row in rows:
        kb.row()
        for action, color in row:
            kb.add(action, color=color)
    return kb.get_json()


# ============================================================================ главное меню
def main_menu() -> str:
    """Постоянная клавиатура внизу чата."""
    kb = Keyboard(one_time=False, inline=False)
    layout = [["last", "compose"], ["accounts", "add"], ["link", "help"]]
    for row in layout:
        kb.row()
        for cmd in row:
            kb.add(Text(t.plain(MENU[cmd]), payload={"cmd": cmd}),
                   color=KeyboardButtonColor.PRIMARY if cmd == "last" else None)
    return kb.get_json()


# ============================================================================ ящики
def account_status_icon(acc: AccountInfo) -> str:
    """Иконка состояния ящика."""
    if not acc.is_active:
        return "⚠️"
    return "🔔" if acc.notify else "🔕"


def accounts_list(accounts: list[AccountInfo]) -> str:
    """Список ящиков пользователя (+ кнопка «Добавить»)."""
    rows = [[_cb(f"{account_status_icon(acc)} {acc.title}", a="acc", op="menu", id=acc.id)]
            for acc in accounts[: MAX_INLINE_BUTTONS - 1]]
    rows.append([_cb(t.plain("menu.add"), KeyboardButtonColor.POSITIVE, a="acc", op="add")])
    return _inline(rows)


def account_menu(acc: AccountInfo) -> str:
    """Меню одного ящика."""
    toggle = "accounts.btn_notify_off" if acc.notify and acc.is_active else "accounts.btn_notify_on"
    return _inline([
        [_cb(t.plain(toggle), a="acc", op="toggle", id=acc.id)],
        [_cb(t.plain("menu.last"), KeyboardButtonColor.PRIMARY, a="acc", op="last", id=acc.id),
         _cb(t.plain("menu.compose"), a="acc", op="compose", id=acc.id)],
        [_cb(t.plain("accounts.btn_change_password"), a="acc", op="passwd", id=acc.id)],
        [_cb(t.plain("accounts.btn_delete"), KeyboardButtonColor.NEGATIVE, a="acc", op="delete", id=acc.id)],
        [_cb(t.plain("accounts.btn_back"), a="acc", op="list")],
    ])


def confirm_delete(account_id: int) -> str:
    """Подтверждение удаления ящика."""
    return _inline([[
        _cb(t.plain("accounts.btn_delete_yes"), KeyboardButtonColor.NEGATIVE, a="acc", op="delete_ok", id=account_id),
        _cb(t.plain("accounts.btn_delete_no"), a="acc", op="menu", id=account_id),
    ]])


def choose_account(accounts: list[AccountInfo], action: str) -> str:
    """Выбор ящика для действия (last / compose)."""
    return _inline([[_cb(f"📮 {acc.title}", a="acc", op=action, id=acc.id)] for acc in accounts])


# ============================================================================ мастер подключения
def add_cancel() -> str:
    return _inline([[_cb(t.plain("buttons.cancel"), a="add", op="cancel")]])


def add_failed() -> str:
    """Варианты после неудачной попытки подключения."""
    return _inline([
        [_cb(t.plain("accounts.btn_retry_password"), KeyboardButtonColor.PRIMARY, a="add", op="password")],
        [_cb(t.plain("accounts.btn_login_differs"), a="add", op="login")],
        [_cb(t.plain("accounts.btn_manual_servers"), a="add", op="manual")],
        [_cb(t.plain("buttons.cancel"), a="add", op="cancel")],
    ])


# ============================================================================ письма
def messages_page(account_id: int, headers: list[MessageHeader], offset: int, limit: int, total: int) -> str:
    """Номера писем (само содержание списка — в тексте сообщения) + навигация."""
    numbers = [
        _cb(str(offset + index + 1), KeyboardButtonColor.PRIMARY if not h.seen else None,
            a="op", id=account_id, u=h.uid)
        for index, h in enumerate(headers[:MAX_PAGE_SIZE])
    ]
    rows = [numbers[i : i + NUMBERS_PER_ROW] for i in range(0, len(numbers), NUMBERS_PER_ROW)]
    nav = []
    if offset > 0:
        nav.append(_cb(t.plain("inbox.btn_newer"), a="pg", id=account_id, o=max(offset - limit, 0)))
    nav.append(_cb(t.plain("inbox.btn_refresh"), a="pg", id=account_id, o=offset))
    if offset + limit < total:
        nav.append(_cb(t.plain("inbox.btn_older"), a="pg", id=account_id, o=offset + limit))
    rows.append(nav)
    return _inline(rows)


def email_actions(ref_id: int, has_multiple_recipients: bool = True) -> str:
    """Кнопки под показанным письмом."""
    first = [_cb(t.plain("email.btn_reply"), KeyboardButtonColor.PRIMARY, a="msg", op="reply", r=ref_id)]
    if has_multiple_recipients:
        first.append(_cb(t.plain("email.btn_reply_all"), a="msg", op="replyall", r=ref_id))
    return _inline([first, [_cb(t.plain("email.btn_seen"), a="msg", op="seen", r=ref_id)]])


# ============================================================================ черновик
def compose_panel() -> str:
    """Панель черновика письма."""
    return _inline([
        [_cb(t.plain("compose.btn_send"), KeyboardButtonColor.POSITIVE, a="cmp", op="send"),
         _cb(t.plain("compose.btn_preview"), a="cmp", op="preview")],
        [_cb(t.plain("compose.btn_subject"), a="cmp", op="subject"),
         _cb(t.plain("compose.btn_cc"), a="cmp", op="cc")],
        [_cb(t.plain("compose.btn_clear"), a="cmp", op="clear"),
         _cb(t.plain("compose.btn_cancel"), KeyboardButtonColor.NEGATIVE, a="cmp", op="cancel")],
    ])


def compose_cancel() -> str:
    return _inline([[_cb(t.plain("buttons.cancel"), a="cmp", op="cancel")]])


# ============================================================================ связка мессенджеров
def link_menu(can_unlink: bool) -> str:
    """Кнопки экрана «Мессенджеры»."""
    rows = [[_cb(t.plain("link.btn_enter_code"), KeyboardButtonColor.PRIMARY, a="lnk", op="enter"),
             _cb(t.plain("link.btn_new_code"), a="lnk", op="code")]]
    if can_unlink:
        rows.append([_cb(t.plain("link.btn_unlink"), KeyboardButtonColor.NEGATIVE, a="lnk", op="unlink")])
    return _inline(rows)


def link_cancel() -> str:
    return _inline([[_cb(t.plain("buttons.cancel"), a="lnk", op="cancel")]])
