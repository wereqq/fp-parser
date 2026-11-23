import json
import re
import time
from typing import Optional
import requests
from bs4 import BeautifulSoup
from fastapi import FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel

app = FastAPI(title="FP // PARSER")

# Разрешаем CORS на случай, если позже вынесешь фронтенд на Netlify
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
}

# ID разделов FunPay (Аккаунты)
GAMES = {
    "dota2": {"name": "Dota 2", "url": "https://funpay.com/lots/81/"},
    "valorant": {"name": "Valorant", "url": "https://funpay.com/lots/612/"},
}

# Кэш лотов (обновляется раз в 60 секунд) и сессий пользователей
LOTS_CACHE = {"timestamp": 0, "data": []}
USER_SESSIONS = {}  # golden_key -> {"csrf": str, "user_id": int, "username": str, "ts": float}


class SendMessageReq(BaseModel):
    node_id: str
    text: str


def parse_game_lots(game_key: str, url: str) -> list:
    lots = []
    try:
        resp = requests.get(url, headers=HEADERS, timeout=15)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        for item in soup.select("a.tc-item"):
            href = item.get("href", "")
            lot_id_match = re.search(r"id=(\d+)", href)
            lot_id = lot_id_match.group(1) if lot_id_match else href

            desc_el = item.select_one(".tc-desc-text")
            desc = desc_el.get_text(" ", strip=True) if desc_el else "Без описания"

            # Собираем доп. теги (сервер, MMR, ранг и т.д.)
            tags = []
            for col in item.select(".tc-server, .tc-side, .tc-amount"):
                txt = col.get_text(" ", strip=True)
                if txt:
                    tags.append(txt)

            seller_el = item.select_one(".media-user-name")
            seller = seller_el.get_text(strip=True) if seller_el else "Продавец"

            # Достаем ID продавца, чтобы можно было сразу открыть чат
            seller_id = None
            avatar_el = item.select_one(".avatar-photo")
            if avatar_el and avatar_el.get("data-href"):
                m = re.search(r"/users/(\d+)/", avatar_el["data-href"])
                if m:
                    seller_id = int(m.group(1))

            reviews_el = item.select_one(".rating-mini-count")
            reviews = reviews_el.get_text(strip=True) if reviews_el else "0"

            price_el = item.select_one(".tc-price")
            price_val = 0.0
            price_str = "—"
            if price_el:
                price_str = price_el.get_text(" ", strip=True)
                try:
                    price_val = float(price_el.get("data-s", 0))
                except ValueError:
                    price_val = 0.0

            online = item.get("data-online") == "1"

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
                "online": online,
                "url": href if href.startswith("http") else f"https://funpay.com{href}",
            })
    except Exception as e:
        print(f"[Ошибка парсинга {game_key}]: {e}")
    return lots


def get_fp_session(golden_key: str) -> dict:
    if not golden_key:
        raise HTTPException(status_code=401, detail="Не передан golden_key")

    cached = USER_SESSIONS.get(golden_key)
    if cached and (time.time() - cached["ts"] < 600):
        return cached

    resp = requests.get(
        "https://funpay.com/",
        headers=HEADERS,
        cookies={"golden_key": golden_key},
        timeout=15,
    )
    soup = BeautifulSoup(resp.text, "lxml")
    body = soup.find("body")
    if not body or not body.get("data-app-data"):
        raise HTTPException(status_code=401, detail="FunPay не вернул данные сессии (возможна капча или неверный ключ)")

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
    now = time.time()
    if force or (now - LOTS_CACHE["timestamp"] > 60) or not LOTS_CACHE["data"]:
        all_lots = []
        for g_key, g_info in GAMES.items():
            all_lots.extend(parse_game_lots(g_key, g_info["url"]))
        LOTS_CACHE["data"] = all_lots
        LOTS_CACHE["timestamp"] = now

    return {
        "count": len(LOTS_CACHE["data"]),
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
    resp = requests.get(
        "https://funpay.com/chat/",
        headers=HEADERS,
        cookies={"golden_key": x_funpay_key},
        timeout=15,
    )
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
    resp = requests.get(
        f"https://funpay.com/chat/?node={node_id}",
        headers=HEADERS,
        cookies={"golden_key": x_funpay_key},
        timeout=15,
    )
    soup = BeautifulSoup(resp.text, "lxml")
    messages = []

    # Находим реальный числовой ID ноды, если передали users-X-Y
    chat_header = soup.select_one(".chat-header")
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
        **HEADERS,
        "x-requested-with": "XMLHttpRequest",
        "origin": "https://funpay.com",
        "referer": f"https://funpay.com/chat/?node={req.node_id}",
    }

    resp = requests.post(
        "https://funpay.com/runner/",
        headers=send_headers,
        cookies={"golden_key": x_funpay_key},
        data=payload,
        timeout=15,
    )

    if resp.status_code != 200:
        raise HTTPException(status_code=500, detail="Не удалось отправить сообщение на FunPay")

    return {"ok": True}
