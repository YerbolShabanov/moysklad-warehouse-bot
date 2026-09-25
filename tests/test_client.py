"""Проверка формирования запросов к API МойСклад (httpx MockTransport)."""
import asyncio
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx

from msbot.moysklad import MoySkladClient, MoySkladError

BASE = "https://api.moysklad.ru/api/remap/1.2"
requests_log = []


def handler(request: httpx.Request) -> httpx.Response:
    requests_log.append(request)
    path = urlparse(str(request.url)).path
    query = parse_qs(urlparse(str(request.url)).query)

    if path.endswith("/entity/store"):
        return httpx.Response(200, json={"rows": [{"id": "store-1", "name": "Склад адрес. хранение"}]})

    if "/slots" in path:
        offset = int(query.get("offset", ["0"])[0])
        # 1500 ячеек -> две страницы по 1000
        rows = [{"id": f"s{i}", "name": f"A-{i}"} for i in range(offset, min(offset + 1000, 1500))]
        return httpx.Response(200, json={"meta": {"size": 1500}, "rows": rows})

    if path.endswith("/report/stock/byslot/current"):
        # 429 и 503 подряд: оба должны сняться повтором, а не упасть наверх
        calls = len([r for r in requests_log if "byslot" in str(r.url)])
        if calls == 1:
            return httpx.Response(429, headers={"X-Lognex-Retry-After": "10"}, json={})
        if calls == 2:
            return httpx.Response(503, text="<html><body>503 - Service Unavailable</body></html>")
        return httpx.Response(200, json=[
            {"assortmentId": "a1", "storeId": "store-1", "slotId": "s0", "stock": 4},
        ])

    if path.endswith("/entity/customerorder"):
        return httpx.Response(200, json={"rows": [{"id": "o1", "name": "00123"}]})

    return httpx.Response(404, json={"errors": [{"error": "Не найдено"}]})


async def main():
    client = MoySkladClient("TOKEN", BASE)
    client._client = httpx.AsyncClient(
        base_url=BASE,
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer TOKEN", "Accept-Encoding": "gzip"},
    )

    store = await client.find_store("Склад адрес. хранение")
    assert store["id"] == "store-1"
    assert requests_log[-1].headers["Authorization"] == "Bearer TOKEN"
    assert parse_qs(urlparse(str(requests_log[-1].url)).query)["filter"] == [
        "name=Склад адрес. хранение"
    ]

    slots = await client.get_slots("store-1")
    assert len(slots) == 1500, len(slots)

    rows = await client.stock_by_slot("store-1", ["a1", "a2"])
    assert rows == [{"assortmentId": "a1", "storeId": "store-1", "slotId": "s0", "stock": 4}]
    byslot_filter = parse_qs(urlparse(str(requests_log[-1].url)).query)["filter"][0]
    assert byslot_filter == "storeId=store-1;assortmentId=a1,a2", byslot_filter

    order = await client.find_order_by_name("00123")
    assert order["name"] == "00123"
    order_query = parse_qs(urlparse(str(requests_log[-1].url)).query)
    assert order_query["filter"] == ["name=00123"]
    assert order_query["expand"] == ["state,salesChannel,agent,store"]
    assert order_query["limit"] == ["100"]

    try:
        await client.get("/entity/nonexistent")
    except MoySkladError as exc:
        assert "404" in str(exc) and "Не найдено" in str(exc), exc
    else:
        raise AssertionError("ожидалась MoySkladError")

    # HTML-заглушка вместо JSON превращается в понятный текст
    stub = httpx.Response(503, text="<html><head><title>Error</title></head>"
                                    "<body>503 - Service Unavailable</body></html>")
    assert MoySkladClient._describe_error(stub) == "МойСклад 503: сервис временно недоступен"

    # Не-повторяемая ошибка поднимается сразу, без задержек
    attempts = []

    def failing(request):
        attempts.append(request)
        return httpx.Response(403, json={"errors": [{"error": "Нет прав"}]})

    strict = MoySkladClient("TOKEN", BASE)
    strict._client = httpx.AsyncClient(base_url=BASE, transport=httpx.MockTransport(failing))
    try:
        await strict.get("/entity/store")
    except MoySkladError as exc:
        assert "403" in str(exc) and "Нет прав" in str(exc), exc
    assert len(attempts) == 1, f"403 повторять не нужно, попыток: {len(attempts)}"
    await strict.aclose()

    await client.aclose()
    print("OK: запросы к API формируются верно, 429 и пагинация обработаны")


if __name__ == "__main__":
    asyncio.run(main())
