"""Реестр аккаунтов отправителя (менеджеров).

Зачем: у команды несколько аккаунтов Ozon, и каждый менеджер работает из
СВОЕГО профиля Chrome (свои cookie сессии) на СВОЁМ порту отладки. Один
процесс отправщика — один аккаунт; очередь в базе атомарная, поэтому
процессы не мешают друг другу и продавец не получает два сообщения.

Правила мультиаккаунта (решение владельца от 02.10.2026, см. AGENTS.md):
* аккаунт — отдельная личность со своими лимитами, а не запасная корзина;
* при ограничении платформы останавливается только этот аккаунт
  (свой стоп-кран), автопереключения на другой аккаунт нет;
* подмена отпечатков и «переброс» нагрузки между аккаунтами не делаются.

Файл реестра: outreach_data/accounts.json. Аккаунт `main` — основной:
его профиль и порт совпадают с историческими значениями парсера
(`~/.ozon_parser_chrome`, порт из OZON_DEBUG_PORT / 9222), поэтому уже
залогиненная сессия продолжает работать без переделок.
"""

import json
import logging
import re
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import List, Optional

logger = logging.getLogger(__name__)

MAIN_ID = "main"
# id попадает в имя профиля Chrome и стоп-крана (STOP_<id>) — только
# файлобезопасные символы в нижнем регистре.
_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
_BASE_PROFILE = Path.home() / ".ozon_parser_chrome"
_BASE_PORT = 9222


@dataclass
class SenderAccount:
    """Один аккаунт отправителя: сессия Chrome + лимиты.

    Лимитные поля — переопределения поверх outreach_data/sender.json
    (None — взять общее значение). Оператор выставляет их сам: по умолчанию
    у всех аккаунтов одинаковые лимиты, и каждый считает их по СВОИМ
    сообщениям в журнале.
    """

    id: str
    label: str = ""
    profile: str = ""        # путь к профилю Chrome; пусто — выводится из id
    port: int = 0            # порт отладки; 0 — выводится (main: базовый порт)
    daily_cap: Optional[int] = None
    interval_minutes: Optional[int] = None
    interval_jitter_minutes: Optional[int] = None
    window_start: Optional[str] = None
    window_end: Optional[str] = None
    # Разгон свежего аккаунта: лимит растёт от ramp_min_cap до daily_cap
    # за ramp_days суток. Свежая учка, которая в первый же день пишет много,
    # уезжает в антифрод (случалось 03.10.2026) — новым аккаунтам разгон
    # обязателен.
    proxy: str = ""          # индивидуальный резидентный прокси (host:port или user:pass@host:port)
    ramp_days: Optional[int] = None
    ramp_min_cap: Optional[int] = None

    def get_fingerprint(self):
        """Возвращает постоянный отпечаток аккаунта (привязан к его профилю)."""
        from ..utils.fingerprint import get_or_create_fingerprint
        return get_or_create_fingerprint(self.profile_dir(), account_id=self.id)

    def profile_dir(self) -> Path:
        if self.profile:
            return Path(self.profile).expanduser()
        if self.id == MAIN_ID:
            return _BASE_PROFILE
        return Path.home() / f".ozon_parser_chrome_{self.id}"

    def debug_port(self) -> int:
        # Явный порт в реестре главнее. Не задан: main — базовый, остальным
        # порт проставляется при загрузке по позиции (см. load_accounts).
        return int(self.port) if self.port else _BASE_PORT

    def stop_file_name(self, base_name: str) -> str:
        """Имя стоп-крана аккаунта: STOP для main, STOP_<id> для остальных."""
        return base_name if self.id == MAIN_ID else f"{base_name}_{self.id}"

    def apply_to_config(self, config) -> None:
        """Кладёт переопределения лимитов аккаунта в SenderConfig (на месте)."""
        for name in ("daily_cap", "interval_minutes", "interval_jitter_minutes",
                     "ramp_days", "ramp_min_cap"):
            value = getattr(self, name)
            if value is not None:
                setattr(config, name, int(value))
        for name in ("window_start", "window_end"):
            value = getattr(self, name)
            if value:
                setattr(config, name, str(value))


def accounts_path(data_dir: Path) -> Path:
    return Path(data_dir) / "accounts.json"


def load_accounts(data_dir: Path) -> List[SenderAccount]:
    """Читает реестр; файла нет — один main (историческое поведение).

    Аккаунтам без явного порта порт проставляется по позиции среди таких же
    (9223, 9224, …): детерминированно и без коллизий с main. Явно указанный
    порт в файле главнее — порядок строк тогда не важен.
    """
    path = accounts_path(data_dir)
    if path.exists():
        try:
            items = (json.loads(path.read_text(encoding="utf-8")) or {}).get("accounts") or []
        except (OSError, ValueError) as exc:
            raise ValueError(f"Не читается {path.name}: {exc}") from exc
        known = set(SenderAccount.__dataclass_fields__)
        accounts = []
        for item in items:
            try:
                accounts.append(SenderAccount(**{k: v for k, v in item.items() if k in known}))
            except TypeError as exc:
                raise ValueError(f"Битая запись аккаунта в {path.name}: {exc}") from exc
    else:
        accounts = []

    if not accounts:
        accounts = [SenderAccount(id=MAIN_ID)]

    slot = 0
    for acc in accounts:
        if not _ID_RE.match(acc.id or ""):
            raise ValueError(f"Недопустимый id аккаунта: {acc.id!r} "
                             "(нужны строчные латинские буквы, цифры, - и _)")
        if not acc.port and acc.id != MAIN_ID:
            slot += 1
            acc.port = _BASE_PORT + slot
    return accounts


def save_accounts(data_dir: Path, accounts: List[SenderAccount]) -> Path:
    path = accounts_path(data_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [{k: v for k, v in asdict(acc).items() if (v not in ("", None) and v != 0) or k == "id"}
               for acc in accounts]
    path.write_text(
        json.dumps({"accounts": payload}, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return path


def get_account(data_dir: Path, account_id: str) -> SenderAccount:
    """Аккаунт по id; неизвестный id — ошибка со списком существующих."""
    account_id = (account_id or "").strip().lower() or MAIN_ID
    accounts = load_accounts(data_dir)
    for acc in accounts:
        if acc.id == account_id:
            return acc
    known = ", ".join(acc.id for acc in accounts)
    raise ValueError(f"Аккаунт {account_id!r} не найден в accounts.json (есть: {known})")


def add_account(data_dir: Path, account_id: str, label: str = "", proxy: str = "") -> SenderAccount:
    """Добавляет аккаунт и сохраняет реестр; порт берёт следующий свободный."""
    account_id = (account_id or "").strip().lower()
    if not _ID_RE.match(account_id):
        raise ValueError("id аккаунта: строчные латинские буквы, цифры, - и _ "
                         "(например: mgr2)")
    accounts = load_accounts(data_dir)
    if any(acc.id == account_id for acc in accounts):
        raise ValueError(f"Аккаунт {account_id!r} уже есть в реестре")
    next_port = max([acc.port for acc in accounts] + [_BASE_PORT]) + 1
    account = SenderAccount(id=account_id, label=label.strip(), port=next_port, proxy=proxy.strip())
    accounts.append(account)
    save_accounts(data_dir, accounts)
    return account


def remove_account(data_dir: Path, account_id: str) -> None:
    """Убирает аккаунт из реестра. main удалить нельзя — он один всегда."""
    account_id = (account_id or "").strip().lower()
    if account_id == MAIN_ID:
        raise ValueError("Основной аккаунт main удалить нельзя")
    accounts = load_accounts(data_dir)
    left = [acc for acc in accounts if acc.id != account_id]
    if len(left) == len(accounts):
        raise ValueError(f"Аккаунта {account_id!r} нет в реестре")
    save_accounts(data_dir, left)
