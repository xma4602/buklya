"""Шифрование учётных данных."""

import pytest

from buklya.crypto import CredentialCipher, CryptoError, generate_master_key


def test_roundtrip_and_user_binding():
    cipher = CredentialCipher(generate_master_key())
    token = cipher.encrypt(1, "secret-password")
    assert "secret" not in token
    assert cipher.decrypt(1, token) == "secret-password"
    # Запись одного пользователя не расшифровывается ключом другого
    with pytest.raises(CryptoError):
        cipher.decrypt(2, token)


def test_wrong_master_key():
    token = CredentialCipher(generate_master_key()).encrypt(1, "x")
    with pytest.raises(CryptoError):
        CredentialCipher(generate_master_key()).decrypt(1, token)


def test_key_rotation():
    old, new = generate_master_key(), generate_master_key()
    token = CredentialCipher(old).encrypt(5, "data")
    rotated = CredentialCipher(new, [old])
    assert rotated.decrypt(5, token) == "data"
    fresh = rotated.rotate(5, token)
    assert CredentialCipher(new).decrypt(5, fresh) == "data"


def test_blind_index():
    cipher = CredentialCipher(generate_master_key())
    assert cipher.blind_index(1, "A@B.ru ") == cipher.blind_index(1, "a@b.ru")
    assert cipher.blind_index(1, "a@b.ru") != cipher.blind_index(2, "a@b.ru")
    assert "a@b" not in cipher.blind_index(1, "a@b.ru")


def test_bad_key():
    with pytest.raises(CryptoError):
        CredentialCipher("short")
