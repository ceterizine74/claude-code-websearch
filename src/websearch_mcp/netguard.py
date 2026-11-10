"""Проверка сетевых адресов перед запросом — защита от SSRF.

Сервер скачивает любой URL, который ему передаст MCP-клиент. Клиент, в свою
очередь, может быть направлен на вредоносный адрес содержимым другой страницы
или prompt-injection, поэтому доверять URL нельзя.

Блокируются приватные, loopback, link-local, зарезервированные и
не-глобальные адреса — включая ``169.254.169.254`` (cloud metadata) и
``127.0.0.1`` (локальные сервисы машины пользователя).

Проверяется и доменное имя после резолва: иначе достаточно зарегистрировать
``evil.example`` → ``127.0.0.1``, чтобы обойти проверку литерала.

**Известное ограничение (TOCTOU).** Адрес проверяется до установки соединения.
Злоумышленник, управляющий DNS, теоретически может отдать публичный IP на
проверку и приватный — на подключение. Полностью это снимается только
закреплением соединения на проверенном IP, чего httpx не предоставляет.
Для локального однопользовательского сервера остаточный риск приемлем, но
знать о нём нужно.
"""

from __future__ import annotations

import ipaddress
import os
import socket
from urllib.parse import urlparse

# Имена, которые всегда означают локальный/служебный хост, даже если DNS
# настроен необычно. Проверяются до резолва, чтобы не зависеть от него.
_BLOCKED_HOSTNAMES = frozenset({
    "localhost",
    "localhost.localdomain",
    "ip6-localhost",
    "ip6-loopback",
    "metadata",                    # Azure Instance Metadata Service
    "metadata.google.internal",    # GCP metadata
    "instance-data",               # AWS (внутри VPC)
    "kubernetes.default.svc",
})

_ALLOWED_SCHEMES = frozenset({"http", "https"})


def _allow_local() -> bool:
    """Осознанный отказ от защиты по loopback/приватным адресам.

    Чтение не ставится по умолчанию: без него сервер, которому подсунули
    вредоносную страницу, может быть направлен на ``127.0.0.1`` или на
    cloud-metadata и отдать содержимое обратно в контекст модели.

    Переменная нужна, потому что блокировка локальных адресов — регрессия
    для законного кейса: разработчик читает собственную документацию на
    ``localhost:3000``. Это его информированный выбор на его машине.
    """
    return os.environ.get("WEBSEARCH_ALLOW_LOCAL", "").strip().lower() in {
        "1", "true", "yes", "on",
    }


class BlockedAddressError(ValueError):
    """URL ведёт на запрещённый адрес. ValueError — чтобы вызывающий код
    мог отличить «нельзя запрашивать» от сетевой аварии и, где уместно,
    пропустить адрес вместо падения всего вызова.
    """


def resolve_addresses(host: str) -> list[str]:
    """Все IP-адреса хоста. Отдельная функция, чтобы тесты подменяли её
    и не зависели от DNS.

    Нерезолвимое имя считается блокировкой (fail closed): запрос всё равно
    не прошёл бы, а молча разрешить неизвестный хост нельзя.
    """
    try:
        infos = socket.getaddrinfo(host, None)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise BlockedAddressError(
            f"не удалось разрешить имя {host!r}: {exc}"
        ) from exc
    # set: один хост обычно отдаёт и A, и AAAA записи, дубли не нужны
    return sorted({info[4][0] for info in infos})


# Адреса сервисов метаданных облачных провайдеров. Блокируются ВСЕГДА, даже
# при WEBSEARCH_ALLOW_LOCAL: они отдают IAM-креденшелы, и легитимной причины
# читать их у инструмента для документации нет.
_ALWAYS_BLOCKED_IPS = frozenset({
    "169.254.169.254",   # AWS / Azure / GCP / Oracle metadata
    "169.254.170.2",     # AWS ECS task metadata
    "fd00:ec2::254",     # AWS metadata over IPv6
})

_ALWAYS_BLOCKED_HOSTS = frozenset({
    "metadata", "metadata.google.internal", "instance-data",
    "kubernetes.default.svc",
})


def _ip_block_reason(address: str, allow_local: bool = False) -> str | None:
    """Причина, по которой IP запрещён, или None если адрес публичный.

    ``is_global`` проверяется отдельно: в Python ``is_multicast`` может
    сочетаться с ``is_global == True``, а CGNAT-диапазон 100.64.0.0/10 не
    попадает ни в ``is_private``, ни в ``is_global``.

    При ``allow_local`` loopback и приватные адреса разрешаются, но адреса
    метаданных облаков — нет.
    """
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return None

    if str(ip) in _ALWAYS_BLOCKED_IPS:
        return f"{address} — сервис метаданных облачного провайдера"

    # IPv4-адрес, завёрнутый в IPv6 (::ffff:127.0.0.1) — частый способ обхода
    mapped = getattr(ip, "ipv4_mapped", None)
    if mapped is not None:
        inner = _ip_block_reason(str(mapped), allow_local)
        if inner:
            return f"{address} (IPv4-mapped: {inner})"

    # Link-local блокируется ВСЕГДА, даже при allow_local: в этом диапазоне
    # (169.254.0.0/16) живут сервисы метаданных облаков и zeroconf, а
    # легитимного кейса «читаю документацию» для него нет. Opt-in открывает
    # только loopback и RFC1918 — то, что действительно нужно для :3000.
    for flag in ("is_link_local", "is_reserved", "is_multicast",
                 "is_unspecified"):
        if getattr(ip, flag, False):
            return f"{address} — {flag.removeprefix('is_')}"

    for flag in ("is_loopback", "is_private"):
        if getattr(ip, flag, False):
            if allow_local:
                return None
            return f"{address} — {flag.removeprefix('is_')}"

    if not ip.is_global:
        return f"{address} — не глобальный адрес"

    return None


def _check_host(host: str, check_dns: bool = True,
                allow_local: bool = False) -> None:
    """Бросить BlockedAddressError, если хост ведёт на запрещённый адрес."""
    if not host:
        raise BlockedAddressError("в URL нет имени хоста")

    # IPv6 в скобках: [::1] → ::1
    bare = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    normalized = bare.rstrip(".").lower()

    # При allow_local служебные имена localhost/ip6-* разрешаются, но имена
    # сервисов метаданных — нет (см. _ALWAYS_BLOCKED_HOSTS).
    denied_hosts = _ALWAYS_BLOCKED_HOSTS if allow_local else _BLOCKED_HOSTNAMES
    if normalized in denied_hosts:
        raise BlockedAddressError(f"{host!r} — запрещённое служебное имя")

    literal = _ip_block_reason(normalized, allow_local)
    if literal:
        raise BlockedAddressError(literal)

    if not check_dns:
        return

    # Доменное имя: проверяем ВСЕ адреса, иначе достаточно одного приватного
    # в списке записей, чтобы запрос ушёл внутрь сети.
    for address in resolve_addresses(normalized):
        reason = _ip_block_reason(address, allow_local)
        if reason:
            raise BlockedAddressError(
                f"{host!r} разрешается в запрещённый адрес: {reason}"
            )


def validate_url(url: str, check_dns: bool = True) -> str:
    """Проверить URL и вернуть его же, если он разрешён.

    Бросает ``BlockedAddressError`` (подкласс ``ValueError``) иначе.
    ``check_dns=False`` отключает резолв — для дешёвой фильтрации большого
    числа ссылок, где достаточно проверить литералы и служебные имена.
    """
    parsed = urlparse(url or "")

    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise BlockedAddressError(
            f"схема {parsed.scheme!r} не поддерживается (только http/https)"
        )

    _check_host(parsed.hostname or "", check_dns=check_dns,
                allow_local=_allow_local())
    return url


def is_public_url(url: str, check_dns: bool = True) -> bool:
    """Не бросающий вариант ``validate_url`` — для фильтрации списков ссылок."""
    try:
        validate_url(url, check_dns=check_dns)
    except (BlockedAddressError, ValueError):
        return False
    return True
