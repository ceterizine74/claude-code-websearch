"""CLI: кроссплатформенная кодировка вывода.

На Windows консоль по умолчанию открывается в cp866/cp1251, и click падает с
UnicodeEncodeError на символах вроде «→» из текста справки — это происходит
внутри click, до нашего _echo, поэтому перехват исключения не спасает.
Регрессия проверяется запуском CLI в отдельном процессе с принудительно
заданной кодировкой консоли.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _run_cli(env_encoding: str, *args: str) -> subprocess.CompletedProcess:
    """Запустить CLI в отдельном процессе с заданной кодировкой stdio.

    Отдельный процесс нужен потому, что кодировка stdout определяется при
    старте интерпретатора и не меняется из уже запущенного теста.
    Окружение наследуется, переопределяются только кодировка и PYTHONPATH.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = env_encoding
    env["PYTHONPATH"] = str(_PROJECT_ROOT / "src")
    return subprocess.run(
        [sys.executable, "-m", "websearch_mcp.cli", *args],
        capture_output=True,
        cwd=_PROJECT_ROOT,
        env=env,
        timeout=60,
    )


@pytest.mark.parametrize("encoding", ["cp1251", "cp866", "utf-8"])
def test_help_survives_non_utf8_console(encoding):
    """Справка не должна падать ни в одной из распространённых кодировок."""
    proc = _run_cli(encoding, "--help")
    assert proc.returncode == 0, (
        f"CLI упал в кодировке {encoding}:\n{proc.stderr.decode(errors='replace')}"
    )
    out = proc.stdout.decode("utf-8", errors="replace")
    assert "Локальный веб-поиск" in out
    # Символ «→» — именно он ломал cp1251/cp866
    assert "→" in out


@pytest.mark.parametrize("encoding", ["cp1251", "cp866"])
def test_subcommand_help_survives_non_utf8_console(encoding):
    proc = _run_cli(encoding, "search", "--help")
    assert proc.returncode == 0, proc.stderr.decode(errors="replace")
    assert "max-results" in proc.stdout.decode("utf-8", errors="replace")


def test_force_utf8_stdio_is_idempotent():
    """Повторный вызов не должен ломать уже перекодированный поток (macOS/Linux)."""
    from websearch_mcp.cli import _force_utf8_stdio

    before = sys.stdout.encoding
    _force_utf8_stdio()
    _force_utf8_stdio()
    after = sys.stdout.encoding
    # Поток остался рабочим и не перекодировался дважды в мусор
    assert after == before or after.lower().replace("-", "") == "utf8"
    sys.stdout.write("→\n")


def test_cache_dir_never_relative_without_home():
    """Без home-каталога путь кэша обязан быть явной ошибкой, а не относительным.

    Такое бывает в контейнерах и CI без HOME: Path.home() бросает RuntimeError
    или возвращает '.', и кэш молча создавался бы в непредсказуемом CWD
    MCP-клиента. Правильное поведение — громко упасть с подсказкой.
    """
    env = dict(os.environ)
    for var in ("HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
                "LOCALAPPDATA", "APPDATA", "XDG_CACHE_HOME"):
        env[var] = ""
    env["PYTHONPATH"] = str(_PROJECT_ROOT / "src")

    proc = subprocess.run(
        [sys.executable, "-c",
         "from websearch_mcp.cache import default_cache_dir;"
         "print(default_cache_dir())"],
        capture_output=True, cwd=_PROJECT_ROOT, env=env, timeout=60,
    )
    out = proc.stdout.decode("utf-8", errors="replace").strip()

    # Либо вернули абсолютный путь, либо упали с понятным сообщением
    if proc.returncode == 0:
        assert Path(out).is_absolute(), f"каталог кэша относительный: {out}"
    else:
        err = proc.stderr.decode("utf-8", errors="replace")
        assert "RuntimeError" in err
        assert "HOME" in err or "USERPROFILE" in err, \
            f"сообщение не подсказывает, что настроить:\n{err}"


def test_cache_dir_is_absolute_in_normal_environment():
    """В обычном окружении путь к кэшу всегда абсолютный."""
    from websearch_mcp.cache import default_cache_dir

    assert default_cache_dir().is_absolute()
