"""
Обработчики бота ВКонтакте.

Импорт модулей регистрирует их обработчики в общем реестре `buklya.vk.router.router`.
Порядок не важен: приоритет (команды -> шаг диалога -> «не понимаю») задаёт
`VkDispatcher`.
"""

from buklya.vk.handlers import accounts, common, compose, inbox, link  # noqa: F401
