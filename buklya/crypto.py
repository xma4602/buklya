"""
Шифрование учётных данных почтовых ящиков.

Модель угроз: злоумышленник получил копию файла БД (или дамп Postgres).
Он не должен суметь извлечь логины, пароли и адреса серверов.

Как это устроено:
    * Мастер-ключ (`ENCRYPTION_KEY`) хранится только в окружении/секрете,
      в базе его нет.
    * Для каждого пользователя Букли из мастер-ключа выводится свой ключ
      (HKDF-SHA256, info = "buklya:user:<id пользователя>"). Даже если кто-то
      скопирует зашифрованное поле в запись другого пользователя, расшифровать
      его не выйдет.
    * Данные шифруются Fernet (AES-128-CBC + HMAC-SHA256, со встроенной
      проверкой целостности).
    * Для поиска дубликатов адресов используется «слепой индекс» —
      HMAC-SHA256 от адреса. По нему нельзя восстановить сам адрес.
    * Поддерживается ротация ключа: старые ключи указываются в
      `OLD_ENCRYPTION_KEYS`, расшифровка пробует их по очереди.

Сгенерировать новый мастер-ключ:  python -m buklya.crypto
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


class CryptoError(Exception):
    """Ошибка шифрования/расшифровки (неверный ключ, повреждённые данные)."""


def generate_master_key() -> str:
    """Создаёт новый случайный мастер-ключ (base64-строка)."""
    return Fernet.generate_key().decode()


def _decode_master(key: str) -> bytes:
    """Проверяет и декодирует мастер-ключ из base64."""
    try:
        raw = base64.urlsafe_b64decode(key.encode())
    except Exception as exc:  # noqa: BLE001
        raise CryptoError("ENCRYPTION_KEY должен быть base64-строкой") from exc
    if len(raw) != 32:
        raise CryptoError("ENCRYPTION_KEY должен содержать 32 байта (сгенерируйте: python -m buklya.crypto)")
    return raw


def _derive(master: bytes, purpose: str) -> bytes:
    """Выводит 32-байтовый подключ из мастер-ключа для заданной цели."""
    hkdf = HKDF(algorithm=hashes.SHA256(), length=32, salt=b"buklya-v1", info=purpose.encode())
    return hkdf.derive(master)


class CredentialCipher:
    """Шифратор данных пользователя с ключами, привязанными к ID пользователя Букли."""

    def __init__(self, master_key: str, old_keys: list[str] | None = None) -> None:
        # Первый ключ — актуальный (им шифруем), остальные — только для расшифровки
        self._masters = [_decode_master(master_key)] + [_decode_master(k) for k in (old_keys or [])]
        self._index_key = _derive(self._masters[0], "buklya:blind-index")

    # ------------------------------------------------------------------ internal
    def _fernet_for(self, user_id: int) -> MultiFernet:
        """MultiFernet из подключей пользователя для всех известных мастер-ключей."""
        fernets = [
            Fernet(base64.urlsafe_b64encode(_derive(master, f"buklya:user:{user_id}")))
            for master in self._masters
        ]
        return MultiFernet(fernets)

    # ------------------------------------------------------------------ public
    def encrypt(self, user_id: int, plaintext: str) -> str:
        """Шифрует строку для пользователя `user_id`."""
        return self._fernet_for(user_id).encrypt(plaintext.encode("utf-8")).decode("ascii")

    def decrypt(self, user_id: int, token: str) -> str:
        """Расшифровывает строку пользователя `user_id`."""
        try:
            return self._fernet_for(user_id).decrypt(token.encode("ascii")).decode("utf-8")
        except InvalidToken as exc:
            raise CryptoError("Не удалось расшифровать данные: неверный ключ или повреждённая запись") from exc

    def encrypt_json(self, user_id: int, data: Any) -> str:
        """Шифрует произвольный JSON-сериализуемый объект."""
        return self.encrypt(user_id, json.dumps(data, ensure_ascii=False))

    def decrypt_json(self, user_id: int, token: str) -> Any:
        """Расшифровывает объект, зашифрованный `encrypt_json`."""
        return json.loads(self.decrypt(user_id, token))

    def rotate(self, user_id: int, token: str) -> str:
        """Перешифровывает данные актуальным мастер-ключом (для ротации)."""
        return self._fernet_for(user_id).rotate(token.encode("ascii")).decode("ascii")

    def token_hash(self, value: str) -> str:
        """HMAC одноразового значения без привязки к пользователю (коды связки мессенджеров)."""
        return hmac.new(self._index_key, f"token:{value.strip().upper()}".encode("utf-8"), hashlib.sha256).hexdigest()

    def blind_index(self, user_id: int, value: str) -> str:
        """Необратимый хэш значения (для проверки уникальности без раскрытия)."""
        msg = f"{user_id}:{value.strip().lower()}".encode("utf-8")
        return hmac.new(self._index_key, msg, hashlib.sha256).hexdigest()


if __name__ == "__main__":  # pragma: no cover
    # Утилита: python -m buklya.crypto -> печатает новый мастер-ключ
    print(generate_master_key())
