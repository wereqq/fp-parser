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
    "dota2": {"name": "Dota 2", "url": "https://funpay.com/lots/81/"},
    "valorant": {"name": "Valorant", "url": "https://funpay.com/lots/612/"},
}

LOTS_CACHE = {"timestamp": 0, "data": [], "errors": {}, "updating": False}
USER_SESSIONS = {}


class SendMessageReq(BaseModel):
    node_id: str
    text: str


def fp_request(method: str, url: str, **kwargs) -> requests.Response:
    headers = {**HEADERS, **kwargs.pop("headers", {})}
    return requests.request(
        method=method,
        url=url,
        headers=headers,
        proxies=PROXIES,
        timeout=kwargs.pop("timeout", (5, 10)),
        **kwargs,
    )


def parse_game_lots(game_key: str, url: str) -> tuple[str, list, Optional[str]]:
    lots = []
    try:
        resp = fp_request("GET", url)
        resp.raise_for_status()

        tree = lxml_html.fromstring(resp.content)
        items = tree.xpath('//a[contains(concat(" ", normalize-space(@class), " "), " tc-item ")]')[:120]

        for item in items:
            href = item.get("href", "")
            lot_id_match = re.search(r"id=(\d+)", href)
            lot_id = lot_id_match.group(1) if lot_id_match else href

            desc_nodes = item.xpath('.//*[contains(@class, "tc-desc-text")]')
            desc = " ".join(desc_nodes[0].text_content().split()) if desc_nodes else "Без описания"

            tags = []
            for col in item.xpath('.//*[contains(@class, "tc-server") or contains(@class, "tc-side") or contains(@class, "tc-amount")]'):
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

            price_nodes = item.xpath('.//*[contains(@class, "tc-price")]')
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

        del resp, tree, items
        gc.collect()
        return game_key, lots, None
    except Exception as e:
        return game_key, [], str(e)


def refresh_cache_sync():
    if LOTS_CACHE["updating"]:
        return
    LOTS_CACHE["updating"] = True
    try:
        all_lots = []
        errors = {}
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(parse_game_lots, k, v["url"]) for k, v in GAMES.items()]
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