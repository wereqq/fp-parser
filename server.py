import gc
import json
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Optional
import requests
from bs4 import BeautifulSoup
from lxml import html as lxml_html
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="FP // PARSER")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

PROXY_URL = os.getenv("PROXY_URL", "").strip()
PROXIES = {"http": PROXY_URL, "https": PROXY_URL} if PROXY_URL else None

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

GAMES = {
    "dota2": {"name": "Dota 2", "url": "https://funpay.com/lots/81/", "offer_id": "81"},
    "valorant": {"name": "Valorant", "url": "https://funpay.com/lots/612/", "offer_id": "612"},
}

LOTS_CACHE = {"timestamp": 0, "data": [], "errors": {}, "updating": False}
RATES_CACHE = {"timestamp": 0, "rates": {"RUB": 1.0, "USD": 0.0105, "KZT": 5.2}}
USER_SESSIONS = {}


class SendMessageReq(BaseModel):
    node_id: str
    text: str


def fp_request(method: str, url: str, **kwargs) -> requests.Response:
    headers = {**HEADERS, **kwargs.pop("headers", {})}
    cookies = {"cy": "rub", "locale": "ru", **kwargs.pop("cookies", {})}
    return requests.request(
        method=method,
        url=url,
        headers=headers,
        cookies=cookies,
        proxies=PROXIES,
        timeout=kwargs.pop("timeout", (5, 10)),
        **kwargs,
    )


def update_exchange_rates():
    now = time.time()
    if now - RATES_CACHE["timestamp"] < 3600:
        return
    try:
        r = requests.get("https://open.er-api.com/v6/latest/RUB", timeout=5)
        data = r.json()
        if data.get("result") == "success":
            rates = data.get("rates", {})
            if "USD" in rates and "KZT" in rates:
                RATES_CACHE["rates"] = {
                    "RUB": 1.0,
                    "USD": float(rates["USD"]),
                    "KZT": float(rates["KZT"]),
                }
                RATES_CACHE["timestamp"] = now
    except Exception:
        pass


def extract_lots_from_html(html_content: str, game_key: str, seen_ids: set) -> list:
    lots = []
    if not html_content or not html_content.strip():
        return lots

    tree = lxml_html.fromstring(html_content)
    # Ищем любые карточки лотов FunPay (основные и дополнительные классы)
    items = tree.xpath(
        '//a[contains(concat(" ", normalize-space(@class), " "), " tc-item ") '
        'or contains(concat(" ", normalize-space(@class), " "), " offer-item ")]'
    )

    for item in items:
        href = item.get("href", "")
        lot_id_match = re.search(r"id=(\d+)", href)
        lot_id = lot_id_match.group(1) if lot_id_match else href
        if not lot_id or lot_id in seen_ids:
            continue
        seen_ids.add(lot_id)

        desc_nodes = item.xpath(
            './/*[contains(@class, "tc-desc-text") or contains(@class, "offer-desc") or contains(@class, "js-offer-desc")]'
        )
        desc = " ".join(desc_nodes[0].text_content().split()) if desc_nodes else "Без описания"

        tags = []
        for col in item.xpath(
            './/*[contains(@class, "tc-server") or contains(@class, "tc-side") '
            'or contains(@class, "tc-amount") or contains(@class, "tc-param")]'
        ):
            txt = " ".join(col.text_content().split())
            if txt:
                tags.append(txt)

        seller_nodes = item.xpath('.//*[contains(@class, "media-user-name")]')
        seller = seller_nodes[0].text_content().strip() if seller_nodes else "Продавец"

        seller_id = None
        avatar_nodes = item.xpath('.//*[contains(@class, "avatar-photo")]')
        if avatar_nodes and avatar_nodes[0].get("data-href"):
            m = re.search(r"/users/(\d+)/", avatar_nodes[0].get("data-href"))
            if m:
                seller_id = int(m.group(1))

        rev_nodes = item.xpath('.//*[contains(@class, "rating-mini-count")]')
        reviews = rev_nodes[0].text_content().strip() if rev_nodes else "0"

        price_nodes = item.xpath('.//*[contains(@class, "tc-price") or contains(@class, "offer-price")]')
        price_val = 0.0
        price_str = "—"
        if price_nodes:
            price_str = " ".join(price_nodes[0].text_content().split())
            try:
                price_val = float(price_nodes[0].get("data-s", 0))
            except ValueError:
                price_val = 0.0

        lots.append({
            "id": lot_id,
            "game": game_key,
            "game_name": GAMES[game_key]["name"],
            "title": desc[:110] + ("..." if len(desc) > 110 else ""),
            "description": desc,
            "tags": tags,
            "price": price_val,
            "price_str": price_str,
            "seller": seller,
            "seller_id": seller_id,
            "reviews": reviews,
            "online": item.get("data-online") == "1",
            "url": href if href.startswith("http") else f"https://funpay.com{href}",
        })

    del tree, items
    return lots


def detect_offer_params(html_content: str, fallback_id: str) -> tuple[str, str]:
    """Пытается достать реальные параметры подгрузки лотов из разметки страницы."""
    tree = lxml_html.fromstring(html_content)
    offer_type = "lots"
    offer_id = fallback_id

    # Ищем элементы с data-offer-type или data-id / data-node
    for el in tree.xpath('//*[@data-offer-type or @data-node-id or @data-offer]'):
        if el.get("data-offer-type"):
            offer_type = el.get("data-offer-type")
        if el.get("data-node-id"):
            offer_id = el.get("data-node-id")
        elif el.get("data-offer"):
            offer_id = el.get("data-offer")

    del tree
    return offer_type, offer_id


def parse_game_lots(game_key: str, game_info: dict) -> tuple[str, list, Optional[str]]:
    lots = []
    seen_ids = set()
    max_per_game = 350

    try:
        resp = fp_request("GET", game_info["url"])
        resp.raise_for_status()
        html_text = resp.text
        base_lots = extract_lots_from_html(html_text, game_key, seen_ids)
        lots.extend(base_lots)
        print(f"[PARSER] {game_key} page 1: +{len(base_lots)} (total {len(lots)})", flush=True)

        offer_type, offer_id = detect_offer_params(html_text, game_info.get("offer_id", ""))

        # Пробуем подтянуть следующие страницы через AJAX /lots/offer
        if offer_id and len(lots) >= 10:
            for page in range(2, 9):
                if len(lots) >= max_per_game:
                    break
                added_on_page = 0
                for payload in (
                    {"game": offer_id, "nodeId": offer_id, "page": page, "offerType": offer_type},
                    {"id": offer_id, "page": page},
                ):
                    try:
                        ajax_resp = fp_request(
                            "POST",
                            "https://funpay.com/lots/offer",
                            headers={
                                "x-requested-with": "XMLHttpRequest",
                                "referer": game_info["url"],
                            },
                            data=payload,
                            timeout=(4, 7),
                        )
                        if ajax_resp.status_code == 200:
                            raw_txt = ajax_resp.text
                            # Иногда FunPay возвращает HTML внутри JSON-поля
                            if raw_txt.lstrip().startswith("{"):
                                try:
                                    j = json.loads(raw_txt)
                                    raw_txt = j.get("html") or j.get("content") or j.get("offers") or ""
                                except Exception:
                                    pass
                            extra = extract_lots_from_html(str(raw_txt), game_key, seen_ids)
                            if extra:
                                lots.extend(extra)
                                added_on_page += len(extra)
                                print(f"[PARSER] {game_key} ajax p.{page}: +{len(extra)} (total {len(lots)})", flush=True)
                                break
                    except Exception:
                        continue
                if added_on_page == 0:
                    break

        del resp, html_text
        gc.collect()
        return game_key, lots[:max_per_game], None
    except Exception as e:
        print(f"[PARSER ERROR] {game_key}: {e}", flush=True)
        return game_key, [], str(e)


def refresh_cache_sync():
    if LOTS_CACHE["updating"]:
        return
    LOTS_CACHE["updating"] = True
    try:
        update_exchange_rates()
        all_lots = []
        errors = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(parse_game_lots, k, v) for k, v in GAMES.items()]
            for f in futures:
                g_key, g_lots, err = f.result()
                all_lots.extend(g_lots)
                if err:
                    errors[g_key] = err
        if all_lots:
            LOTS_CACHE["data"] = all_lots
        LOTS_CACHE["errors"] = errors
        LOTS_CACHE["timestamp"] = time.time()
    finally:
        LOTS_CACHE["updating"] = False


def background_updater():
    while True:
        refresh_cache_sync()
        time.sleep(90)


@app.on_event("startup")
def start_bg_worker():
    threading.Thread(target=background_updater, daemon=True).start()


def get_fp_session(golden_key: str) -> dict:
    if not golden_key:
        raise HTTPException(status_code=401, detail="Не передан golden_key")

    cached = USER_SESSIONS.get(golden_key)
    if cached and (time.time() - cached["ts"] < 600):
        return cached

    resp = fp_request("GET", "https://funpay.com/", cookies={"golden_key": golden_key})
    soup = BeautifulSoup(resp.text, "lxml")
    body = soup.find("body")
    if not body or not body.get("data-app-data"):
        raise HTTPException(status_code=401, detail="FunPay не вернул данные сессии (проверь прокси или ключ)")

    app_data = json.loads(body["data-app-data"])
    user_id = app_data.get("userId", 0)
    csrf = app_data.get("csrf-token", "")

    if not user_id:
        raise HTTPException(status_code=401, detail="Неверный или устаревший golden_key")

    name_el = soup.select_one(".user-link-name")
    username = name_el.get_text(strip=True) if name_el else f"User #{user_id}"

    session_data = {
        "csrf": csrf,
        "user_id": int(user_id),
        "username": username,
        "ts": time.time(),
    }
    USER_SESSIONS[golden_key] = session_data
    return session_data


@app.get("/")
def serve_frontend():
    return FileResponse("index.html")


@app.get("/api/lots")
def api_lots(force: bool = False):
    if force:
        refresh_cache_sync()
    elif not LOTS_CACHE["data"] and not LOTS_CACHE["updating"]:
        refresh_cache_sync()

    return {
        "count": len(LOTS_CACHE["data"]),
        "proxy_active": bool(PROXIES),
        "rates": RATES_CACHE["rates"],
        "errors": LOTS_CACHE["errors"],
        "updated_at": int(LOTS_CACHE["timestamp"]),
        "lots": LOTS_CACHE["data"],
    }


@app.get("/api/auth")
def api_auth(x_funpay_key: Optional[str] = Header(None)):
    sess = get_fp_session(x_funpay_key)
    return {
        "authenticated": True,
        "user_id": sess["user_id"],
        "username": sess["username"],
    }


@app.get("/api/chats")
def api_chats(x_funpay_key: Optional[str] = Header(None)):
    get_fp_session(x_funpay_key)
    resp = fp_request("GET", "https://funpay.com/chat/", cookies={"golden_key": x_funpay_key})
    soup = BeautifulSoup(resp.text, "lxml")
    chats = []

    for c in soup.select("a.contact-item"):
        node_id = c.get("data-id")
        name_el = c.select_one(".media-user-name")
        msg_el = c.select_one(".contact-item-message")
        time_el = c.select_one(".contact-item-time")
        unread = "unread" in c.get("class", [])

        chats.append({
            "node_id": node_id,
            "name": name_el.get_text(strip=True) if name_el else "Чат",
            "last_message": msg_el.get_text(" ", strip=True) if msg_el else "",
            "time": time_el.get_text(strip=True) if time_el else "",
            "unread": unread,
        })
    return {"chats": chats}


@app.get("/api/chats/{node_id}")
def api_chat_messages(node_id: str, x_funpay_key: Optional[str] = Header(None)):
    sess = get_fp_session(x_funpay_key)
    resp = fp_request("GET", f"https://funpay.com/chat/?node={node_id}", cookies={"golden_key": x_funpay_key})
    soup = BeautifulSoup(resp.text, "lxml")
    messages = []

    real_node = soup.select_one(".chat")
    actual_node_id = real_node.get("data-id") if real_node and real_node.get("data-id") else node_id

    for m in soup.select(".chat-msg-item"):
        author_el = m.select_one(".chat-msg-author-link")
        text_el = m.select_one(".chat-msg-text")
        date_el = m.select_one(".chat-msg-date")

        author = author_el.get_text(strip=True) if author_el else ""
        is_me = (author == sess["username"])
        text = text_el.get_text("\n", strip=True) if text_el else ""
        date = date_el.get_text(strip=True) if date_el else ""

        if text:
            messages.append({
                "id": m.get("id", ""),
                "author": author,
                "is_me": is_me,
                "text": text,
                "time": date,
            })

    return {"node_id": actual_node_id, "messages": messages[-40:]}


@app.post("/api/chats/send")
def api_send_message(req: SendMessageReq, x_funpay_key: Optional[str] = Header(None)):
    sess = get_fp_session(x_funpay_key)

    payload = {
        "objects": json.dumps([{
            "type": "chat_node",
            "id": req.node_id,
            "tag": "0",
            "data": {"node": req.node_id, "last_message": -1, "content": ""},
        }]),
        "request": json.dumps({
            "action": "chat_message",
            "data": {
                "node": req.node_id,
                "last_message": -1,
                "content": req.text,
            },
        }),
        "csrf_token": sess["csrf"],
    }

    send_headers = {
        "x-requested-with": "XMLHttpRequest",
        "origin": "https://funpay.com",
        "referer": f"https://funpay.com/chat/?node={req.node_id}",
    }

    resp = fp_request(
        "POST",
        "https://funpay.com/runner/",
        headers=send_headers,
        cookies={"golden_key": x_funpay_key},
        data=payload,
    )

    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail="Не удалось отправить сообщение на FunPay")

    return {"ok": True}
