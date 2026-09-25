"""Тонкий асинхронный клиент JSON API МойСклад (remap 1.2).

Документация: https://dev.moysklad.ru/doc/api/remap/1.2/
Авторизация — токен доступа: заголовок `Authorization: Bearer <token>`.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Dict, Iterable, List, Optional, Sequence

import httpx

log = logging.getLogger(__name__)

MAX_RETRIES = 4
# 429 — превышен лимит запросов, 5xx — кратковременные сбои на стороне
# МойСклад (в логах это примерно один запрос из тысячи). И то и другое
# лечится повтором, поэтому не поднимаем ошибку наверх сразу.
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
PAGE_LIMIT = 1000
HTTP_REASONS = {
    500: "внутренняя ошибка сервиса",
    502: "сервис временно недоступен",
    503: "сервис временно недоступен",
    504: "сервис не ответил вовремя",
}
EXPAND_PAGE_LIMIT = 100  # expand игнорируется при limit > 100


class MoySkladError(RuntimeError):
    """Ошибка API МойСклад с человекочитаемым текстом."""


class MoySkladClient:
    def __init__(self, token: str, base_url: str, timeout: float = 30.0) -> None:
        self._base_url = base_url.rstrip("/")
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=timeout,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept-Encoding": "gzip",
                "Accept": "application/json;charset=utf-8",
            },
        )

    @property
    def base_url(self) -> str:
        return self._base_url

    async def aclose(self) -> None:
        await self._client.aclose()

    # ---------------------------------------------------------------- core

    async def get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Any:
        last_error = "МойСклад недоступен"
        for attempt in range(MAX_RETRIES):
            try:
                response = await self._client.get(path, params=params)
            except httpx.TransportError as exc:  # обрыв связи, таймаут, DNS
                last_error = f"МойСклад: нет связи ({exc.__class__.__name__})"
                if attempt == MAX_RETRIES - 1:
                    break
                delay = self._backoff(attempt)
                log.warning("%s, повтор через %.1f c", last_error, delay)
                await asyncio.sleep(delay)
                continue

            if response.is_success:
                return response.json()

            if response.status_code in RETRY_STATUSES:
                last_error = self._describe_error(response)
                if attempt == MAX_RETRIES - 1:
                    break
                delay = self._retry_delay(response, attempt)
                log.warning("МойСклад: %s, повтор через %.1f c",
                            response.status_code, delay)
                await asyncio.sleep(delay)
                continue

            raise MoySkladError(self._describe_error(response))

        raise MoySkladError(last_error)

    @classmethod
    def _retry_delay(cls, response: httpx.Response, attempt: int) -> float:
        """Задержка перед повтором: у 429 её подсказывает сам МойСклад."""
        raw = response.headers.get("X-Lognex-Retry-After")
        if raw and raw.isdigit():
            return min(int(raw) / 1000.0, 10.0)
        return cls._backoff(attempt)

    @staticmethod
    def _backoff(attempt: int) -> float:
        return min(0.5 * (2 ** attempt), 8.0)

    @staticmethod
    def _describe_error(response: httpx.Response) -> str:
        """Человекочитаемая ошибка.

        При сбоях МойСклад иногда отдаёт HTML-заглушку вместо JSON —
        показывать её сборщику бессмысленно, заменяем на понятный текст.
        """
        try:
            errors = response.json().get("errors") or []
            text = "; ".join(e.get("error", "") for e in errors if e.get("error"))
        except ValueError:
            text = ""

        if not text:
            text = HTTP_REASONS.get(
                response.status_code,
                re.sub(r"<[^>]+>", " ", response.text)[:150].strip() or "неизвестная ошибка",
            )
        return f"МойСклад {response.status_code}: {text}"

    async def get_all_rows(
        self,
        path: str,
        params: Optional[Dict[str, Any]] = None,
        limit: int = PAGE_LIMIT,
    ) -> List[Dict[str, Any]]:
        """Постранично собирает все `rows` коллекции."""
        rows: List[Dict[str, Any]] = []
        offset = 0
        while True:
            page_params = dict(params or {})
            page_params.update({"limit": limit, "offset": offset})
            payload = await self.get(path, page_params)
            batch = payload.get("rows") or []
            rows.extend(batch)
            size = (payload.get("meta") or {}).get("size", len(rows))
            offset += limit
            if len(batch) < limit or offset >= size:
                return rows

    # ------------------------------------------------------------ entities

    async def find_store(self, name: str) -> Optional[Dict[str, Any]]:
        """Склад по точному наименованию."""
        payload = await self.get("/entity/store", {"filter": f"name={name}", "limit": 2})
        rows = payload.get("rows") or []
        return rows[0] if rows else None

    async def get_slots(self, store_id: str) -> List[Dict[str, Any]]:
        """Все ячейки склада (адресное хранение)."""
        return await self.get_all_rows(f"/entity/store/{store_id}/slots")

    async def find_order_by_name(self, name: str) -> Optional[Dict[str, Any]]:
        """Заказ покупателя по точному номеру."""
        payload = await self.get(
            "/entity/customerorder",
            {
                "filter": f"name={name}",
                "expand": "state,salesChannel,agent,store",
                "limit": EXPAND_PAGE_LIMIT,
            },
        )
        rows = payload.get("rows") or []
        return rows[0] if rows else None

    async def search_orders(self, query: str, limit: int = 10) -> List[Dict[str, Any]]:
        """Поиск заказов покупателя по подстроке номера/контрагента."""
        payload = await self.get(
            "/entity/customerorder",
            {
                "search": query,
                "expand": "state,salesChannel,agent,store",
                "order": "moment,desc",
                "limit": min(limit, EXPAND_PAGE_LIMIT),
            },
        )
        return payload.get("rows") or []

    async def get_order(self, order_id: str) -> Dict[str, Any]:
        return await self.get(
            f"/entity/customerorder/{order_id}",
            {"expand": "state,salesChannel,agent,store"},
        )

    async def get_order_positions(self, order_id: str) -> List[Dict[str, Any]]:
        """Позиции заказа с раскрытым товаром (expand ограничен 100 на страницу).

        Единицу измерения раскрываем вложенным expand; если аккаунт его не
        принимает — откатываемся на обычный `assortment`.
        """
        path = f"/entity/customerorder/{order_id}/positions"
        variants = ("assortment.uom", "assortment")
        for index, expand in enumerate(variants):
            try:
                return await self.get_all_rows(
                    path, {"expand": expand}, limit=EXPAND_PAGE_LIMIT
                )
            except MoySkladError:
                if index == len(variants) - 1:
                    raise
                log.warning("expand=%s не поддержан, пробуем упрощённый", expand)

    # ------------------------------------------------------------- reports

    async def stock_by_slot(
        self,
        store_id: str,
        assortment_ids: Sequence[str],
        chunk_size: int = 50,
    ) -> List[Dict[str, Any]]:
        """Краткий отчёт об остатках по ячейкам `/report/stock/byslot/current`.

        Строка ответа: {assortmentId, storeId, slotId, stock}.
        Обязателен фильтр по storeId или assortmentId; товары вне ячеек не возвращаются.
        """
        rows: List[Dict[str, Any]] = []
        for chunk in _chunks(assortment_ids, chunk_size):
            payload = await self.get(
                "/report/stock/byslot/current",
                {"filter": f"storeId={store_id};assortmentId={','.join(chunk)}"},
            )
            if isinstance(payload, list):  # отчёт отдаёт голый массив
                rows.extend(payload)
            else:
                rows.extend(payload.get("rows") or [])
        return rows


def _chunks(items: Iterable[str], size: int) -> Iterable[List[str]]:
    batch: List[str] = []
    for item in items:
        batch.append(item)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch
