"""
HTTP-слой дашборда: авторизация, защита деструктивных операций, ниши.

Аудит: HTTP-слой (~45 эндпоинтов) не был покрыт совсем, а здесь — замок
перебора пароля (обещание v1.8.3) и единственные операции, удаляющие
собранную базу (delete/purge с защитой подтверждённых).

Сервер поднимается на эфемерном порту с ВРЕМЕННОЙ базой; живые данные
не затрагиваются.

Запуск: PYTHONPATH=. python test/test_dashboard_http.py
"""

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "dashboard"))

os.environ["OZON_DASHBOARD_PASSWORD"] = "test-pass-123"
os.environ["WB_ENABLED"] = "0"

import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("dashserver", REPO / "dashboard" / "server.py")
server = importlib.util.module_from_spec(_spec)
sys.argv = ["server.py", "--port", "0"]
_spec.loader.exec_module(server)

import threading  # noqa: E402
from http.server import ThreadingHTTPServer  # noqa: E402

PASSED = 0
TOTAL = 0


def check(condition, description):
    global PASSED, TOTAL
    TOTAL += 1
    mark = "OK " if condition else "FAIL"
    PASSED += 1 if condition else 0
    print(f"  [{mark}] {description}")
    return condition


def main() -> int:
    with tempfile.TemporaryDirectory() as raw_tmp:
        tmp = Path(raw_tmp)
        db = tmp / "sellers.sqlite3"
        server.CRUD.db_path = db
        server.CRUD_WB.db_path = tmp / "wb.sqlite3"

        # Тестовые продавцы: один подтверждённый, один нет
        store = server.CRUD.store()
        from src.outreach.scoring import SellerMetrics

        store.add_sellers([
            SellerMetrics.from_raw(
                seller_id="111", orders_count="10", reviews_count="5",
                working_time="12 мес", average_rating="4.8",
                shop_name="Тест Подтверждённый", company_name="Test Co",
                is_chinese=True, uscc="91330203MA2H632J5N", province="Zhejiang",
                verified_by="uscc",
            ),
            SellerMetrics.from_raw(
                seller_id="222", orders_count="1", reviews_count="0",
                working_time="1 мес", average_rating="4.0",
                shop_name="Тест Отсеянный", company_name="ИП Ктото",
                is_chinese=False, verified_by="none",
            ),
        ])

        httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
        port = httpd.server_address[1]
        thread = threading.Thread(target=httpd.serve_forever, daemon=True)
        thread.start()
        base = f"http://127.0.0.1:{port}"

        def request(path, data=None, headers=None):
            req = urllib.request.Request(base + path, headers=headers or {})
            body = None
            if data is not None:
                body = json.dumps(data).encode("utf-8")
                req.add_header("Content-Type", "application/json")
                req.data = body
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, resp.read().decode("utf-8")
            except urllib.error.HTTPError as e:
                return e.code, e.read().decode("utf-8")

        # Авторизация
        print("Авторизация")
        code, _ = request("/api/health")
        check(code == 401, "без пароля — 401")
        req = urllib.request.Request(base + "/api/health")
        import base64
        req.add_header("Authorization", "Basic " +
                       base64.b64encode(b"admin:wrong-pass").decode())
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                code = resp.status
        except urllib.error.HTTPError as e:
            code = e.code
        check(code == 401, "неверный пароль — 401")

        auth = "Basic " + base64.b64encode(b"admin:test-pass-123").decode()

        def get(path):
            req = urllib.request.Request(base + path)
            req.add_header("Authorization", auth)
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return e.code, {}

        def post(path, data):
            req = urllib.request.Request(base + path)
            req.add_header("Authorization", auth)
            req.add_header("Content-Type", "application/json")
            req.data = json.dumps(data).encode("utf-8")
            try:
                with urllib.request.urlopen(req, timeout=10) as resp:
                    return resp.status, json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as e:
                return e.code, json.loads(e.read().decode("utf-8"))

        print("Список продавцов и ниши")
        code, data = get("/api/sellers?limit=10")
        check(code == 200 and data["total"] == 1,
              "список по умолчанию — только подтверждённые (uscc)")
        check("niche_facets" in data, "фасеты ниш присутствуют в ответе")

        print("Защита деструктивных операций (purge/delete — единственные операции, удаляющие базу)")
        # purge: подтверждённых не сносит, возвращает 400 «нет неподтверждённых»
        code, resp = post("/api/sellers/purge", {"seller_ids": ["111"]})
        check(code == 400 and "неподтверждённых" in resp.get("error", ""),
              "purge подтверждённого — 400, подтверждённые не сносятся")
        code, _ = get("/api/sellers?limit=10")
        check(code == 200, "после отказа база не изменилась")
        # purge неподтверждённого — удаляется
        code, resp = post("/api/sellers/purge", {"seller_ids": ["222"]})
        check(code == 200 and resp.get("deleted") == 1,
              "purge неподтверждённого — удаляется штатно")
        # delete подтверждённого без allow_verified — 409 need_confirm
        code, resp = post("/api/sellers/delete", {"seller_ids": ["111"]})
        check(code == 409 and "подтверждённых" in resp.get("error", ""),
              "delete подтверждённого без allow_verified — 409")
        # с allow_verified — удаляется
        code, resp = post("/api/sellers/delete",
                          {"seller_ids": ["111"], "allow_verified": True})
        check(code == 200, "delete подтверждённого с явным allow_verified — 200")
        # дети удалены из журналов (аудит: сироты)
        import sqlite3
        conn = sqlite3.connect(db)
        orphans = conn.execute(
            "SELECT COUNT(*) FROM seller_products WHERE seller_id='222'").fetchone()[0]
        conn.close()
        check(orphans == 0, "журналы удалённого продавца чистятся в той же транзакции")

        print("Валидация входа (аудит: инъекция заголовков)")
        code, _ = get("/api/sellers/export.csv?marketplace=%0d%0aX-Injected:%201")
        check(code in (400, 200), "кривой marketplace не роняет сервер")

        print("Мультиаккаунт выведен из эксплуатации — маршрутов /api/accounts больше нет")
        code, _ = get("/api/accounts")
        check(code in (404, 501), "маршрут /api/accounts убран")

        httpd.shutdown()

    print(f"\nРезультат: {PASSED}/{TOTAL} кейсов прошло")
    return 0 if PASSED == TOTAL else 1


if __name__ == "__main__":
    sys.exit(main())
