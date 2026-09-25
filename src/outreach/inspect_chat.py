"""
Одноразовая разведка разметки чата Ozon.

Зачем нужен отдельный инструмент
--------------------------------
Разметка чата Ozon закрыта авторизацией: посмотреть её можно только из
залогиненной сессии, а угадывать CSS-селекторы для кнопки «Отправить» —
плохая идея, потому что ошибка отправит сообщение не туда или не отправит
вообще. Поэтому отправщик требует файл `selectors.json`, а этот модуль его
готовит: открывает карточку товара, находит кнопку чата, жмёт её, снимает
кандидатов на поле ввода и кнопку отправки, и складывает всё в отчёт.

Запуск (нужно быть залогиненным в Ozon в постоянном профиле Chrome):

    python run_outreach.py inspect --seller <seller_id>

Результат:
    outreach_data/selectors.json    — предложенные селекторы (проверьте глазами)
    outreach_data/inspect_dump.html — полный HTML страницы с открытым чатом
"""

import json
import logging
import time
from pathlib import Path
from typing import Dict, List, Optional

logger = logging.getLogger(__name__)

# JS собирает кандидатов: всё, что похоже на поле ввода или на кнопку, плюс
# сведения о шапке — по ним определяется, залогинены мы или нет.
COLLECT_SCRIPT = """
const visible = (el) => {
  const r = el.getBoundingClientRect();
  const st = window.getComputedStyle(el);
  return r.width > 2 && r.height > 2 && st.visibility !== 'hidden' && st.display !== 'none';
};

const cssPath = (el) => {
  if (el.id) return '#' + CSS.escape(el.id);
  for (const attr of ['data-testid', 'data-widget', 'data-state-id', 'name', 'placeholder', 'aria-label']) {
    const v = el.getAttribute && el.getAttribute(attr);
    if (v && v.length < 60) return `${el.tagName.toLowerCase()}[${attr}="${CSS.escape(v)}"]`;
  }
  const parts = [];
  let node = el;
  for (let depth = 0; node && node.nodeType === 1 && depth < 5; depth++) {
    let part = node.tagName.toLowerCase();
    const cls = (node.className || '').toString().trim().split(/\\s+/).filter(Boolean).slice(0, 2);
    if (cls.length) part += '.' + cls.map((c) => CSS.escape(c)).join('.');
    if (node.parentElement) {
      const same = [...node.parentElement.children].filter((c) => c.tagName === node.tagName);
      if (same.length > 1) part += `:nth-of-type(${same.indexOf(node) + 1})`;
    }
    parts.unshift(part);
    if (/^[a-z]+$/.test(parts[0]) === false && depth >= 2) break;
    node = node.parentElement;
  }
  return parts.join(' > ');
};

const collect = (selector, kind) =>
  [...document.querySelectorAll(selector)]
    .filter(visible)
    .slice(0, 8)
    .map((el) => ({
      kind,
      tag: el.tagName.toLowerCase(),
      selector: cssPath(el),
      text: (el.innerText || el.value || '').trim().slice(0, 60),
      attrs: {
        placeholder: el.getAttribute('placeholder'),
        name: el.getAttribute('name'),
        type: el.getAttribute('type'),
        'aria-label': el.getAttribute('aria-label'),
        'data-testid': el.getAttribute('data-testid'),
      },
    }));

const header = [...document.querySelectorAll('[data-widget^="profileMenu"]')]
  .map((el) => el.getAttribute('data-widget'));

return {
  url: location.href,
  profileMenuWidgets: header,
  inputs: [
    ...collect('textarea', 'textarea'),
    ...collect('input[type="text"]', 'input-text'),
    ...collect('[contenteditable="true"]', 'contenteditable'),
  ],
  buttons: collect('button', 'button'),
  chatCandidates: [...document.querySelectorAll('button, a, [role="button"]')]
    .filter(visible)
    .filter((el) => /^(чат|написать|сообщение)/i.test((el.innerText || '').trim()))
    .slice(0, 6)
    .map((el) => ({ selector: cssPath(el), text: (el.innerText || '').trim().slice(0, 40) })),
};
"""


def _propose_selectors(data: Dict) -> Dict:
    """Собирает черновик selectors.json из того, что нашлось на странице."""
    header_widgets = data.get("profileMenuWidgets") or []

    login_marker = ""
    if any("Anonymous" not in w for w in header_widgets):
        login_marker = '[data-widget="%s"]' % next(
            w for w in header_widgets if "Anonymous" not in w
        )

    chat_button = ""
    if data.get("chatCandidates"):
        chat_button = "text=" + data["chatCandidates"][0]["text"]

    # Первое видимое поле ввода — обычно это и есть поле сообщения.
    inputs = data.get("inputs") or []
    message_input = inputs[0]["selector"] if inputs else ""

    send_button = ""
    for candidate in data.get("buttons") or []:
        text = (candidate.get("text") or "").lower()
        if any(word in text for word in ("отправить", "send")):
            send_button = "text=" + candidate["text"]
            break

    return {
        "login_marker": login_marker,
        "chat_button": chat_button,
        "message_input": message_input,
        "send_button": send_button,
        "sent_marker": "",
        "_comment": (
            "Черновик, снятый автоматически. Проверьте каждый селектор глазами: "
            "неверный message_input или send_button приведёт к отправке не туда "
            "или к отправке пустого сообщения."
        ),
    }


def inspect_chat(product_url: str, data_dir: Path, dump_html: bool = True) -> Dict:
    """Открывает карточку товара, раскрывает чат и снимает разметку."""
    from selenium.webdriver.common.by import By

    from ..utils.selenium_manager import SeleniumManager

    manager = SeleniumManager(headless=False)
    driver = manager.create_driver()
    report: Dict = {"stage": "start", "product_url": product_url}

    try:
        driver.get(product_url)
        time.sleep(4)

        before = driver.execute_script(COLLECT_SCRIPT)
        report["stage"] = "page_loaded"
        report["profileMenuWidgets"] = before.get("profileMenuWidgets") or []

        if any("Anonymous" in w for w in report["profileMenuWidgets"]):
            report["logged_in"] = False
            report["warning"] = (
                "Похоже, вы не залогинены (в шапке profileMenuAnonymous). "
                "Войдите в Ozon в открывшемся окне и повторите inspect."
            )
        else:
            report["logged_in"] = True

        candidates = before.get("chatCandidates") or []
        report["chat_candidates"] = candidates

        if not candidates:
            report["warning"] = (
                "Кнопка «Чат» не найдена. Проверьте, что карточка товара "
                "принадлежит продавцу, с которым можно связаться."
            )
        else:
            driver.execute_script(
                "arguments[0].scrollIntoView({block: 'center'});",
                driver.find_element(By.XPATH, f"//*[contains(normalize-space(text()), {json.dumps(candidates[0]['text'])})]"),
            )
            time.sleep(0.5)
            clicked = False
            try:
                driver.find_element(
                    By.XPATH, f"//*[contains(normalize-space(text()), {json.dumps(candidates[0]['text'])})]"
                ).click()
                clicked = True
            except Exception as exc:                       # noqa: BLE001
                report["click_error"] = str(exc)[:200]
            report["stage"] = "chat_clicked" if clicked else "click_failed"
            time.sleep(5)

        after = driver.execute_script(COLLECT_SCRIPT)
        report["inputs"] = after.get("inputs")
        report["buttons"] = (after.get("buttons") or [])[:20]
        report["final_url"] = after.get("url")

        proposed = _propose_selectors(after)
        report["proposed_selectors"] = proposed

        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "selectors.proposed.json").write_text(
            json.dumps(proposed, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        report["selectors_written_to"] = str(data_dir / "selectors.proposed.json")

        if dump_html:
            dump_path = data_dir / "inspect_dump.html"
            dump_path.write_text(driver.page_source, encoding="utf-8")
            report["html_dump"] = str(dump_path)

        report["stage"] = "done"
    finally:
        try:
            driver.quit()
        except Exception:                                  # noqa: BLE001
            pass

    return report
