"""Фоллоу-ап: вежливое повторное касание тех, кому написали и кто молчит.

Правила безопасности (не нарушаются нигде в модуле):
* только тем, у кого статус ровно 'sent' — ответившим, отказавшимся и
  opt-out фоллоу-ап не пишется (отбор в store.followup_candidates);
* это ОСОЗНАННЫЙ повтор: глобальный запрет повторов из sender.run_queue
  здесь обходится не «мимо журнала», а явно — кампания помечается
  campaign_id='followup', и следующая такая кампания кандидата не увидит;
* интервал после_days и лимит за раунд настраиваются; по умолчанию выключено.

Текст фоллоу-апа живёт в outreach_data/followup.txt — НЕ в каталоге шаблонов
рассылки, чтобы не попадать в основную ротацию.
"""

import json
import logging
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from ..crm.risk import PlatformRestricted, load_extra_markers
from ..crm.store import SellerStore
from .sender import (
    ChatClient,
    ChatSelectors,
    build_driver,
    raise_if_platform_restricted,
)
from .templates import MessageTemplate

logger = logging.getLogger(__name__)


@dataclass
class FollowupConfig:
    """Настройки фоллоу-апа. Выключен по умолчанию — включать осознанно."""

    enabled: bool = False
    after_days: int = 14            # молчит дольше N дней — кандидат
    limit_per_round: int = 5        # сколько за один проход
    pause_seconds: int = 600        # пауза между фоллоу-апами в проходе

    @classmethod
    def load(cls, path: Path) -> "FollowupConfig":
        if not path.exists():
            return cls()
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return cls()
        known = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in data.items() if k in known})

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(asdict(self), ensure_ascii=False, indent=2), encoding="utf-8"
        )


def load_followup_template(data_dir: Path) -> Optional[MessageTemplate]:
    """Текст фоллоу-апа или None, если его нет / он ещё заготовка."""
    path = Path(data_dir) / "followup.txt"
    if not path.exists():
        return None
    try:
        template = MessageTemplate(name="followup", body=path.read_text(encoding="utf-8"))
    except Exception:                                        # noqa: BLE001
        return None
    if template.is_placeholder():
        return None
    return template


def run_followup_round(
    store: SellerStore,
    config: FollowupConfig,
    data_dir: Path,
    sender_account_id: str = "main",
    dry_run: bool = False,
    browser_guard=None,
    stop_event=None,
    on_progress: Optional[Callable[[str], None]] = None,
    account=None,
) -> dict:
    """Один проход фоллоу-апа. Возвращает сводку {sent, failed, skipped, reason}.

    browser_guard — BROWSER_LOCK дашборда (или None при ручных запусках):
    фоллоу-ап открывает вкладку в общем Chrome и обязан уважать общий замок.

    account — аккаунт, из сессии которого уходят фоллоу-апы (менеджер должен
    писать своим именем): кандидаты отбираются из ЕГО контактов, а браузер
    открывается с ЕГО профилем. None — основной аккаунт main.
    """
    report = {"sent": 0, "failed": 0, "skipped": 0, "reason": ""}

    def progress(message: str) -> None:
        logger.info(message)
        if on_progress:
            on_progress(message)

    if not config.enabled:
        report["reason"] = "фоллоу-апы выключены в настройках"
        return report

    template = load_followup_template(data_dir)
    if template is None:
        report["reason"] = "нет текста followup.txt (или это заготовка)"
        return report

    candidates = store.followup_candidates(
        config.after_days, config.limit_per_round,
        sender_account_id=sender_account_id or "main")
    if not candidates:
        report["reason"] = (f"кандидатов нет (молчат дольше {config.after_days} дн. "
                            f"среди контактов аккаунта {sender_account_id or 'main'})")
        progress(f"Фоллоу-ап: {report['reason']}")
        return report

    if browser_guard is not None:
        if not browser_guard.acquire(blocking=False):
            report["reason"] = "браузер занят другой задачей"
            return report

    extra_risk_markers = load_extra_markers(data_dir)
    manager = None
    try:
        if dry_run:
            row = candidates[0]
            text = template.render(dict(row))
            progress(f"[фоллоу-ап/предпросмотр] {row['shop_name'] or row['seller_id']}:\n{text}")
            report["reason"] = "предпросмотр"
            report["preview"] = {"seller_id": row["seller_id"], "text": text}
            return report

        manager, driver = build_driver(headless=False, account=account)
        selectors = ChatSelectors.load(Path(data_dir) / "selectors.json")
        client = ChatClient(driver, selectors, manager=manager)
        client._focus()
        driver.get("https://www.ozon.ru/")
        time.sleep(3)
        raise_if_platform_restricted(driver, extra_risk_markers, stage="фоллоу-ап: проверка входа")
        if not client.is_logged_in():
            report["reason"] = "нет залогиненной сессии Ozon"
            return report

        if hasattr(store, "register_sender_account"):
            store.register_sender_account(sender_account_id)

        for index, row in enumerate(candidates):
            if stop_event is not None and stop_event.is_set():
                report["reason"] = "остановлено вручную"
                break
            seller_id = row["seller_id"]
            shop = row["shop_name"] or seller_id
            text = template.render(dict(row))
            try:
                client.open_chat(seller_id)
                raise_if_platform_restricted(
                    driver, extra_risk_markers, stage=f"фоллоу-ап: чат {seller_id}")
                client.send(text)
                if client.confirm_sent():
                    # campaign_id='followup' — метка, по которой кандидат
                    # больше не попадёт в следующий проход.
                    store.mark_sent(seller_id, text, template_name="followup",
                                    sender_account_id=sender_account_id,
                                    campaign_id="followup")
                    report["sent"] += 1
                    progress(f"Фоллоу-ап отправлен: {shop}")
                else:
                    # Статус 'sent' не трогаем — только журнал: главное
                    # сообщение уже ушло, сбивать статус нельзя.
                    store.log_message(seller_id, text, "failed",
                                      "фоллоу-ап: не подтвердилась отправка",
                                      template_name="followup",
                                      sender_account_id=sender_account_id)
                    report["failed"] += 1
            except PlatformRestricted:
                # Ограничение платформы — стоп всего; инцидент запишет
                # основной Risk Control при следующем прогоне рассылки,
                # здесь фиксируем в журнал и выходим.
                report["reason"] = "ограничение платформы — фоллоу-ап остановлен"
                progress(f"СТОП: {report['reason']}")
                break
            except Exception as exc:                         # noqa: BLE001
                store.log_message(seller_id, text, "failed", str(exc)[:300],
                                  template_name="followup",
                                  sender_account_id=sender_account_id)
                report["failed"] += 1
                progress(f"Фоллоу-ап не удался ({shop}): {str(exc)[:120]}")

            if index < len(candidates) - 1:
                deadline = time.time() + config.pause_seconds
                while time.time() < deadline:
                    if stop_event is not None and stop_event.is_set():
                        break
                    time.sleep(min(2.0, max(0.0, deadline - time.time())))
    finally:
        if manager is not None:
            try:
                manager.close()   # только своя вкладка, Chrome общий
            except Exception:                                # noqa: BLE001
                pass
        if browser_guard is not None:
            try:
                browser_guard.release()
            except RuntimeError:
                pass
    return report
