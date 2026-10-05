"""Сэмпл отзывов с открытой карточки товара — источник «живости» продавца.

Как устроено в Ozon (снято с живого API 26.09.2026, см. AGENTS.md §3.6):

* В карточке товара (`/api/composer-api.bx/page/json/v2?url=%2Fproduct%2F{id}%2F`)
  отзывов НЕТ — есть только счётчик `webReviewProductScore` с полями
  `url` (путь страницы отзывов, например `product/slug-123/reviews`)
  и `reviewsCount`.
* Список отзывов отдаёт страница `/{url}`: виджет `webListReviews`,
  массив `reviews` (30 штук, самые свежие).
* У отзыва `comments: {list, totalCount}` — но `list` в общей выдаче ВСЕГДА
  пуст. Тело комментариев приходит только по якорной ссылке
  `{страница отзывов}?ruuid={uuid отзыва}` — и только для якорного отзыва.
* Ответ продавца — это комментарий с `author.clientOfficial.id != "0"`
  (у магазина там его seller_id, `partnerType: 1`, имя и brandUri).
  Комментарий покупателя имеет `id: "0"`. Поле `isUserComment` НЕ годится:
  оно False у обоих.

Стоимость для сбора: +1 запрос на карточку (страница отзывов) плюс не более
`MAX_ANCHORS_PER_CARD` якорных запросов — только для отзывов, у которых
есть комментарии (их обычно единицы из тридцати). Темп держит адаптивная
пауза OzonPageClient, сессия не страдает.

Учёт честный: отзывы, у которых комментарии есть, но не проверены (вышел
лимит якорей), из сэмпла ВЫПАДАЮТ — иначе непроверенные молчуны завышали
бы «молчит». Отзыв с `totalCount == 0` разрешён достоверно: ответ продавца —
тоже комментарий, без комментариев ответа нет.
"""

import json
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Сколько отзывов с комментариями максимум проверяем якорями на одной карточке.
MAX_ANCHORS_PER_CARD = 2


def extract_reviews_url(card_json: Dict) -> str:
    """Путь страницы отзывов из счётчика карточки; пусто, если виджета нет.

    В виджете путь без ведущего слэша (`product/slug/reviews`) — приводим
    к виду, который принимает composer-API.
    """
    for key, raw in (card_json or {}).get("widgetStates", {}).items():
        if not key.startswith("webReviewProductScore") or not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        url = (payload.get("url") or "").strip()
        if url:
            return "/" + url.lstrip("/")
    return ""


def reviews_count(card_json: Dict) -> int:
    """Сколько всего отзывов у товара по счётчику карточки (0 если нет данных)."""
    for key, raw in (card_json or {}).get("widgetStates", {}).items():
        if not key.startswith("webReviewProductScore") or not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        try:
            return int(payload.get("reviewsCount") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def parse_reviews(page_json: Dict) -> List[dict]:
    """Список отзывов из ответа страницы отзывов (пусто, если не нашёлся)."""
    for key, raw in (page_json or {}).get("widgetStates", {}).items():
        if not key.startswith("webListReviews") or not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        reviews = payload.get("reviews")
        if isinstance(reviews, list):
            return reviews
    return []


def is_shop_reply(comment: Dict) -> bool:
    """Это комментарий магазина (ответ продавца)?

    Признак — заполненный clientOfficial автора: у покупателя там
    `id: "0"` и пусто, у магазина — его seller_id, имя и partnerType.
    """
    author = (comment or {}).get("author") or {}
    official = author.get("clientOfficial") or {}
    shop_id = str(official.get("id") or "")
    if shop_id and shop_id != "0":
        return True
    return bool(official.get("partnerType"))


def count_shop_replies(comments: List[dict]) -> int:
    """Сколько ответов продавца в списке комментариев отзыва."""
    return sum(1 for c in comments or [] if is_shop_reply(c))


def plan_sample(reviews: List[dict], max_anchors: int = MAX_ANCHORS_PER_CARD
                ) -> Tuple[List[str], int]:
    """Чистый план сэмпла: кого якорить и сколько отзывов осталось неразобранными.

    Возвращает (uuid отзывов для якорных запросов, число неразобранных).
    Неразобранные — отзывы с комментариями сверх лимита якорей: про них
    неизвестно, был ли среди комментариев ответ магазина, поэтому в сэмпл
    они не попадают (см. модульную докстроку).
    """
    candidates = [
        r for r in reviews or []
        if ((r.get("comments") or {}).get("totalCount") or 0) > 0 and r.get("uuid")
    ]
    anchors = [r["uuid"] for r in candidates[:max(0, max_anchors)]]
    return anchors, len(candidates) - len(anchors)


def extract_questions_count(card_json: Dict) -> int:
    """Extracts questions count from the product card."""
    for key, raw in (card_json or {}).get("widgetStates", {}).items():
        if not isinstance(raw, str):
            continue
        if "webQAProduct" in key or "webQuestion" in key or "webCharacteristicsQA" in key:
            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                continue
            count = payload.get("questionsCount") or payload.get("unansweredCount") or payload.get("totalCount") or 0
            if count:
                try:
                    return int(count)
                except (ValueError, TypeError):
                    pass
    return 0


def sample_from_card(client, card_json: Dict,
                     max_anchors: int = MAX_ANCHORS_PER_CARD) -> Tuple[int, int, int, float, int]:
    """Снимает сэмпл отзывов с уже полученного JSON карточки товара.

    Возвращает (seen, replies, unanswered, recent_rating, unanswered_questions).
    Ничего не бросает: сбор не должен падать из-за витального сэмпла.
    Любая неудача — нули, продавец просто остаётся без пополнения.
    """
    try:
        url = extract_reviews_url(card_json)
        unanswered_questions = extract_questions_count(card_json)
        
        if not url:
            return 0, 0, 0, 0.0, unanswered_questions
        if reviews_count(card_json) <= 0:
            return 0, 0, 0, 0.0, unanswered_questions

        page_json = client.page(url)
        reviews = parse_reviews(page_json or {})
        if not reviews:
            return 0, 0, 0, 0.0, unanswered_questions

        anchors, unresolved = plan_sample(reviews, max_anchors)
        replies = 0
        unanswered = 0
        recent_rating_sum = 0.0
        
        for r in reviews:
            content = r.get("content") or {}
            recent_rating_sum += float(content.get("score") or 0.0)
            
            comments = r.get("comments") or {}
            if int(comments.get("totalCount") or 0) == 0:
                unanswered += 1

        recent_rating = round(recent_rating_sum / len(reviews), 2) if reviews else 0.0

        for uuid in anchors:
            replies += count_shop_replies(_anchored_comments(client, url, uuid))

        seen = len(reviews) - unresolved
        return max(0, seen), replies, unanswered, recent_rating, unanswered_questions
    except Exception as exc:                                 # noqa: BLE001
        logger.debug(f"Сэмпл отзывов не удался: {exc}")
        return 0, 0, 0, 0.0, 0


def _anchored_comments(client, reviews_url: str, review_uuid: str) -> List[dict]:
    """Комментарии одного отзыва (якорная ссылка); пусто при любой неудаче."""
    page_json = client.page(f"{reviews_url}?ruuid={review_uuid}") or {}
    for review in parse_reviews(page_json):
        if review.get("uuid") == review_uuid:
            comments = (review.get("comments") or {}).get("list")
            return comments if isinstance(comments, list) else []
    return []
