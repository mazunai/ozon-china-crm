#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Управление автономной обработкой очереди без дашборда.

    python run_autonomous.py status    # состояние режима, очереди, инцидентов
    python run_autonomous.py resume    # снять риск-паузу после разбора

Долгоживущий режим крутится в дашборде (кнопка/API /api/autonomous/start)
и переживает его перезапуск. CLI нужен для проверки состояния и снятия
риск-паузы прямо с VM, когда под рукой нет браузера.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.utils.logger import setup_logging

REPO_ROOT = Path(__file__).parent
STATE_PATH = REPO_ROOT / "data" / "autonomous_state.json"
LOCK_PATH = REPO_ROOT / "data" / "autonomous.lock"
STOP_PATH = REPO_ROOT / "data" / "autonomous.stop"


def cmd_status() -> int:
    from src.crm.store import SellerStore

    print("=== Автономный режим ===")
    if STATE_PATH.exists():
        try:
            state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except ValueError:
            state = {}
        for key in ("running", "paused_reason", "rounds", "sent", "failed",
                    "skipped", "started_at", "updated_at",
                    "last_finish_reason", "last_error"):
            if key in state:
                print(f"  {key}: {state[key]}")
    else:
        print("  стейта нет — режим ни разу не запускался")

    if LOCK_PATH.exists():
        print(f"  лок: {LOCK_PATH.read_text(encoding='utf-8').strip()} (pid)")
    if STOP_PATH.exists():
        print("  штатный стоп-файл: на месте")
    risk_stop = REPO_ROOT / "outreach_data" / "STOP"
    if risk_stop.exists():
        print(f"  СТОП-КРАН отправщика: {risk_stop} — отправка заблокирована")

    store = SellerStore(REPO_ROOT / "data" / "sellers.sqlite3")
    health = store.queue_health()
    print("=== Очередь ===")
    print(f"  готово к отправке: {health['ready']}, с ошибкой: {health['failed']}, "
          f"без товара: {health['no_product']}")
    print(f"  сегодня отправлено: {store.sent_today()}")

    incident = store.open_risk_incident()
    if incident:
        print("=== ОТКРЫТЫЙ ИНЦИДЕНТ ===")
        print(f"  #{incident['id']} маркер «{incident['marker']}» этап {incident['stage']}"
              f" продавец {incident['seller_id'] or '—'} от {incident['created_at']}")
        print(f"  деталь: {(incident['detail'] or '')[:300]}")
        print("  → проверьте аккаунт, затем: python run_autonomous.py resume")
    return 0


def cmd_resume() -> int:
    from src.crm.store import SellerStore
    from src.outreach.autonomous import AutonomousRunner
    from src.notify import telegram as tg

    store = SellerStore(REPO_ROOT / "data" / "sellers.sqlite3")
    runner = AutonomousRunner(
        data_dir=REPO_ROOT / "outreach_data",
        state_path=STATE_PATH, lock_path=LOCK_PATH, stop_path=STOP_PATH,
        tg_send=tg.send,
    )
    result = runner.resume(store)
    print("Возобновлено:", json.dumps(result, ensure_ascii=False))
    return 0


def main() -> int:
    setup_logging()
    parser = argparse.ArgumentParser(description="Автономная обработка очереди")
    parser.add_argument("command", choices=("status", "resume"))
    args = parser.parse_args()
    return cmd_status() if args.command == "status" else cmd_resume()


if __name__ == "__main__":
    sys.exit(main())
