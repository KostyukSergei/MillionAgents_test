import argparse
import csv
import os
import re
import sys
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests


BASE_URL = "https://middle-api.magnit.ru"

STORE_TYPE = "market"
CATALOG_TYPE = "4"
STORE_CODE = "000"

DEFAULT_LIMIT = 20

# FIAS ID из HAR:
FIAS_SPB = "c2deb16a-0330-4f05-821f-1d09c93331e6"
FIAS_MOSCOW = "0c5b2444-70a0-4932-980c-b4dc0d3f02b5"

KNOWN_CITIES = {
    # Москва
    "москва": FIAS_MOSCOW,
    "moscow": FIAS_MOSCOW,
    "msk": FIAS_MOSCOW,
    # Санкт-Петербург
    "санкт-петербург": FIAS_SPB,
    "санкт петербург": FIAS_SPB,
    "спб": FIAS_SPB,
    "питер": FIAS_SPB,
    "saint petersburg": FIAS_SPB,
    "st petersburg": FIAS_SPB,
    "spb": FIAS_SPB,
}


@dataclass
class ProductRow:
    product_id: str
    name: str
    regular_price: Optional[float]
    promo_price: Optional[float]
    brand: str
    city: str


def money_from_int(value: Optional[int]) -> Optional[float]:
    # цены в ответах приходят целыми (обычно копейки) -> рубли
    if value is None:
        return None
    return round(value / 100.0, 2)


def extract_brand_from_name(name: str) -> str:
    # В HAR brand отдельным полем не приходит -> best-effort из названия
    s = name.strip()
    s = re.sub(r"^(кофе|кофейный\s+напиток|кофейные\s+напитки)\s+", "", s, flags=re.I)
    tokens = re.split(r"[\s,]+", s)
    if not tokens:
        return ""

    skip = {
        "растворимый", "молотый", "зерновой", "натуральный", "жареный",
        "сублимированный", "в", "капсулах", "дрип", "дрип-пакетах",
        "смесь", "для", "кофемашин", "эспрессо", "арабика", "робуста",
        "вес", "г", "кг", "мл",
    }

    for t in tokens:
        tt = t.strip().strip("()[]{}\"'").replace("«", "").replace("»", "")
        if not tt or tt.lower() in skip:
            continue
        if re.match(r"^[A-Z][A-Za-z0-9\-]+$", tt) or re.match(r"^[А-ЯЁ][А-Яа-яЁё0-9\-]+$", tt):
            return tt
        if re.match(r"^[A-Z0-9\-]{2,}$", tt):
            return tt

    for t in tokens:
        tt = t.strip().strip("()[]{}\"'").replace("«", "").replace("»", "")
        if tt and tt.lower() not in skip:
            return tt

    return ""


def make_session(token: str, device_id: str, app_version: str, user_agent: str) -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "authorization": f"bearer {token}",
        "x-device-id": device_id,
        "x-app-version": app_version,
        "user-agent": user_agent,
        "content-type": "application/json; charset=UTF-8",
        "accept": "application/json",
    })
    return s


def resolve_city(session: requests.Session, city: Optional[str], fias_id: Optional[str]) -> Tuple[str, str, str]:
    """
    Получаем cityId и красивое имя города строго через эндпоинт приложения:
      POST /market/v2/city/info  { "fiasId": "..." }

    Возвращает: (city_id, city_name, fias_id)
    """
    fias = None

    if fias_id:
        fias = fias_id.strip()
    elif city:
        key = city.strip().lower()
        fias = KNOWN_CITIES.get(key)

        # мягкие совпадения для ввода типа "Москва, РФ"
        if fias is None:
            for k, v in KNOWN_CITIES.items():
                if k in key:
                    fias = v
                    break

    if not fias:
        raise RuntimeError(
            "Не могу определить fiasId.\n"
            "Варианты:\n"
            "  1) Для Москвы/СПб используйте --city 'Москва' или --city 'Санкт-Петербург'\n"
            "  2) Для любого другого города передайте --fias-id <UUID> (как делает приложение в HAR)\n"
        )

    url = f"{BASE_URL}/market/v2/city/info"
    r = session.post(url, json={"fiasId": fias}, timeout=30)
    r.raise_for_status()
    data = r.json()

    city_id = str(data.get("cityId", "")).strip()
    city_name = str(data.get("name", "")).strip() or (city or "")

    if not city_id:
        raise RuntimeError(f"city/info не вернул cityId для fiasId={fias}")

    return city_id, city_name, fias


def get_coffee_category_id(session: requests.Session) -> int:
    url = f"{BASE_URL}/v3/categories/store/{STORE_CODE}"
    params = {"storetype": STORE_TYPE, "catalogtype": CATALOG_TYPE}
    r = session.get(url, params=params, timeout=30)
    r.raise_for_status()
    data = r.json()

    def walk(nodes: Any) -> Optional[int]:
        if isinstance(nodes, dict):
            if nodes.get("name") == "Кофе":
                return int(nodes["id"])
            for ch in nodes.get("children", []) or []:
                found = walk(ch)
                if found is not None:
                    return found
        elif isinstance(nodes, list):
            for n in nodes:
                found = walk(n)
                if found is not None:
                    return found
        return None

    cid = walk(data.get("items", []))
    if cid is None:
        raise RuntimeError("Не нашёл категорию 'Кофе' в дереве категорий.")
    return cid


def goods_search_page(
    session: requests.Session,
    category_id: int,
    city_id: str,
    offset: int,
    limit: int,
    token: str = "",
) -> Tuple[List[Dict[str, Any]], str]:
    url = f"{BASE_URL}/v2/goods/search"
    payload = {
        "catalogType": CATALOG_TYPE,
        "pagination": {"limit": limit, "offset": offset},
        "sort": {"order": "desc", "type": "popularity"},
        "storeCode": STORE_CODE,
        "storeType": STORE_TYPE,
        "categories": [category_id],
        "cityId": city_id,
        "filters": [],
        "token": token or "",
    }
    r = session.post(url, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("items", []) or [], data.get("token", "") or ""


def iter_coffee_products_in_stock(
    session: requests.Session,
    category_id: int,
    city_id: str,
    limit: int = DEFAULT_LIMIT,
    sleep_sec: float = 0.2,
) -> Iterable[Dict[str, Any]]:
    offset = 0
    search_token = ""
    while True:
        items, search_token = goods_search_page(
            session=session,
            category_id=category_id,
            city_id=city_id,
            offset=offset,
            limit=limit,
            token=search_token,
        )
        if not items:
            break

        for it in items:
            qty = it.get("quantity")
            if isinstance(qty, int) and qty > 0:
                yield it

        offset += limit
        if sleep_sec:
            time.sleep(sleep_sec)


def to_row(it: Dict[str, Any], city_name: str) -> ProductRow:
    promo = it.get("promotion") or {}
    is_promo = bool(promo.get("isPromotion"))

    price = it.get("price")
    old_price = promo.get("oldPrice")

    promo_price = money_from_int(price) if is_promo else None
    regular_price = money_from_int(old_price) if is_promo and isinstance(old_price, int) else money_from_int(price)

    name = (it.get("name") or "").strip()
    product_id = str(it.get("productId") or it.get("id") or "").strip()
    brand = extract_brand_from_name(name)

    return ProductRow(
        product_id=product_id,
        name=name,
        regular_price=regular_price,
        promo_price=promo_price,
        brand=brand,
        city=city_name,
    )


def write_csv(rows: List[ProductRow], out_path: str) -> None:
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f, delimiter=";")
        w.writerow(["id", "name", "regular_price", "promo_price", "brand", "city"])
        for r in rows:
            w.writerow([
                r.product_id,
                r.name,
                "" if r.regular_price is None else f"{r.regular_price:.2f}",
                "" if r.promo_price is None else f"{r.promo_price:.2f}",
                r.brand,
                r.city,
            ])


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--token", default=os.getenv("MAGNIT_TOKEN"),
                   help="Bearer JWT token (как в HAR: authorization: bearer ...)")
    p.add_argument("--device-id", default=os.getenv("MAGNIT_DEVICE_ID"),
                   help="x-device-id (как в HAR)")
    p.add_argument("--app-version", default=os.getenv("MAGNIT_APP_VERSION", "8.90.0"),
                   help="x-app-version (как в HAR)")
    p.add_argument("--user-agent", default=os.getenv("MAGNIT_UA", "okhttp/5.1.0"),
                   help="User-Agent (как в HAR)")

    # Город
    p.add_argument("--city", help="Город (например: Москва / Санкт-Петербург). Для других городов используйте --fias-id.")
    p.add_argument("--fias-id", help="FIAS UUID города (как использует приложение)")
    p.add_argument("--out", default="coffee.csv", help="Путь к CSV")
    p.add_argument("--limit", type=int, default=DEFAULT_LIMIT, help="Размер страницы (в HAR = 20)")
    args = p.parse_args()

    if not args.token or not args.device_id:
        print(
            "Нужно передать --token и --device-id (или env MAGNIT_TOKEN / MAGNIT_DEVICE_ID).\n"
            "Эти значения видны в HAR в заголовках запросов к middle-api.magnit.ru.",
            file=sys.stderr,
        )
        return 2

    session = make_session(args.token, args.device_id, args.app_version, args.user_agent)

    # 1) Определяем cityId через /market/v2/city/info (по FIAS)
    city_id, city_name, fias = resolve_city(session, args.city, args.fias_id)

    # 2) Категория "Кофе"
    coffee_category_id = get_coffee_category_id(session)

    # 3) Парсинг товаров в наличии
    rows: List[ProductRow] = []
    for it in iter_coffee_products_in_stock(session, coffee_category_id, city_id, limit=args.limit):
        rows.append(to_row(it, city_name))

    write_csv(rows, args.out)
    print(f"OK: город={city_name} (cityId={city_id}, fiasId={fias}) | выгружено {len(rows)} товаров (в наличии) -> {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
