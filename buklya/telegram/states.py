"""
Состояния конечного автомата (FSM) диалогов бота.
"""

from aiogram.fsm.state import State, StatesGroup


class AddAccount(StatesGroup):
    """Мастер подключения почтового ящика."""

    email = State()  # ждём адрес
    password = State()  # ждём пароль / пароль приложения
    login = State()  # ждём логин, если он отличается от адреса
    imap = State()  # ручной ввод IMAP-сервера host:port
    smtp = State()  # ручной ввод SMTP-сервера host:port[:ssl|starttls]


class ChangePassword(StatesGroup):
    """Смена пароля у подключённого ящика."""

    password = State()


class Compose(StatesGroup):
    """Написание письма (ответ или новое)."""

    to = State()  # адресаты (для нового письма)
    subject = State()  # тема
    body = State()  # текст и вложения (можно несколькими сообщениями)
    edit_subject = State()  # смена темы из черновика
    edit_cc = State()  # указание копии из черновика


class LinkMessengers(StatesGroup):
    """Связка мессенджеров: ждём код из другого мессенджера."""

    code = State()
