"""Эмуляция человеческого движения мыши (Human-like Cursor) через кривые Безье.

Зачем: антифрод-системы маркетплейсов отслеживают мгновенную телепортацию курсора
и клики в строго центральный пиксель элемента. 

Модуль строит плавную кривую Безье третьего порядка (Cubic Bézier Curve)
с естественным ускорением/замедлением (Ease-in-out), случайными контрольными
точками и микро-джиттером руки, после чего отправляет нативные события через
Chrome DevTools Protocol (CDP Input.dispatchMouseEvent) с системным флагом isTrusted=true.
"""

from __future__ import annotations

import logging
import math
import random
import time
from typing import List, Tuple

logger = logging.getLogger(__name__)


def ease_in_out(t: float) -> float:
    """Плавное ускорение в начале и замедление в конце пути."""
    return 0.5 * (1.0 - math.cos(math.pi * t))


def generate_bezier_trajectory(
    start_x: float,
    start_y: float,
    end_x: float,
    end_y: float,
    steps: int = 25,
    deviation_factor: float = 0.25,
) -> List[Tuple[int, int]]:
    """Генерирует последовательность координат (x, y) по кривой Безье с микро-отклонениями."""
    dx = end_x - start_x
    dy = end_y - start_y
    dist = math.hypot(dx, dy)

    if dist < 5 or steps <= 2:
        return [(int(start_x), int(start_y)), (int(end_x), int(end_y))]

    # Нормальный вектор для искривления траектории
    nx = -dy / dist
    ny = dx / dist

    # Случайный изгиб траектории (влево или вправо)
    arc_magnitude = dist * random.uniform(0.1, max(0.15, deviation_factor))
    side = random.choice([-1.0, 1.0])

    # Контрольные точки P1 и P2
    p1_x = start_x + dx * random.uniform(0.2, 0.4) + nx * arc_magnitude * side
    p1_y = start_y + dy * random.uniform(0.2, 0.4) + ny * arc_magnitude * side

    p2_x = start_x + dx * random.uniform(0.6, 0.8) + nx * (arc_magnitude * 0.7) * side
    p2_y = start_y + dy * random.uniform(0.6, 0.8) + ny * (arc_magnitude * 0.7) * side

    trajectory: List[Tuple[int, int]] = []

    for i in range(steps + 1):
        tau = i / float(steps)
        t = ease_in_out(tau)

        # Формула кривой Безье 3-го порядка
        inv = 1.0 - t
        x = (inv**3) * start_x + 3 * (inv**2) * t * p1_x + 3 * inv * (t**2) * p2_x + (t**3) * end_x
        y = (inv**3) * start_y + 3 * (inv**2) * t * p1_y + 3 * inv * (t**2) * p2_y + (t**3) * end_y

        # Микро-джиттер руки (кроме первой и последней точки)
        if 0 < i < steps:
            x += random.uniform(-1.5, 1.5)
            y += random.uniform(-1.5, 1.5)

        trajectory.append((int(round(x)), int(round(y))))

    return trajectory


class HumanCursor:
    """Контроллер физического курсора мыши через CDP."""

    def __init__(self, initial_x: int = 200, initial_y: int = 200):
        self.x = initial_x
        self.y = initial_y

    def move_to(
        self,
        driver,
        target_x: int,
        target_y: int,
        steps: int = 20,
        speed_ms: float = 8.0,
    ) -> None:
        """Плавно перемещает курсор к указанным координатам экрана."""
        path = generate_bezier_trajectory(self.x, self.y, target_x, target_y, steps=steps)

        for px, py in path:
            try:
                driver.execute_cdp_cmd(
                    "Input.dispatchMouseEvent",
                    {
                        "type": "mouseMoved",
                        "x": px,
                        "y": py,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.debug(f"CDP mouseMoved error: {exc}")
                break

            self.x = px
            self.y = py
            # Небольшая случайная вариация интервала между движениями
            time.sleep(random.uniform(speed_ms * 0.0006, speed_ms * 0.0014))

        self.x = target_x
        self.y = target_y

    def click_point(self, driver, x: int, y: int) -> None:
        """Плавно ведёт мышь в точку (x, y) и нажимает левую кнопку с естественной паузой."""
        self.move_to(driver, x, y)
        time.sleep(random.uniform(0.06, 0.14))  # Пауза перед кликом

        try:
            # Нажатие кнопки мыши
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mousePressed",
                    "button": "left",
                    "clickCount": 1,
                    "x": x,
                    "y": y,
                },
            )
            time.sleep(random.uniform(0.05, 0.12))  # Время удержания кнопки (dwell time)
            # Отпускание кнопки мыши
            driver.execute_cdp_cmd(
                "Input.dispatchMouseEvent",
                {
                    "type": "mouseReleased",
                    "button": "left",
                    "clickCount": 1,
                    "x": x,
                    "y": y,
                },
            )
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"CDP click error: {exc}")

    def click_element(self, driver, element) -> None:
        """Находит элемент, плавно прокручивает страницу при необходимости и кликает."""
        try:
            # Плавный скролл, если элемент вне зоны видимости
            driver.execute_script(
                "arguments[0].scrollIntoView({behavior: 'smooth', block: 'center'});",
                element,
            )
            time.sleep(random.uniform(0.2, 0.4))
        except Exception:  # noqa: BLE001
            pass

        try:
            rect = driver.execute_script(
                """
                const r = arguments[0].getBoundingClientRect();
                return {x: r.left, y: r.top, w: r.width, h: r.height};
                """,
                element,
            )
        except Exception:
            rect = element.rect

        x = rect.get("x", 0)
        y = rect.get("y", 0)
        w = max(4, rect.get("w", rect.get("width", 20)))
        h = max(4, rect.get("h", rect.get("height", 20)))

        # Человек никогда не кликает в строгий центр: берём случайную точку в диапазоне 25%..75%
        target_x = int(x + random.uniform(0.25 * w, 0.75 * w))
        target_y = int(y + random.uniform(0.25 * h, 0.75 * h))

        self.click_point(driver, target_x, target_y)


# Синглтон-курсор для процесса
_DEFAULT_CURSOR = HumanCursor()


def human_click(driver, element) -> None:
    """Высокоуровневая функция: плавный человеческий клик по элементу."""
    try:
        _DEFAULT_CURSOR.click_element(driver, element)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Human-click failed ({exc}), fallback to element.click()")
        try:
            element.click()
        except Exception:
            driver.execute_script("arguments[0].click();", element)


def human_move_to(driver, element) -> None:
    """Плавное наведение курсора на элемент без клика."""
    try:
        rect = driver.execute_script(
            """
            const r = arguments[0].getBoundingClientRect();
            return {x: r.left, y: r.top, w: r.width, h: r.height};
            """,
            element,
        )
        x = rect.get("x", 0)
        y = rect.get("y", 0)
        w = max(4, rect.get("w", 20))
        h = max(4, rect.get("h", 20))
        target_x = int(x + random.uniform(0.3 * w, 0.7 * w))
        target_y = int(y + random.uniform(0.3 * h, 0.7 * h))
        _DEFAULT_CURSOR.move_to(driver, target_x, target_y)
    except Exception as exc:  # noqa: BLE001
        logger.debug(f"Human move failed ({exc})")
