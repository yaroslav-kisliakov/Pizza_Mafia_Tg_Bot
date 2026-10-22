import asyncio
import json
import logging
import os
import re
from collections import Counter
from datetime import datetime, timedelta
from typing import Optional

import aiohttp
import aiosqlite
from aiogram import Bot, Dispatcher, F, types
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import (
    BotCommand,
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)

TOKEN = os.getenv("TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", 0))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("pizza_mafia")

DB_FILE = os.getenv("DB_FILE", "pizza_mafia.db")

# --- ВНЕШНИЕ ИНТЕГРАЦИИ (все опциональны, по умолчанию выключены) ---
GOOGLE_SHEETS_WEBHOOK = "https://script.google.com/macros/s/AKfycbxikARrkCY0QnakBRMs3fMFKUCkTwf8faBFIls2AAYCPcPEw0WQqHRK04wezMpS5jYH/exec"
KITCHEN_WEBHOOK = os.getenv("KITCHEN_WEBHOOK", "").strip()
PAYMENT_PROVIDER = (
    os.getenv("PAYMENT_PROVIDER", "").strip().lower()
)  # "" | "bepaid" | "webpay"
PAYMENT_SHOP_ID = os.getenv("PAYMENT_SHOP_ID", "").strip()
PAYMENT_SECRET = os.getenv("PAYMENT_SECRET", "").strip()

# --- КОНСТАНТЫ БИЗНЕС-ЛОГИКИ ---
DELIVERY_FEE = 0.0
MIN_ORDER_SUM = 15.0
MAX_ITEM_QTY = 20
ORDER_STATUSES = {
    "new": "🆕 Новый",
    "accepted": "✅ Принят",
    "cooking": "👨‍🍳 Готовится",
    "delivering": "🚗 В доставке",
    "done": "🎉 Доставлен",
    "canceled": "❌ Отменён",
}

dp = Dispatcher()


# --- FSM ---
class OrderSteps(StatesGroup):
    waiting_for_address = State()
    waiting_for_phone = State()
    waiting_for_comment = State()
    waiting_for_promo = State()
    waiting_for_payment = State()
    waiting_for_confirm = State()


class BroadcastState(StatesGroup):
    waiting_for_text = State()
    waiting_for_confirm = State()


class AdminMenuStates(StatesGroup):
    waiting_for_price_item = State()
    waiting_for_new_price = State()
    waiting_for_toggle_item = State()
    waiting_for_promo_code = State()
    waiting_for_promo_discount = State()
    waiting_for_promo_deactivate = State()
    waiting_for_order_status_id = State()
    waiting_for_order_status_value = State()


class ReviewState(StatesGroup):
    waiting_for_rating = State()
    waiting_for_text = State()


class PromoCheckState(StatesGroup):
    waiting_for_code = State()


# --- МЕНЮ ---
MENU_DATA = {
    "pizza": {
        "title": "🍕 Пицца",
        "items": {
            "p_ostraya": {
                "name": "Пицца «Острая»",
                "price": 22.50,
                "desc": "Соус чесночный, грудинка, пепперони, перец сладкий, халапеньо.",
            },
            "p_myasnaya": {
                "name": "Пицца «Мясная»",
                "price": 26.50,
                "desc": "Пепперони, ветчина, грудинка, фарш говяжий, моцарелла.",
            },
            "p_italy": {
                "name": "Пицца «Итальянская»",
                "price": 23.50,
                "desc": "Пицца-соус, пепперони, шампиньоны, грудинка, моцарелла.",
            },
            "p_pep": {
                "name": "Пицца «Пепперони»",
                "price": 23.50,
                "desc": "Пицца-соус, моцарелла, много пепперони.",
            },
            "p_bbq": {
                "name": "Пицца «Барбекю»",
                "price": 23.50,
                "desc": "Пицца-соус, колбаски барбекю, грудинка, лук, соус BBQ.",
            },
            "p_4seasons": {
                "name": "Пицца «Четыре сезона»",
                "price": 25.50,
                "desc": "Соус чесночный, пепперони, ветчина, шампиньоны, филе куриное.",
            },
            "p_mafia": {
                "name": "Пицца «Мафия»",
                "price": 23.50,
                "desc": "Соус-гриль, лук, фарш говяжий, грудинка, салат айсберг.",
            },
            "p_firm": {
                "name": "Пицца «Фирменная»",
                "price": 23.50,
                "desc": "Пицца-соус, пепперони, ветчина, перец сладкий, помидоры.",
            },
            "p_4cheese": {
                "name": "Пицца «Четыре сыра»",
                "price": 21.50,
                "desc": "Соус сырный, моцарелла, фета, пармезан.",
            },
            "p_chiz": {
                "name": "Пицца «Чизбургер»",
                "price": 22.50,
                "desc": "Соус сырный, грудинка, фарш говяжий, помидоры, огурец маринованный.",
            },
        },
    },
    "burrito": {
        "title": "🌯 Буррито",
        "items": {
            "b_chili": {
                "name": "Буррито «Чили» 🌶",
                "price": 8.00,
                "desc": "Моцарелла, салат айсберг, огурец, острый соус чили.",
            },
            "b_grill": {
                "name": "Буррито-гриль",
                "price": 8.00,
                "desc": "Соус-гриль, салат айсберг, помидор, мясной фарш.",
            },
            "b_chicken": {
                "name": "Буррито «Чикен»",
                "price": 8.00,
                "desc": "Соус цезарь, моцарелла, помидор, куриное филе.",
            },
            "b_bavar": {
                "name": "Буррито «По-баварски» 🔝",
                "price": 8.00,
                "desc": "Соус горчичный, моцарелла, пикантные колбаски.",
            },
        },
    },
    "snacks": {
        "title": "🍟 Закуски",
        "items": {
            "s_hash": {
                "name": "Хашбраун (15 шт)",
                "price": 12.50,
                "desc": "Хрустящие картофельные оладушки с луком.",
            },
            "s_draniki": {
                "name": "Драники со сметаной (200 г)",
                "price": 10.50,
                "desc": "Традиционные драники со сметаной.",
            },
            "s_wings_bbq": {
                "name": "Крылья «Барбекю»",
                "price": 14.50,
                "desc": "Куриные крылышки в соусе BBQ.",
            },
            "s_wings_hot": {
                "name": "Крылья острые 🌶",
                "price": 14.50,
                "desc": "Огненные крылышки для любителей погорячее.",
            },
            "s_nuggets": {
                "name": "Куриные наггетсы",
                "price": 9.50,
                "desc": "Хрустящие наггетсы из куриного филе.",
            },
            "s_fri": {
                "name": "Картофель фри",
                "price": 7.50,
                "desc": "Классический золотистый картофель фри.",
            },
            "s_rings": {
                "name": "Кольца луковые (12 шт)",
                "price": 10.00,
                "desc": "Хрустящие луковые кольца.",
            },
            "s_country": {
                "name": "Картофель «По-деревенски»",
                "price": 7.50,
                "desc": "Ароматные картофельные дольки со специями.",
            },
        },
    },
    "drinks": {
        "title": "🥤 Напитки",
        "items": {
            "d_bon_negaz": {
                "name": "BonAqua негазированная",
                "price": 2.10,
                "desc": "Питьевая вода без газа.",
            },
            "d_bon_sred": {
                "name": "BonAqua среднегазированная",
                "price": 2.70,
                "desc": "Вода средней газации.",
            },
            "d_bon_gaz": {
                "name": "BonAqua сильногазированная",
                "price": 2.10,
                "desc": "Сильногазированная вода.",
            },
            "d_hell": {
                "name": "Энергетик HELL",
                "price": 3.70,
                "desc": "Классический вкус энергии.",
            },
        },
    },
}


# ============================================================
# БАЗА ДАННЫХ
# ============================================================


async def db_init():
    async with aiosqlite.connect(DB_FILE) as db:
        await db.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                first_name TEXT,
                username TEXT,
                phone TEXT,
                address TEXT,
                consent INTEGER DEFAULT 0,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS user_cart (
                user_id INTEGER,
                item_id TEXT,
                qty INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS products (
                item_id TEXT PRIMARY KEY,
                category TEXT,
                name TEXT,
                price REAL,
                desc TEXT,
                is_available INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS orders (
                order_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                items TEXT,
                total_price REAL,
                discount REAL DEFAULT 0,
                address TEXT,
                phone TEXT,
                comment TEXT,
                promo_code TEXT,
                status TEXT DEFAULT 'new',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS order_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                order_id INTEGER,
                item_id TEXT,
                name TEXT,
                price REAL,
                qty INTEGER
            );
            CREATE TABLE IF NOT EXISTS promo_codes (
                code TEXT PRIMARY KEY,
                discount_percent INTEGER,
                is_active INTEGER DEFAULT 1
            );
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                order_id INTEGER,
                rating INTEGER,
                text TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
            CREATE TABLE IF NOT EXISTS event_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER,
                event TEXT,
                payload TEXT,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            );
        """)

        cur = await db.execute("PRAGMA table_info(orders)")
        existing_cols = {row[1] for row in await cur.fetchall()}
        migrations = {
            "discount": "ALTER TABLE orders ADD COLUMN discount REAL DEFAULT 0",
            "comment": "ALTER TABLE orders ADD COLUMN comment TEXT",
            "promo_code": "ALTER TABLE orders ADD COLUMN promo_code TEXT",
            "status": "ALTER TABLE orders ADD COLUMN status TEXT DEFAULT 'new'",
        }
        for col, sql in migrations.items():
            if col not in existing_cols:
                await db.execute(sql)
                logger.info("Миграция: добавлена колонка orders.%s", col)

        await db.executescript("""
            CREATE INDEX IF NOT EXISTS idx_orders_user ON orders(user_id);
            CREATE INDEX IF NOT EXISTS idx_orders_status ON orders(status);
            CREATE INDEX IF NOT EXISTS idx_order_items_order ON order_items(order_id);
        """)
        await db.commit()
    await db_fill_initial_menu()


async def db_fill_initial_menu():
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT COUNT(*) FROM products")
        (cnt,) = await cur.fetchone()
        if cnt == 0:
            rows = []
            for cat_id, cat in MENU_DATA.items():
                for item_id, info in cat["items"].items():
                    rows.append(
                        (item_id, cat_id, info["name"], info["price"], info["desc"], 1)
                    )
            await db.executemany(
                "INSERT INTO products (item_id, category, name, price, desc, is_available) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                rows,
            )
            await db.commit()


async def log_event(user_id: int, event: str, payload: str = ""):
    try:
        async with aiosqlite.connect(DB_FILE) as db:
            await db.execute(
                "INSERT INTO event_log (user_id, event, payload) VALUES (?, ?, ?)",
                (user_id, event, payload),
            )
            await db.commit()
    except Exception as e:
        logger.warning("log_event failed: %s", e)


async def db_get_products(category: str, admin_mode: bool = False):
    async with aiosqlite.connect(DB_FILE) as db:
        if admin_mode:
            cur = await db.execute(
                "SELECT item_id, name, price, desc, is_available FROM products WHERE category=?",
                (category,),
            )
        else:
            cur = await db.execute(
                "SELECT item_id, name, price, desc, is_available FROM products "
                "WHERE category=? AND is_available=1",
                (category,),
            )
        rows = await cur.fetchall()
    return [
        {
            "item_id": r[0],
            "name": r[1],
            "price": r[2],
            "desc": r[3],
            "is_available": r[4],
        }
        for r in rows
    ]


async def db_get_all_categories():
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT DISTINCT category FROM products")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def db_get_product(item_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT name, price, desc, is_available FROM products WHERE item_id=?",
            (item_id,),
        )
        row = await cur.fetchone()
    if row:
        return {"name": row[0], "price": row[1], "desc": row[2], "is_available": row[3]}
    return None


async def db_update_price(item_id: str, price: float):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE products SET price=? WHERE item_id=?", (price, item_id)
        )
        await db.commit()


async def db_toggle_availability(item_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE products SET is_available = CASE WHEN is_available=1 THEN 0 ELSE 1 END "
            "WHERE item_id=?",
            (item_id,),
        )
        await db.commit()


async def db_upsert_user(user_id: int, first_name: str, username: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO users (user_id, first_name, username) VALUES (?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET first_name=excluded.first_name, username=excluded.username",
            (user_id, first_name, username),
        )
        await db.commit()


async def db_set_user_consent(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("UPDATE users SET consent=1 WHERE user_id=?", (user_id,))
        await db.commit()


async def db_set_user_contact(user_id: int, phone: str, address: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE users SET phone=?, address=? WHERE user_id=?",
            (phone, address, user_id),
        )
        await db.commit()


async def db_get_user(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT first_name, username, phone, address, consent FROM users WHERE user_id=?",
            (user_id,),
        )
        row = await cur.fetchone()
    if row:
        return {
            "first_name": row[0],
            "username": row[1],
            "phone": row[2],
            "address": row[3],
            "consent": row[4],
        }
    return None


async def db_cart_add(user_id: int, item_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT qty FROM user_cart WHERE user_id=? AND item_id=?",
            (user_id, item_id),
        )
        row = await cur.fetchone()
        if row:
            if row[0] >= MAX_ITEM_QTY:
                return False
            await db.execute(
                "UPDATE user_cart SET qty=qty+1 WHERE user_id=? AND item_id=?",
                (user_id, item_id),
            )
        else:
            await db.execute(
                "INSERT INTO user_cart (user_id, item_id, qty) VALUES (?, ?, 1)",
                (user_id, item_id),
            )
        await db.commit()
    return True


async def db_cart_get(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT item_id, qty FROM user_cart WHERE user_id=?", (user_id,)
        )
        return await cur.fetchall()


async def db_cart_clear(user_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("DELETE FROM user_cart WHERE user_id=?", (user_id,))
        await db.commit()


async def db_cart_remove_one(user_id: int, item_id: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT qty FROM user_cart WHERE user_id=? AND item_id=?",
            (user_id, item_id),
        )
        row = await cur.fetchone()
        if not row:
            return
        if row[0] <= 1:
            await db.execute(
                "DELETE FROM user_cart WHERE user_id=? AND item_id=?",
                (user_id, item_id),
            )
        else:
            await db.execute(
                "UPDATE user_cart SET qty=qty-1 WHERE user_id=? AND item_id=?",
                (user_id, item_id),
            )
        await db.commit()


async def db_order_create_full(
    user_id: int,
    total: float,
    discount: float,
    address: str,
    phone: str,
    comment: str,
    promo: str,
    items: list,  # list of dict: {"item_id","name","price","qty"}
) -> int:
    """Создаёт заказ + позиции order_items в одной транзакции."""
    items_text = "\n".join(
        f"{it['name']} ×{it['qty']} = {it['price'] * it['qty']:.2f} BYN" for it in items
    )
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute("BEGIN")
        try:
            cur = await db.execute(
                "INSERT INTO orders (user_id, items, total_price, discount, address, phone, comment, promo_code) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (user_id, items_text, total, discount, address, phone, comment, promo),
            )
            order_id = cur.lastrowid
            await db.executemany(
                "INSERT INTO order_items (order_id, item_id, name, price, qty) "
                "VALUES (?, ?, ?, ?, ?)",
                [
                    (order_id, it["item_id"], it["name"], it["price"], it["qty"])
                    for it in items
                ],
            )
            await db.commit()
            return order_id
        except Exception:
            await db.rollback()
            raise


async def db_order_set_status(order_id: int, status: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE orders SET status=? WHERE order_id=?", (status, order_id)
        )
        await db.commit()


async def db_order_get(order_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT user_id, items, total_price, discount, address, phone, comment, status "
            "FROM orders WHERE order_id=?",
            (order_id,),
        )
        row = await cur.fetchone()
    if not row:
        return None
    return {
        "user_id": row[0],
        "items": row[1],
        "total": row[2],
        "discount": row[3],
        "address": row[4],
        "phone": row[5],
        "comment": row[6],
        "status": row[7],
    }


async def db_order_get_items(order_id: int):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT item_id, name, price, qty FROM order_items WHERE order_id=?",
            (order_id,),
        )
        rows = await cur.fetchall()
    return [{"item_id": r[0], "name": r[1], "price": r[2], "qty": r[3]} for r in rows]


async def db_user_orders(user_id: int, limit: int = 5):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT order_id, items, total_price, status, created_at FROM orders "
            "WHERE user_id=? ORDER BY order_id DESC LIMIT ?",
            (user_id, limit),
        )
        return await cur.fetchall()


async def db_promo_get(code: str):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT discount_percent FROM promo_codes WHERE code=? AND is_active=1",
            (code.upper(),),
        )
        row = await cur.fetchone()
    return row[0] if row else None


async def db_promo_add(code: str, percent: int):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT OR REPLACE INTO promo_codes (code, discount_percent, is_active) VALUES (?, ?, 1)",
            (code.upper(), percent),
        )
        await db.commit()


async def db_promo_deactivate(code: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "UPDATE promo_codes SET is_active=0 WHERE code=?", (code.upper(),)
        )
        await db.commit()


async def db_promo_list():
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT code, discount_percent, is_active FROM promo_codes ORDER BY code"
        )
        return await cur.fetchall()


async def db_review_add(user_id: int, order_id: int, rating: int, text: str):
    async with aiosqlite.connect(DB_FILE) as db:
        await db.execute(
            "INSERT INTO reviews (user_id, order_id, rating, text) VALUES (?, ?, ?, ?)",
            (user_id, order_id, rating, text),
        )
        await db.commit()


async def db_stats():
    async with aiosqlite.connect(DB_FILE) as db:

        async def one(q, *a):
            cur = await db.execute(q, a)
            r = await cur.fetchone()
            return r[0] if r else 0

        users = await one("SELECT COUNT(*) FROM users")
        orders = await one("SELECT COUNT(*) FROM orders")
        revenue = await one(
            "SELECT COALESCE(SUM(total_price - discount),0) FROM orders WHERE status!='canceled'"
        )
        today = await one(
            "SELECT COUNT(*) FROM orders WHERE date(created_at)=date('now')"
        )
        avg = await one("SELECT COALESCE(AVG(rating),0) FROM reviews")
        new_orders = await one("SELECT COUNT(*) FROM orders WHERE status='new'")
    return {
        "users": users,
        "orders": orders,
        "revenue": revenue,
        "today": today,
        "avg_rating": avg,
        "new_orders": new_orders,
    }


async def db_get_all_users():
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute("SELECT user_id FROM users WHERE consent=1")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


# ============================================================
# УТИЛИТЫ
# ============================================================


def esc(text: Optional[str]) -> str:
    if not text:
        return ""
    for ch in ("_", "*", "`", "["):
        text = text.replace(ch, "\\" + ch)
    return str(text)


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def fmt_price(x: float) -> str:
    return f"{x:.2f} BYN"


def is_valid_phone(phone: str) -> bool:
    return bool(re.fullmatch(r"\+?\d{9,15}", phone))


async def http_post_json(url: str, payload: dict, timeout: int = 30) -> bool:
    """Универсальная отправка JSON на вебхук. Возвращает True при 2xx."""
    if not url:
        return False
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, json=payload, timeout=timeout, allow_redirects=True
            ) as resp:
                if 200 <= resp.status < 300:
                    return True
                logger.warning("Webhook %s вернул %s", url, resp.status)
                return False
    except Exception as e:
        logger.warning("Webhook %s ошибка: %s", url, str(e) or type(e).__name__)
        return False


async def _silent_notify(url: str, payload: dict, label: str):
    """Фоновая отправка, чтобы не блокировать бота."""
    ok = await http_post_json(url, payload)
    if ok:
        logger.info("%s отправлен (фон)", label)
    else:
        logger.warning("%s не доставлен (фон)", label)


async def notify_google_sheets(
    order_id: int,
    user_id: int,
    items_text: str,
    total: float,
    discount: float,
    address: str,
    phone: str,
    comment: str,
    promo: str,
    status: str,
) -> None:
    """Дублирование заказа в Google Sheets через Apps Script Web App."""
    if not GOOGLE_SHEETS_WEBHOOK:
        return
    payload = {
        "order_id": order_id,
        "user_id": user_id,
        "items": items_text,
        "total": total,
        "discount": discount,
        "address": address,
        "phone": phone,
        "comment": comment,
        "promo": promo,
        "status": status,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }
    # ЗАПУСКАЕМ В ФОНЕ, НЕ ЖДЕМ ОТВЕТА
    asyncio.create_task(
        _silent_notify(
            GOOGLE_SHEETS_WEBHOOK, payload, f"Заказ #{order_id} в Google Sheets"
        )
    )


async def notify_kitchen(
    order_id: int, items_text: str, address: str, phone: str, comment: str
) -> None:
    """Отправка заказа на кухню (KDS / принтер) через вебхук."""
    if not KITCHEN_WEBHOOK:
        return
    payload = {
        "order_id": order_id,
        "items": items_text,
        "address": address,
        "phone": phone,
        "comment": comment,
        "ts": datetime.now().isoformat(),
    }
    ok = await http_post_json(KITCHEN_WEBHOOK, payload)
    if ok:
        logger.info("Заказ #%s отправлен на кухню", order_id)


def build_escpos_receipt(
    order_id: int, items_text: str, total: float, address: str, phone: str, comment: str
) -> str:
    """Формирует текстовый чек для ESC/POS-принтера.

    Возвращает строку с управляющими символами ESC/POS:
    - ESC @ (сброс)
    - ESC a 1 (по центру)
    - GS V 0 (обрезка)
    Отправка на принтер — через сокет 9100 (TODO в notify_kitchen).
    """
    ESC = "\x1b"
    GS = "\x1d"
    lines = [
        f"{ESC}@{ESC}a1",  # init + center
        "PIZZA MAFIA\n",
        f"Заказ #{order_id}\n",
        "------------------------\n",
        f"{ESC}a0",  # left
        items_text + "\n",
        "------------------------\n",
        f"ИТОГО: {total:.2f} BYN\n",
        f"Адрес: {address}\n",
        f"Тел: {phone}\n",
    ]
    if comment:
        lines.append(f"Коммент: {comment}\n")
    lines.append(f"{GS}V\x00")  # cut
    return "".join(lines)


def payment_available() -> bool:
    return bool(PAYMENT_PROVIDER and PAYMENT_SHOP_ID and PAYMENT_SECRET)


async def payment_create(order_id: int, amount: float) -> Optional[str]:
    """Создание платежа. Возвращает URL для оплаты или None.

    Сейчас — заглушка. Реальные провайдеры подключаются здесь.
    """
    if not payment_available():
        return None
    # TODO: bePaid — POST https://checkout.bepaid.by/ctp/api/checkouts
    # TODO: WebPay — POST https://securesandbox.webpay.by/api/payment
    # TODO: ЕРИП — через агрегатор (bePaid/WebPay)
    logger.warning(
        "PAYMENT_PROVIDER=%s, но интеграция не реализована", PAYMENT_PROVIDER
    )
    return None


# ============================================================
# КЛАВИАТУРЫ
# ============================================================


def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(text="🍕 Меню"), KeyboardButton(text="🛒 Корзина")],
        [
            KeyboardButton(text="📦 Мои заказы"),
            KeyboardButton(text="🎁 Ввести промокод"),
        ],
        [KeyboardButton(text="⭐ Оставить отзыв"), KeyboardButton(text="🕒 Доставка")],
    ]
    if is_admin(user_id):
        rows.append([KeyboardButton(text="🛠 Панель Управления")])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True)


categories_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="🍕 Пицца"), KeyboardButton(text="🌯 Буррито")],
        [KeyboardButton(text="🍟 Закуски"), KeyboardButton(text="🥤 Напитки")],
        [KeyboardButton(text="⬅️ Назад")],
    ],
    resize_keyboard=True,
)

cancel_kb = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="❌ Отмена")]], resize_keyboard=True
)

share_phone_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
)

skip_comment_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⏭ Пропустить")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
)

skip_promo_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="⏭ Пропустить")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
)

payment_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="💵 Наличными курьеру")],
        [KeyboardButton(text="💳 Картой курьеру")],
        [KeyboardButton(text="❌ Отмена")],
    ],
    resize_keyboard=True,
)

admin_main_kb = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="📊 Статистика"), KeyboardButton(text="📥 Новые заказы")],
        [KeyboardButton(text="✏️ Изменить цену"), KeyboardButton(text="❌ Стоп-лист")],
        [KeyboardButton(text="🎁 Промокоды"), KeyboardButton(text="📢 Рассылка")],
        [KeyboardButton(text="⬅️ Выйти из админки")],
    ],
    resize_keyboard=True,
)


def item_kb(item_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🛒 В корзину", callback_data=f"add_{item_id}")]
        ]
    )


def category_items_kb(products) -> InlineKeyboardMarkup:
    rows = []
    for p in products:
        name = p["name"]
        if len(name) > 30:
            name = name[:28] + "…"
        rows.append(
            [
                InlineKeyboardButton(
                    text=f"➕ {name} — {fmt_price(p['price'])}",
                    callback_data=f"add_{p['item_id']}",
                )
            ]
        )
    return InlineKeyboardMarkup(inline_keyboard=rows)


def cart_kb(cart_rows) -> InlineKeyboardMarkup:
    rows = []
    # Показываем максимум 15 позиций в виде кнопок, чтобы не превысить лимиты Telegram
    for item_id, qty in cart_rows[:15]:
        rows.append(
            [
                InlineKeyboardButton(text="➖", callback_data=f"dec_{item_id}"),
                InlineKeyboardButton(text=f"{qty} шт", callback_data="noop"),
                InlineKeyboardButton(text="➕", callback_data=f"add_{item_id}"),
                InlineKeyboardButton(text="🗑", callback_data=f"del_{item_id}"),
            ]
        )

    # Если позиций больше 15, выводим предупреждение в виде кнопки-заглушки
    if len(cart_rows) > 15:
        rows.append(
            [
                InlineKeyboardButton(
                    text="⚠️ Показаны не все товары (макс. 15)", callback_data="noop"
                )
            ]
        )

    rows.append(
        [InlineKeyboardButton(text="🚀 Оформить заказ", callback_data="checkout")]
    )
    rows.append(
        [InlineKeyboardButton(text="🧹 Очистить корзину", callback_data="clear_cart")]
    )
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def build_admin_products_kb(prefix: str) -> Optional[InlineKeyboardMarkup]:
    rows = []
    for cat in await db_get_all_categories():
        for item in await db_get_products(cat, admin_mode=True):
            icon = "🟢" if item["is_available"] == 1 else "🔴"
            rows.append(
                [
                    InlineKeyboardButton(
                        text=f"{item['name']} — {fmt_price(item['price'])} {icon}",
                        callback_data=f"{prefix}_{item['item_id']}",
                    )
                ]
            )
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def order_actions_kb(order_id: int, status: str) -> Optional[InlineKeyboardMarkup]:
    next_statuses = {
        "new": ["accepted", "canceled"],
        "accepted": ["cooking", "canceled"],
        "cooking": ["delivering", "canceled"],
        "delivering": ["done"],
        "done": [],
        "canceled": [],
    }.get(status, [])
    if not next_statuses:
        return None
    row = [
        InlineKeyboardButton(
            text=ORDER_STATUSES[st], callback_data=f"ostatus_{order_id}_{st}"
        )
        for st in next_statuses
    ]
    return InlineKeyboardMarkup(inline_keyboard=[row])


async def _load_products_map(rows):
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT item_id, name, price, desc, is_available FROM products"
        )
        all_rows = await cur.fetchall()
    mapping = {
        r[0]: {
            "item_id": r[0],
            "name": r[1],
            "price": r[2],
            "desc": r[3],
            "is_available": r[4],
        }
        for r in all_rows
    }
    return {iid: mapping[iid] for iid, _ in rows if iid in mapping}


async def build_cart_items(user_id: int):
    """Возвращает (items_list, total, lines) для текущей корзины."""
    rows = await db_cart_get(user_id)
    if not rows:
        return [], 0.0, []
    products_map = await _load_products_map(rows)
    items = []
    total = 0.0
    lines = []
    for item_id, qty in rows:
        p = products_map.get(item_id)
        if not p or p["is_available"] != 1:
            continue
        s = p["price"] * qty
        total += s
        items.append(
            {"item_id": item_id, "name": p["name"], "price": p["price"], "qty": qty}
        )
        lines.append(f"• {esc(p['name'])} ×{qty} — {fmt_price(s)}")
    return items, total, lines


# ============================================================
# КЛИЕНТСКИЕ ХЭНДЛЕРЫ
# ============================================================


@dp.message(CommandStart())
async def start_cmd(message: Message, state: FSMContext):
    await state.clear()
    await db_upsert_user(
        message.from_user.id,
        message.from_user.first_name or "",
        message.from_user.username or "",
    )
    await log_event(message.from_user.id, "start")

    user = await db_get_user(message.from_user.id)
    if not user or not user["consent"]:
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="✅ Согласен", callback_data="consent_yes")]
            ]
        )
        await message.answer(
            "Привет! 🍕 Добро пожаловать в **Пицца Мафия**.\n\n"
            "Перед началом подтвердите согласие на обработку персональных данных "
            "(имя, телефон, адрес доставки) — это требуется по закону №99-З.",
            reply_markup=kb,
            parse_mode="Markdown",
        )
        return

    await message.answer(
        f"Привет, {message.from_user.first_name}! 🍕\n\n"
        "Заказывайте пиццу прямо в Telegram — без звонков и ожидания на линии.",
        reply_markup=main_menu_kb(message.from_user.id),
    )


@dp.callback_query(F.data == "consent_yes")
async def consent_yes(callback: CallbackQuery):
    await db_set_user_consent(callback.from_user.id)
    try:
        await callback.message.edit_text("✅ Спасибо! Согласие получено.")
    except Exception:
        pass
    await callback.message.answer(
        "Выберите действие 👇", reply_markup=main_menu_kb(callback.from_user.id)
    )
    await callback.answer()


@dp.message(F.text == "🍕 Меню")
async def show_menu(message: Message):
    await log_event(message.from_user.id, "open_menu")
    await message.answer("Выберите категорию 👇", reply_markup=categories_kb)


async def show_category(message: Message, cat: str):
    products = await db_get_products(cat, admin_mode=False)
    if not products:
        await message.answer("⚠️ Раздел временно пуст.")
        return
    title = MENU_DATA.get(cat, {}).get("title", cat)
    text_lines = [f"✨ **{esc(title)}** ✨\n"]
    for p in products:
        text_lines.append(
            f"**{esc(p['name'])}** — {fmt_price(p['price'])}\n_{esc(p['desc'])}_"
        )
    await message.answer(
        "\n\n".join(text_lines),
        reply_markup=category_items_kb(products),
        parse_mode="Markdown",
    )


@dp.message(F.text == "🍕 Пицца")
async def cat_pizza(m: Message):
    await show_category(m, "pizza")


@dp.message(F.text == "🌯 Буррито")
async def cat_burrito(m: Message):
    await show_category(m, "burrito")


@dp.message(F.text == "🍟 Закуски")
async def cat_snacks(m: Message):
    await show_category(m, "snacks")


@dp.message(F.text == "🥤 Напитки")
async def cat_drinks(m: Message):
    await show_category(m, "drinks")


@dp.callback_query(F.data.startswith("add_"))
async def cb_add(callback: CallbackQuery):
    item_id = callback.data[4:]
    p = await db_get_product(item_id)
    if not p or p["is_available"] != 1:
        await callback.answer("❌ Товар недоступен", show_alert=True)
        return

    ok = await db_cart_add(callback.from_user.id, item_id)
    await log_event(callback.from_user.id, "cart_add", item_id)

    if not ok:
        await callback.answer(f"⚠️ Максимум {MAX_ITEM_QTY} шт", show_alert=True)
        return

    await callback.answer(f"✅ {p['name']}")

    # Если это нажатие произошло внутри корзины — перерисовываем сообщение
    if callback.message.text and callback.message.text.startswith("🛒 **Ваш заказ:**"):
        await render_cart(callback.message, callback.from_user.id, edit=True)


@dp.callback_query(F.data.startswith("dec_"))
async def cb_dec(callback: CallbackQuery):
    item_id = callback.data[4:]
    await db_cart_remove_one(callback.from_user.id, item_id)
    await callback.answer("➖")
    await render_cart(callback.message, callback.from_user.id, edit=True)


@dp.callback_query(F.data.startswith("del_"))
async def cb_del(callback: CallbackQuery):
    item_id = callback.data[4:]
    rows = await db_cart_get(callback.from_user.id)
    for iid, qty in rows:
        if iid == item_id:
            for _ in range(qty):
                await db_cart_remove_one(callback.from_user.id, item_id)
    await callback.answer("🗑 Удалено")
    await render_cart(callback.message, callback.from_user.id, edit=True)


@dp.callback_query(F.data == "clear_cart")
async def cb_clear_cart(callback: CallbackQuery):
    await db_cart_clear(callback.from_user.id)
    await callback.answer("🧹 Очищено")
    try:
        await callback.message.edit_text("Корзина очищена.", reply_markup=None)
    except Exception:
        await callback.message.answer("Корзина очищена.")


@dp.callback_query(F.data == "noop")
async def cb_noop(callback: CallbackQuery):
    await callback.answer()


@dp.message(F.text == "🛒 Корзина")
async def show_cart(message: Message):
    await render_cart(message, message.from_user.id, edit=False)


async def render_cart(message: Message, user_id: int, edit: bool):
    items, total, lines = await build_cart_items(user_id)
    if not items:
        text = "🛒 Корзина пуста. Загляните в меню!"
        if edit:
            try:
                await message.edit_text(text)
                return
            except Exception:
                pass
        await message.answer(text)
        return

    lines.insert(0, "🛒 **Ваш заказ:**\n")
    lines.append(f"\n💰 **Итого: {fmt_price(total)}**")
    if total < MIN_ORDER_SUM:
        lines.append(f"⚠️ Минимальная сумма заказа — {fmt_price(MIN_ORDER_SUM)}")
    text = "\n".join(lines)
    kb = cart_kb([(it["item_id"], it["qty"]) for it in items])
    if edit:
        try:
            await message.edit_text(text, reply_markup=kb, parse_mode="Markdown")
            return
        except Exception:
            pass
    await message.answer(text, reply_markup=kb, parse_mode="Markdown")


# --- ПРОМОКОД ---


@dp.message(F.text == "🎁 Ввести промокод")
async def promo_check_start(message: Message, state: FSMContext):
    await message.answer(
        "Введите промокод, чтобы проверить его действие:", reply_markup=cancel_kb
    )
    await state.set_state(PromoCheckState.waiting_for_code)


@dp.message(PromoCheckState.waiting_for_code)
async def promo_check_code(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "Отменено.", reply_markup=main_menu_kb(message.from_user.id)
        )
        return
    if not message.text:
        await message.answer("Введите код текстом.")
        return
    code = message.text.upper().strip()
    percent = await db_promo_get(code)
    if percent is None:
        await message.answer(
            f"❌ Промокод `{esc(code)}` не найден или неактивен.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(message.from_user.id),
        )
    else:
        await message.answer(
            f"✅ Промокод `{esc(code)}` действует: скидка **{percent}%**.\n"
            "Он применится автоматически при оформлении заказа.",
            parse_mode="Markdown",
            reply_markup=main_menu_kb(message.from_user.id),
        )
    await state.clear()


# --- ПОВТОР ЗАКАЗА ---


@dp.message(F.text == "📦 Мои заказы")
async def my_orders(message: Message):
    orders = await db_user_orders(message.from_user.id, limit=5)
    if not orders:
        await message.answer("У вас пока нет заказов.")
        return
    for oid, items, total, status, created in orders:
        text = (
            f"**Заказ #{oid}** — {ORDER_STATUSES.get(status, status)}\n"
            f"_{esc(created)}_\n"
            f"{esc(items)}\n"
            f"💰 {fmt_price(total)}"
        )
        kb = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="🔁 Повторить", callback_data=f"repeat_{oid}"
                    )
                ]
            ]
        )
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data.startswith("repeat_"))
async def cb_repeat_order(callback: CallbackQuery):
    try:
        order_id = int(callback.data.split("_")[1])
    except (IndexError, ValueError):
        await callback.answer("Ошибка", show_alert=True)
        return
    order = await db_order_get(order_id)
    if not order or order["user_id"] != callback.from_user.id:
        await callback.answer("Заказ не найден", show_alert=True)
        return
    items = await db_order_get_items(order_id)
    if not items:
        await callback.answer("Состав заказа недоступен", show_alert=True)
        return

    await db_cart_clear(callback.from_user.id)
    added = 0
    skipped = []
    for it in items:
        p = await db_get_product(it["item_id"])
        if not p or p["is_available"] != 1:
            skipped.append(it["name"])
            continue
        for _ in range(it["qty"]):
            ok = await db_cart_add(callback.from_user.id, it["item_id"])
            if ok:
                added += 1
    await log_event(callback.from_user.id, "order_repeat", str(order_id))

    if added == 0:
        await callback.answer("Все товары из заказа недоступны", show_alert=True)
        return
    msg = f"🔁 В корзину добавлено позиций: {added}."
    if skipped:
        msg += f"\n⚠️ Недоступны: {', '.join(skipped)}"
    await callback.message.answer(msg, reply_markup=main_menu_kb(callback.from_user.id))
    await callback.answer()


# --- ОФОРМЛЕНИЕ ---


@dp.callback_query(F.data == "checkout")
async def start_checkout(callback: CallbackQuery, state: FSMContext):
    items, total, _ = await build_cart_items(callback.from_user.id)
    if not items:
        await callback.answer("Корзина пуста", show_alert=True)
        return
    if total < MIN_ORDER_SUM:
        await callback.answer(f"Минимум {fmt_price(MIN_ORDER_SUM)}", show_alert=True)
        return

    user = await db_get_user(callback.from_user.id)
    if user and user["address"]:
        await state.update_data(saved_address=user["address"])
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="✅ Использовать этот адрес")],
                [
                    KeyboardButton(text="✏️ Ввести другой"),
                    KeyboardButton(text="❌ Отмена"),
                ],
            ],
            resize_keyboard=True,
        )
        await callback.message.answer(
            f"Ваш сохранённый адрес:\n_{esc(user['address'])}_",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    else:
        await callback.message.answer(
            "📍 Введите адрес доставки (улица, дом, подъезд, этаж, кв.):",
            reply_markup=cancel_kb,
        )
    await state.set_state(OrderSteps.waiting_for_address)
    await callback.answer()


@dp.message(OrderSteps.waiting_for_address)
async def step_address(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "Отменено.", reply_markup=main_menu_kb(message.from_user.id)
        )
        return

    if message.text == "✅ Использовать этот адрес":
        data = await state.get_data()
        address = data.get("saved_address")
        if not address:
            await message.answer(
                "Адрес не найден. Введите вручную:", reply_markup=cancel_kb
            )
            return
    elif message.text == "✏️ Ввести другой":
        await message.answer("Введите новый адрес:", reply_markup=cancel_kb)
        return
    else:
        if not message.text or len(message.text.strip()) < 5:
            await message.answer("❌ Адрес слишком короткий. Введите подробнее:")
            return
        if len(message.text) > 500:
            await message.answer("❌ Слишком длинный адрес.")
            return
        address = message.text.strip()

    await state.update_data(address=address)
    user = await db_get_user(message.from_user.id)
    if user and user["phone"]:
        kb = ReplyKeyboardMarkup(
            keyboard=[
                [KeyboardButton(text="✅ Использовать сохранённый номер")],
                [KeyboardButton(text="📱 Поделиться номером", request_contact=True)],
                [KeyboardButton(text="❌ Отмена")],
            ],
            resize_keyboard=True,
        )
        await message.answer(
            f"Сохранённый номер: `{esc(user['phone'])}`",
            reply_markup=kb,
            parse_mode="Markdown",
        )
    else:
        await message.answer(
            "📱 Поделитесь номером телефона:", reply_markup=share_phone_kb
        )
    await state.set_state(OrderSteps.waiting_for_phone)


@dp.message(OrderSteps.waiting_for_phone)
async def step_phone(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "Отменено.", reply_markup=main_menu_kb(message.from_user.id)
        )
        return

    phone = None
    if message.text == "✅ Использовать сохранённый номер":
        user = await db_get_user(message.from_user.id)
        if user and user["phone"]:
            phone = user["phone"]
    elif message.contact:
        if message.contact.user_id and message.contact.user_id != message.from_user.id:
            await message.answer("⚠️ Отправьте свой контакт.")
            return
        phone = message.contact.phone_number

    if not phone or not is_valid_phone(phone.replace(" ", "")):
        await message.answer("⚠️ Введите корректный номер или нажмите кнопку.")
        return

    await state.update_data(phone=phone)
    await message.answer(
        "💬 Комментарий к заказу (или «⏭ Пропустить»):", reply_markup=skip_comment_kb
    )
    await state.set_state(OrderSteps.waiting_for_comment)


@dp.message(OrderSteps.waiting_for_comment)
async def step_comment(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "Отменено.", reply_markup=main_menu_kb(message.from_user.id)
        )
        return
    comment = "" if message.text == "⏭ Пропустить" else (message.text or "")[:300]
    await state.update_data(comment=comment)

    await message.answer(
        "🎁 Есть промокод? Введите его или нажмите «⏭ Пропустить».",
        reply_markup=skip_promo_kb,
    )
    await state.set_state(OrderSteps.waiting_for_promo)


@dp.message(OrderSteps.waiting_for_promo)
async def step_promo(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "Отменено.", reply_markup=main_menu_kb(message.from_user.id)
        )
        return

    if message.text and message.text != "⏭ Пропустить":
        code = message.text.upper().strip()
        percent = await db_promo_get(code)
        if percent is None:
            await message.answer(
                f"❌ Промокод `{esc(code)}` не найден или неактивен.\n"
                "Попробуйте другой или нажмите «⏭ Пропустить».",
                parse_mode="Markdown",
            )
            return
        await state.update_data(promo_code=code)
        await message.answer(f"✅ Промокод применён: −{percent}%")
    else:
        await state.update_data(promo_code="")

    # выбор способа оплаты
    await message.answer("💳 Выберите способ оплаты:", reply_markup=payment_kb)
    await state.set_state(OrderSteps.waiting_for_payment)


@dp.message(OrderSteps.waiting_for_payment)
async def step_payment(message: Message, state: FSMContext):
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer(
            "Отменено.", reply_markup=main_menu_kb(message.from_user.id)
        )
        return

    if message.text == "💵 Наличными курьеру":
        await state.update_data(payment_method="cash")
    elif message.text == "💳 Картой курьеру":
        await state.update_data(payment_method="card")
    else:
        await message.answer(
            "Выберите способ оплаты кнопкой ниже.", reply_markup=payment_kb
        )
        return

    await show_checkout_summary(message, state)


async def show_checkout_summary(message: Message, state: FSMContext):
    data = await state.get_data()
    items, total, lines = await build_cart_items(message.from_user.id)
    if not items:
        await message.answer(
            "Корзина пуста.", reply_markup=main_menu_kb(message.from_user.id)
        )
        await state.clear()
        return

    promo_code = data.get("promo_code", "")
    discount = 0.0
    if promo_code:
        percent = await db_promo_get(promo_code)
        if percent:
            discount = round(total * percent / 100, 2)
    final_total = round(total - discount + DELIVERY_FEE, 2)

    body = ["📋 **Проверьте заказ:**\n"]
    body.extend(lines)
    if promo_code:
        body.append(f"\n🎁 Промокод `{esc(promo_code)}`: −{fmt_price(discount)}")
    body.append(f"\n📍 Адрес: {esc(data.get('address', ''))}")
    body.append(f"📞 Телефон: {esc(data.get('phone', ''))}")
    comment = data.get("comment", "")
    if comment:
        body.append(f"💬 Комментарий: {esc(comment)}")
    pm = data.get("payment_method", "cash")
    pm_text = "💵 наличными" if pm == "cash" else "💳 картой (терминал)"
    body.append(f"\n💳 Оплата: {pm_text}")
    body.append(f"\n💰 **К оплате: {fmt_price(final_total)}**")

    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="✅ Подтвердить заказ", callback_data="confirm_order"
                )
            ],
            [InlineKeyboardButton(text="❌ Отменить", callback_data="cancel_order")],
        ]
    )
    await message.answer("\n".join(body), reply_markup=kb, parse_mode="Markdown")
    await state.set_state(OrderSteps.waiting_for_confirm)


@dp.callback_query(F.data == "cancel_order")
async def cb_cancel_order(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("❌ Заказ отменён.")
    except Exception:
        await callback.message.answer("❌ Заказ отменён.")
    await callback.message.answer(
        "Главное меню", reply_markup=main_menu_kb(callback.from_user.id)
    )
    await callback.answer()


@dp.callback_query(F.data == "confirm_order", OrderSteps.waiting_for_confirm)
async def cb_confirm_order(callback: CallbackQuery, state: FSMContext, bot: Bot):
    data = await state.get_data()
    address = data.get("address")
    phone = data.get("phone")
    comment = data.get("comment", "")
    promo_code = data.get("promo_code", "")
    payment_method = data.get("payment_method", "cash")

    if not address or not phone:
        await callback.answer("Данные потеряны, начните заново", show_alert=True)
        await state.clear()
        return

    items, total, _ = await build_cart_items(callback.from_user.id)
    if not items:
        await callback.answer("Корзина пуста", show_alert=True)
        await state.clear()
        return
    if total < MIN_ORDER_SUM:
        await callback.answer(f"Минимум {fmt_price(MIN_ORDER_SUM)}", show_alert=True)
        return

    discount = 0.0
    if promo_code:
        percent = await db_promo_get(promo_code)
        if percent:
            discount = round(total * percent / 100, 2)
    final_total = round(total - discount + DELIVERY_FEE, 2)

    try:
        order_id = await db_order_create_full(
            callback.from_user.id,
            final_total,
            discount,
            address,
            phone,
            comment,
            promo_code,
            items,
        )
    except Exception as e:
        logger.error("Не удалось создать заказ: %s", e)
        await callback.answer("Ошибка при создании заказа", show_alert=True)
        return

    await db_set_user_contact(callback.from_user.id, phone, address)
    await db_cart_clear(callback.from_user.id)
    await log_event(callback.from_user.id, "order_created", str(order_id))

    items_text = "\n".join(
        f"{it['name']} ×{it['qty']} = {fmt_price(it['price'] * it['qty'])}"
        for it in items
    )

    # Уведомление админу
    user = await db_get_user(callback.from_user.id)
    username = f"@{user['username']}" if user and user["username"] else "—"
    admin_text = (
        f"🚨 **НОВЫЙ ЗАКАЗ #{order_id}**\n\n"
        f"🛒 {esc(items_text)}\n\n"
        f"💰 Сумма: {fmt_price(total)}"
    )
    if discount:
        admin_text += (
            f"\n🎁 Скидка: −{fmt_price(discount)} (промокод `{esc(promo_code)}`)"
        )
    pm_admin_text = (
        "💵 наличными" if payment_method == "cash" else "💳 картой (терминал)"
    )
    admin_text += (
        f"\n💵 **К оплате: {fmt_price(final_total)}**\n"
        f"💳 Оплата: {pm_admin_text}\n\n"
        f"📍 {esc(address)}\n📞 {esc(phone)}\n"
        f"👤 {esc(user['first_name'] if user else '')} ({esc(username)})\n"
        f"🆔 `{callback.from_user.id}`"
    )
    if comment:
        admin_text += f"\n💬 {esc(comment)}"

    kb = order_actions_kb(order_id, "new")
    try:
        await bot.send_message(
            ADMIN_ID, admin_text, parse_mode="Markdown", reply_markup=kb
        )
    except Exception as e:
        logger.error("Не удалось отправить заказ админу: %s", e)

    # Google Sheets + кухня (опционально, если заданы env)
    await notify_google_sheets(
        order_id,
        callback.from_user.id,
        items_text,
        total,
        discount,
        address,
        phone,
        comment,
        promo_code,
        "new",
    )
    await notify_kitchen(order_id, items_text, address, phone, comment)

    # Онлайн-оплата (если выбрана и настроена)
    payment_url = None
    if payment_method == "online" and payment_available():
        payment_url = await payment_create(order_id, final_total)

    if payment_url:
        pkb = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="💳 Перейти к оплате", url=payment_url)]
            ]
        )
        try:
            await callback.message.edit_text(
                f"🎉 **Заказ #{order_id} создан!**\n\n"
                f"Сумма: {fmt_price(final_total)}\n"
                "Оплатите по кнопке ниже. После оплаты заказ уйдёт на кухню.",
                parse_mode="Markdown",
                reply_markup=pkb,
            )
        except Exception:
            pass
    else:
        try:
            await callback.message.edit_text(
                f"🎉 **Заказ #{order_id} принят!**\n\n"
                f"Сумма: {fmt_price(final_total)}\n"
                "Оператор свяжется с вами в течение 3 минут для подтверждения.\n\n"
                "Статус заказа можно смотреть в «📦 Мои заказы».",
                parse_mode="Markdown",
            )
        except Exception:
            pass

    await callback.message.answer(
        "Спасибо, что выбрали Пицца Мафия! 🍕",
        reply_markup=main_menu_kb(callback.from_user.id),
    )
    await state.clear()
    await callback.answer()


@dp.message(F.text == "🕒 Доставка")
async def delivery_info(message: Message):
    await message.answer(
        "🕒 **Доставка:**\n"
        "• Ежедневно 11:00–23:00\n"
        "• Пт, Сб — до 01:00 🌙\n"
        "• Время доставки: 45–60 мин\n"
        f"• Доставка: {'бесплатно' if DELIVERY_FEE == 0 else fmt_price(DELIVERY_FEE)}\n"
        f"• Минимальный заказ: {fmt_price(MIN_ORDER_SUM)}",
        parse_mode="Markdown",
    )


@dp.message(F.text == "⭐ Оставить отзыв")
async def review_start(message: Message, state: FSMContext):
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="1⭐", callback_data="rate_1"),
                InlineKeyboardButton(text="2⭐", callback_data="rate_2"),
                InlineKeyboardButton(text="3⭐", callback_data="rate_3"),
                InlineKeyboardButton(text="4⭐", callback_data="rate_4"),
                InlineKeyboardButton(text="5⭐", callback_data="rate_5"),
            ]
        ]
    )
    await message.answer("Оцените наш сервис:", reply_markup=kb)
    await state.set_state(ReviewState.waiting_for_rating)


@dp.callback_query(F.data.startswith("rate_"), ReviewState.waiting_for_rating)
async def review_rate(callback: CallbackQuery, state: FSMContext):
    rating = int(callback.data.split("_")[1])
    await state.update_data(rating=rating)
    try:
        await callback.message.edit_text(
            f"Оценка: {rating}⭐\n\nНапишите пару слов (или «-»):"
        )
    except Exception:
        await callback.message.answer(
            f"Оценка: {rating}⭐\n\nНапишите пару слов (или «-»):"
        )
    await state.set_state(ReviewState.waiting_for_text)
    await callback.answer()


@dp.message(ReviewState.waiting_for_text)
async def review_text(message: Message, state: FSMContext, bot: Bot):
    data = await state.get_data()
    rating = data.get("rating", 5)
    text = "" if message.text == "-" else (message.text or "")[:500]
    await db_review_add(message.from_user.id, 0, rating, text)
    await state.clear()
    await message.answer(
        "🙏 Спасибо за отзыв!", reply_markup=main_menu_kb(message.from_user.id)
    )
    try:
        await bot.send_message(
            ADMIN_ID,
            f"⭐ Новый отзыв: {rating}⭐\n👤 {esc(message.from_user.first_name)}\n💬 {esc(text) or '—'}",
            parse_mode="Markdown",
        )
    except Exception:
        pass


# ============================================================
# АДМИН-ПАНЕЛЬ
# ============================================================


@dp.message(Command("admin"))
@dp.message(F.text == "🛠 Панель Управления")
async def admin_panel(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer(
        "🛠 **Админ-панель**", reply_markup=admin_main_kb, parse_mode="Markdown"
    )


@dp.message(F.text == "⬅️ Выйти из админки")
async def admin_exit(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await state.clear()
    await message.answer("Выход.", reply_markup=main_menu_kb(message.from_user.id))


@dp.message(F.text == "📊 Статистика")
async def admin_stats(message: Message):
    if not is_admin(message.from_user.id):
        return
    s = await db_stats()
    await message.answer(
        f"📊 **Статистика**\n\n"
        f"👥 Пользователей: {s['users']}\n"
        f"📦 Заказов всего: {s['orders']}\n"
        f"🆕 Новых (не обработано): {s['new_orders']}\n"
        f"📅 Заказов сегодня: {s['today']}\n"
        f"💰 Выручка: {fmt_price(s['revenue'])}\n"
        f"⭐ Средний рейтинг: {s['avg_rating']:.1f}",
        parse_mode="Markdown",
        reply_markup=admin_main_kb,
    )


@dp.message(F.text == "📥 Новые заказы")
async def admin_new_orders(message: Message):
    if not is_admin(message.from_user.id):
        return
    async with aiosqlite.connect(DB_FILE) as db:
        cur = await db.execute(
            "SELECT order_id, user_id, items, total_price, address, phone, comment "
            "FROM orders WHERE status='new' ORDER BY order_id DESC LIMIT 10"
        )
        rows = await cur.fetchall()
    if not rows:
        await message.answer("Нет новых заказов.")
        return
    for oid, uid, items, total, addr, phone, comment in rows:
        text = (
            f"**Заказ #{oid}**\n{esc(items)}\n"
            f"💰 {fmt_price(total)}\n📍 {esc(addr)}\n📞 {esc(phone)}"
        )
        if comment:
            text += f"\n💬 {esc(comment)}"
        kb = order_actions_kb(oid, "new")
        await message.answer(text, parse_mode="Markdown", reply_markup=kb)


@dp.callback_query(F.data.startswith("ostatus_"))
async def cb_set_status(callback: CallbackQuery, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    parts = callback.data.split("_")
    if len(parts) != 3:
        await callback.answer("Ошибка данных", show_alert=True)
        return
    _, oid_str, status = parts
    if status not in ORDER_STATUSES:
        await callback.answer("Неизвестный статус", show_alert=True)
        return
    try:
        oid = int(oid_str)
    except ValueError:
        await callback.answer("Ошибка ID", show_alert=True)
        return
    await db_order_set_status(oid, status)
    order = await db_order_get(oid)
    if order:
        try:
            await bot.send_message(
                order["user_id"],
                f"📦 Статус заказа #{oid}: **{ORDER_STATUSES[status]}**",
                parse_mode="Markdown",
            )
        except Exception:
            pass
        # обновляем строку в Google Sheets
        if GOOGLE_SHEETS_WEBHOOK:
            await notify_google_sheets(
                oid,
                order["user_id"],
                order["items"],
                order["total"],
                order["discount"],
                order["address"],
                order["phone"],
                order["comment"],
                "",
                status,
            )
    try:
        await callback.message.edit_text(
            f"Заказ #{oid}: **{ORDER_STATUSES[status]}**", parse_mode="Markdown"
        )
    except Exception:
        await callback.message.answer(
            f"Заказ #{oid}: **{ORDER_STATUSES[status]}**", parse_mode="Markdown"
        )
    await callback.answer("Обновлено")


@dp.message(F.text == "✏️ Изменить цену")
async def admin_price_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    kb = await build_admin_products_kb("admprice")
    if not kb:
        await message.answer("Нет товаров.", reply_markup=admin_main_kb)
        return
    await message.answer("Выберите товар:", reply_markup=kb)
    await state.set_state(AdminMenuStates.waiting_for_price_item)


@dp.callback_query(
    F.data.startswith("admprice_"), AdminMenuStates.waiting_for_price_item
)
async def admin_price_chosen(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    item_id = callback.data[8:]
    p = await db_get_product(item_id)
    if not p:
        await callback.answer("Не найден", show_alert=True)
        return
    await state.update_data(item_id=item_id)
    await callback.message.answer(
        f"Текущая цена: {fmt_price(p['price'])}\nВведите новую:"
    )
    await state.set_state(AdminMenuStates.waiting_for_new_price)
    await callback.answer()


@dp.message(AdminMenuStates.waiting_for_new_price)
async def admin_price_save(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if not message.text:
        await message.answer("Введите число.")
        return
    try:
        price = float(message.text.replace(",", "."))
        if not (0 < price < 1_000_000):
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("Некорректная цена.")
        return
    data = await state.get_data()
    item_id = data.get("item_id")
    if not item_id:
        await message.answer("Товар не выбран.", reply_markup=admin_main_kb)
        await state.clear()
        return
    await db_update_price(item_id, price)
    await state.clear()
    await message.answer(
        f"✅ Цена обновлена: {fmt_price(price)}", reply_markup=admin_main_kb
    )


@dp.message(F.text == "❌ Стоп-лист")
async def admin_toggle_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    kb = await build_admin_products_kb("admtoggle")
    if not kb:
        await message.answer("Нет товаров.", reply_markup=admin_main_kb)
        return
    await message.answer("Нажмите товар для переключения:", reply_markup=kb)
    await state.set_state(AdminMenuStates.waiting_for_toggle_item)


@dp.callback_query(
    F.data.startswith("admtoggle_"), AdminMenuStates.waiting_for_toggle_item
)
async def admin_toggle(callback: CallbackQuery, state: FSMContext):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    item_id = callback.data[10:]
    await db_toggle_availability(item_id)
    p = await db_get_product(item_id)
    if not p:
        await callback.answer("Не найден", show_alert=True)
        return
    kb = await build_admin_products_kb("admtoggle")
    if kb:
        try:
            await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
    await callback.answer("🟢 Доступен" if p["is_available"] == 1 else "🔴 Скрыт")


@dp.message(F.text == "🎁 Промокоды")
async def admin_promo_menu(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    promos = await db_promo_list()
    if promos:
        lines = ["🎁 **Активные и архивные промокоды:**\n"]
        for code, percent, active in promos:
            icon = "🟢" if active else "🔴"
            lines.append(f"{icon} `{esc(code)}` — {percent}%")
        await message.answer("\n".join(lines), parse_mode="Markdown")
    else:
        await message.answer("Промокодов пока нет.")
    kb = ReplyKeyboardMarkup(
        keyboard=[
            [
                KeyboardButton(text="➕ Создать промокод"),
                KeyboardButton(text="🚫 Деактивировать"),
            ],
            [KeyboardButton(text="⬅️ Назад")],
        ],
        resize_keyboard=True,
    )
    await message.answer("Управление промокодами:", reply_markup=kb)


@dp.message(F.text == "➕ Создать промокод")
async def promo_create_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Введите код (например, MAFIA10):", reply_markup=cancel_kb)
    await state.set_state(AdminMenuStates.waiting_for_promo_code)


@dp.message(AdminMenuStates.waiting_for_promo_code)
async def promo_create_code(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb)
        return
    if not message.text or not re.fullmatch(r"[A-Za-z0-9]{2,20}", message.text):
        await message.answer("Код: 2–20 символов, латиница/цифры.")
        return
    await state.update_data(code=message.text.upper())
    await message.answer("Введите скидку в % (1–90):")
    await state.set_state(AdminMenuStates.waiting_for_promo_discount)


@dp.message(AdminMenuStates.waiting_for_promo_discount)
async def promo_create_percent(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    try:
        p = int(message.text)
        if not (1 <= p <= 90):
            raise ValueError
    except (ValueError, TypeError):
        await message.answer("Введите число 1–90.")
        return
    data = await state.get_data()
    await db_promo_add(data["code"], p)
    await state.clear()
    await message.answer(
        f"✅ Промокод `{data['code']}` на {p}% создан.",
        parse_mode="Markdown",
        reply_markup=admin_main_kb,
    )


@dp.message(F.text == "🚫 Деактивировать")
async def promo_deact_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Введите код для деактивации:", reply_markup=cancel_kb)
    await state.set_state(AdminMenuStates.waiting_for_promo_deactivate)


@dp.message(AdminMenuStates.waiting_for_promo_deactivate)
async def promo_deact_execute(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb)
        return
    if not message.text:
        await message.answer("Введите код текстом.")
        return
    code = message.text.upper().strip()
    percent = await db_promo_get(code)
    if percent is None:
        await message.answer(
            f"Промокод `{esc(code)}` не найден или уже неактивен.",
            parse_mode="Markdown",
        )
        return
    await db_promo_deactivate(code)
    await state.clear()
    await message.answer(
        f"✅ Промокод `{esc(code)}` деактивирован.",
        parse_mode="Markdown",
        reply_markup=admin_main_kb,
    )


@dp.message(F.text == "📢 Рассылка")
async def broadcast_start(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    await message.answer("Введите текст рассылки:", reply_markup=cancel_kb)
    await state.set_state(BroadcastState.waiting_for_text)


@dp.message(BroadcastState.waiting_for_text)
async def broadcast_confirm(message: Message, state: FSMContext):
    if not is_admin(message.from_user.id):
        return
    if message.text == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=admin_main_kb)
        return
    if not message.text:
        await message.answer("Нужен текст.")
        return
    await state.update_data(text=message.text)
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="✅ Отправить", callback_data="bc_go")],
            [InlineKeyboardButton(text="❌ Отмена", callback_data="bc_cancel")],
        ]
    )
    await message.answer(f"Предпросмотр:\n\n{message.text}", reply_markup=kb)


@dp.callback_query(F.data == "bc_cancel")
async def broadcast_cancel(callback: CallbackQuery, state: FSMContext):
    await state.clear()
    try:
        await callback.message.edit_text("Отменено.")
    except Exception:
        await callback.message.answer("Отменено.")
    await callback.answer()


@dp.callback_query(F.data == "bc_go")
async def broadcast_go(callback: CallbackQuery, state: FSMContext, bot: Bot):
    if not is_admin(callback.from_user.id):
        await callback.answer("⛔", show_alert=True)
        return
    data = await state.get_data()
    text = data.get("text")
    if not text:
        await callback.answer("Текст потерян", show_alert=True)
        await state.clear()
        return
    users = await db_get_all_users()
    await state.clear()
    sent = failed = 0
    for uid in users:
        try:
            await bot.send_message(uid, text)
            sent += 1
            await asyncio.sleep(0.05)
        except Exception:
            failed += 1
    try:
        await callback.message.edit_text(f"📢 Готово. ✅ {sent} / ❌ {failed}")
    except Exception:
        await callback.message.answer(f"📢 Готово. ✅ {sent} / ❌ {failed}")
    await callback.message.answer("Админ-панель", reply_markup=admin_main_kb)
    await callback.answer()


@dp.message(F.text == "⬅️ Назад")
async def back_universal(message: Message, state: FSMContext):
    current = await state.get_state()
    await state.clear()
    if current and (
        current.startswith("AdminMenuStates") or current.startswith("BroadcastState")
    ):
        await message.answer("Админ-панель", reply_markup=admin_main_kb)
    else:
        await message.answer(
            "Главное меню", reply_markup=main_menu_kb(message.from_user.id)
        )


@dp.message(F.text == "❌ Отмена")
async def universal_cancel(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Отменено.", reply_markup=main_menu_kb(message.from_user.id))


# ============================================================
# HEALTH-CHECK / ЗАПУСК
# ============================================================


async def health_check(bot: Bot):
    while True:
        try:
            async with aiosqlite.connect(DB_FILE) as db:
                await db.execute("SELECT 1")
        except Exception as e:
            logger.error("DB health check failed: %s", e)
        await asyncio.sleep(300)


async def main():
    await db_init()
    bot = Bot(token=TOKEN)
    try:
        await bot.set_my_commands(
            [
                BotCommand(command="start", description="Запустить бота"),
                BotCommand(command="admin", description="Админ-панель"),
            ]
        )
    except Exception as e:
        logger.warning("set_my_commands: %s", e)

    hc = asyncio.create_task(health_check(bot))
    try:
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        hc.cancel()
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен.")
