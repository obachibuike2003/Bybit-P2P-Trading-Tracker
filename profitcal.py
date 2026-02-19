import os
import time
import hmac
import hashlib
import json
import sqlite3
from datetime import datetime, timedelta

import requests
from dotenv import load_dotenv

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)

from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
    JobQueue,
    CallbackQueryHandler,
    MessageHandler,
    filters,
)

from enum import Enum, auto
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph


class AddTradeState(Enum):
    SIDE = auto()
    AMOUNT = auto()   # ✅ USDT amount
    PRICE = auto()    # ✅ NGN price


    

user_states = {}   # chat_id → current state
user_data = {}     # chat_id → temp trade data


class OpeningBalanceState(Enum):
    AMOUNT = auto()
opening_states = {}

class ClosingBalanceState(Enum):
    AMOUNT = auto()

closing_states = {}




# ========================= LOAD ENV =========================
load_dotenv()

API_KEY = os.getenv("BYBIT_API_KEY")
API_SECRET = os.getenv("BYBIT_API_SECRET")
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID")
DEFAULT_FIAT = os.getenv("DEFAULT_FIAT", "NGN")

BASE_URL = "https://api.bybit.com"
DB_NAME = "mulla p2p.db"

# ========================= DATABASE =========================


def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # Trades table
    c.execute("""
    CREATE TABLE IF NOT EXISTS trades (
        id TEXT PRIMARY KEY,
        side INTEGER,
        token TEXT,
        amount REAL,
        fiat_amount REAL,
        price REAL,
        fee REAL,
        counterparty TEXT,
        status INTEGER,
        created_at INTEGER,
        completed_at INTEGER
    )
    """)

    # Daily balances
    c.execute("""
    CREATE TABLE IF NOT EXISTS daily_balances (
        date TEXT PRIMARY KEY,
        opening_balance REAL,
        closing_balance REAL
    )
    """)

    # Expenses
    c.execute("""
    CREATE TABLE IF NOT EXISTS expenses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        date TEXT,
        description TEXT,
        amount REAL
    )
    """)

    # Trading day control
    c.execute("""
    CREATE TABLE IF NOT EXISTS trading_day (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        started_at INTEGER,
        ended_at INTEGER
    )
    """)

    conn.commit()
    conn.close()



def get_current_day_range():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        SELECT started_at, COALESCE(ended_at, strftime('%s','now')*1000)
        FROM trading_day
        ORDER BY id DESC
        LIMIT 1
    """)
    row = c.fetchone()
    conn.close()

    return row if row else (None, None)


# ========================= SIGNATURE =========================
def _bybit_sign(body_str: str, ts_ms: str, recv_window: str) -> str:
    payload = f"{ts_ms}{API_KEY}{recv_window}{body_str}"
    return hmac.new(API_SECRET.encode("utf-8"), payload.encode("utf-8"), hashlib.sha256).hexdigest()


# ========================= BYBIT API =========================
def _request_bybit(endpoint: str, *, method: str = "POST", body: dict | None = None):
    ts_ms = str(int(time.time() * 1000))
    recv_window = "5000"
    body = body or {}

    body_str = json.dumps(body, separators=(",", ":"))
    sign = _bybit_sign(body_str, ts_ms, recv_window)

    headers = {
        "X-BAPI-API-KEY": API_KEY,
        "X-BAPI-TIMESTAMP": ts_ms,
        "X-BAPI-RECV-WINDOW": recv_window,
        "X-BAPI-SIGN": sign,
        "Content-Type": "application/json"
    }

    url = BASE_URL + endpoint

    try:
        resp = requests.post(url, data=body_str, headers=headers, timeout=10)
        return resp.json()
    except Exception as e:
        print("API ERROR:", e)
        return None

def export_trades_to_pdf(filename="p2p_report.pdf"):
    from reportlab.lib.styles import ParagraphStyle

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        SELECT side, amount, price, fee, completed_at
        FROM trades
        ORDER BY completed_at ASC
    """)
    rows = c.fetchall()
    conn.close()

    buy_count = sum(1 for r in rows if r[0] == 0)
    sell_count = sum(1 for r in rows if r[0] == 1)

    buys = []
    total_profit_ngn = 0.0
    total_buy_fees_ngn = 0.0

    table_data = [[
        "Buy Time", "Sell Time",
        "USDT", "Buy Price", "Sell Price",
        "Buy Fee ₦", "Profit ₦"
    ]]

    for side, usdt, price, fee, ts in rows:
        usdt = float(usdt)
        price = float(price)
        fee = float(fee)
        trade_time = datetime.fromtimestamp(ts / 1000).strftime("%m-%d %H:%M")

        if side == 0:
            buys.append([usdt, price, fee, trade_time])

        elif side == 1 and buys:
            sell_remaining = usdt
            sell_price = price
            sell_time = trade_time

            while sell_remaining > 0 and buys:
                buy_usdt, buy_price, buy_fee, buy_time = buys.pop(0)

                matched = min(buy_usdt, sell_remaining)
                fee_ratio = matched / buy_usdt
                buy_fee_ngn = (buy_fee * fee_ratio) * buy_price

                gross_profit = matched * (sell_price - buy_price)
                net_profit = gross_profit - buy_fee_ngn

                total_profit_ngn += net_profit
                total_buy_fees_ngn += buy_fee_ngn

                table_data.append([
                    buy_time,
                    sell_time,
                    f"{matched:.4f}",
                    f"{buy_price}",
                    f"{sell_price}",
                    f"{buy_fee_ngn:,.2f}",
                    f"{net_profit:,.2f}"
                ])

                sell_remaining -= matched
                leftover = buy_usdt - matched
                if leftover > 0:
                    buys.insert(0, [leftover, buy_price, buy_fee, buy_time])

    styles = getSampleStyleSheet()
    small_style = ParagraphStyle(name="small", fontSize=9)

    summary = Paragraph(
        f"<b>TOTAL PROFIT:</b> ₦{total_profit_ngn:,.2f}<br/>"
        f"<b>TOTAL BUY FEES:</b> ₦{total_buy_fees_ngn:,.2f}<br/>"
        f"<b>TRADES:</b> {buy_count} Buys • {sell_count} Sells",
        small_style
    )

    pdf = SimpleDocTemplate(filename, pagesize=A4)

    table = Table(table_data, repeatRows=1)
    table.setStyle(TableStyle([
        ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
        ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey),
        ("FONTSIZE", (0, 0), (-1, -1), 8),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
    ]))

    pdf.build([summary, table])
    return filename


def get_trade_counts(start_ms, end_ms):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # COUNT BUYS
    c.execute("""
        SELECT COUNT(*) FROM trades
        WHERE side = 0 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    buy_count = c.fetchone()[0]

    # COUNT SELLS
    c.execute("""
        SELECT COUNT(*) FROM trades
        WHERE side = 1 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    sell_count = c.fetchone()[0]

    conn.close()
    return buy_count, sell_count



async def summary_days(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # User must type a number
    if not context.args:
        return await update.message.reply_text("Usage: /summarydays <number_of_days>")

    try:
        days = int(context.args[0])
        if days <= 0 or days > 365:
            return await update.message.reply_text("Enter a value between 1 and 365 days.")
    except:
        return await update.message.reply_text("Invalid number.")

    now = datetime.now()
    start = now - timedelta(days=days)
    start = start.replace(hour=0, minute=0, second=0, microsecond=0)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    # ==== GET BUY TOTALS ====
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        SELECT amount FROM trades
        WHERE side = 0 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    buys_rows = c.fetchall()
    buys = sum([row[0] for row in buys_rows])

    # ==== GET SELL TOTALS ====
    c.execute("""
        SELECT amount FROM trades
        WHERE side = 1 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    sells_rows = c.fetchall()
    sells = sum([row[0] for row in sells_rows])

    conn.close()

    # ==== SIMPLE SPREAD PROFIT ====
    profit = calculate_simple_spread_profit(start_ms, end_ms)

    # ==== SEND RESULT ====
    await update.message.reply_text(
        f"""
📊 <b>Summary of the last {days} days</b>

💰 Bought: ₦{buys:,.2f}
💵 Sold: ₦{sells:,.2f}
📈 Profit (Spread): ₦{profit:,.2f}
""",
        parse_mode="HTML"
    )


async def yesterday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()

    # Yesterday range
    start = (now - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    end = (now - timedelta(days=1)).replace(hour=23, minute=59, second=59, microsecond=0)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)

    # ==== BUY TOTAL ====
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        SELECT amount FROM trades
        WHERE side = 0 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    buys = sum([row[0] for row in c.fetchall()])

    # ==== SELL TOTAL ====
    c.execute("""
        SELECT amount FROM trades
        WHERE side = 1 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    sells = sum([row[0] for row in c.fetchall()])

    conn.close()

    # ==== PROFIT ====
    profit = calculate_simple_spread_profit(start_ms, end_ms)

    await update.message.reply_text(
        f"""
📊 <b>YESTERDAY'S SUMMARY</b>

🗓 Date: {start.strftime('%Y-%m-%d')}

💰 Bought: ₦{buys:,.2f}
💵 Sold: ₦{sells:,.2f}
📈 Profit (Spread): ₦{profit:,.2f}
""",
        parse_mode="HTML"
    )





def sync_completed_orders():
    BUY_FEE_RATE = 0.00275

    def safe_float(x, default=0.0):
        try:
            return float(str(x).replace(",", "").strip())
        except:
            return default

    def safe_int(x, default=0):
        try:
            return int(x)
        except:
            try:
                return int(float(x))
            except:
                return default

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    START_DATE = datetime(2026, 1, 1)  # 🔁 change year if needed
    begin_ms = int(START_DATE.timestamp() * 1000)
    now_ms = int(time.time() * 1000)


    new_count = 0

    for order in fetch_orders_simplify_list(begin_ms, now_ms, status=50):

        order_id = str(order.get("id") or order.get("orderId") or "")
        if not order_id:
            continue

        c.execute("SELECT 1 FROM trades WHERE id=?", (order_id,))
        if c.fetchone():
            continue

        if order.get("currencyId") != "NGN":
            continue
        if order.get("tokenId") != "USDT":
            continue

        status = safe_int(order.get("status"))
        if status != 50:
            continue

        side = safe_int(order.get("side", 0))
        fiat_amount = safe_float(order.get("amount"))
        price = safe_float(order.get("price"))

        raw_crypto = safe_float(
            order.get("notifyTokenQuantity")
            or order.get("tokenQuantity")
            or order.get("tokenAmount")
            or 0
        )

        # ✅ VERY IMPORTANT:
        # ✅ STORE FULL USDT — DO NOT REMOVE BUY FEE HERE
        crypto_amount = raw_crypto

        fee = crypto_amount * BUY_FEE_RATE if side == 0 else 0.0

        counterparty = order.get("targetNickName", "") or order.get("targetUserId", "")

        created_at = safe_int(order.get("createDate", 0))
        completed_at = safe_int(order.get("updateDate", created_at))

        c.execute("""
            INSERT OR REPLACE INTO trades (
                id, side, token, amount, fiat_amount, price, fee,
                counterparty, status, created_at, completed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            order_id,
            side,
            "USDT",
            crypto_amount,   # ✅ FULL USDT
            fiat_amount,
            price,
            fee,
            counterparty,
            50,
            created_at,
            completed_at
        ))

        new_count += 1

    conn.commit()
    conn.close()
    return new_count






async def raw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_ms = int(time.time() * 1000)
    begin_ms = now_ms - (3 * 24 * 60 * 60 * 1000)

    res = _request_bybit("/v5/p2p/order/simplifyList", body={
        "page": 1,
        "size": 5,
        "status": 50,
        "beginTime": str(begin_ms),
        "endTime": str(now_ms)
    })

    await update.message.reply_text(json.dumps(res, indent=2)[:4000])




def fetch_orders_simplify_list(begin_ms, end_ms, status=50, size=30):
    endpoint = "/v5/p2p/order/simplifyList"
    size = min(int(size or 30), 30)
    page = 1
    seen_ids = set()
    consecutive_empty = 0
    max_consecutive_empty = 3

    while True:
        body = {
            "page": page,
            "size": size,
            "status": status,
            "beginTime": str(begin_ms),
            "endTime": str(end_ms),
        }

        res = _request_bybit(endpoint, method="POST", body=body)
        if not res:
            break

        result = res.get("result", {})
        items = result.get("items") or result.get("list") or []

        if not items:
            consecutive_empty += 1
            if consecutive_empty >= max_consecutive_empty:
                break
            page += 1
            continue

        consecutive_empty = 0

        for it in items:
            oid = str(it.get("id") or it.get("orderId") or "")
            if not oid:
                continue
            if oid in seen_ids:
                continue
            seen_ids.add(oid)
            yield it

        if len(items) < size:
            break

        page += 1
        if page > 1000:
            break

def calculate_simple_spread_profit(start_ms, end_ms):
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        SELECT side, amount, price, fee
        FROM trades
        WHERE completed_at BETWEEN ? AND ?
        ORDER BY completed_at ASC
    """, (start_ms, end_ms))

    rows = c.fetchall()
    conn.close()

    buys = []
    total_profit_ngn = 0.0

    for side, usdt, price, fee in rows:

        # BUY
        if side == 0:
            buys.append([usdt, price, fee])

        # SELL
        elif side == 1 and buys:
            sell_remaining = usdt
            sell_price = price

            while sell_remaining > 0 and buys:
                buy_usdt, buy_price, buy_fee = buys.pop(0)

                matched = min(buy_usdt, sell_remaining)

                gross_profit = matched * (sell_price - buy_price)

                # ✅ use stored fee (offline = 0, online = real)
                fee_ratio = matched / buy_usdt
                fee_ngn = (buy_fee * fee_ratio) * buy_price

                net_profit = gross_profit - fee_ngn
                total_profit_ngn += net_profit

                sell_remaining -= matched

                leftover = buy_usdt - matched
                if leftover > 0:
                    buys.insert(0, [leftover, buy_price, buy_fee])

    return round(total_profit_ngn, 2), 0.0




async def send_daily_report(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()

    # 🔑 Get user-defined trading day range
    start_ms, end_ms = get_current_day_range()

    if not start_ms:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="❌ Trading day not started. Use /startday"
        )
        return  # ✅ STOP HERE if no day started

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # USDT bought
    c.execute("""
        SELECT COALESCE(SUM(amount), 0)
        FROM trades
        WHERE side = 0 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    buys = c.fetchone()[0]

    # USDT sold
    c.execute("""
        SELECT COALESCE(SUM(amount), 0)
        FROM trades
        WHERE side = 1 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    sells = c.fetchone()[0]

    conn.close()

    # Profit
    profit_ngn, profit_usdt = calculate_simple_spread_profit(start_ms, end_ms)

    # Trade counts
    buy_count, sell_count = get_trade_counts(start_ms, end_ms)

    msg = f"""
📊 <b>DAILY P2P REPORT</b>

⏱ Period:
{datetime.fromtimestamp(start_ms/1000).strftime('%Y-%m-%d %H:%M')}
→ {datetime.fromtimestamp(end_ms/1000).strftime('%Y-%m-%d %H:%M')}

💰 Bought: {buys:,.2f} USDT
💵 Sold: {sells:,.2f} USDT

🔄 Trades:
• {buy_count} Buys
• {sell_count} Sells

📈 Profit (NGN): ₦{profit_ngn:,.2f}
💎 Profit (USDT): {profit_usdt:,.4f} USDT
"""

    await context.bot.send_message(
        chat_id=CHAT_ID,
        text=msg,
        parse_mode="HTML"
    )

def get_day_range_by_date(date_str: str):
    """
    Returns (start_ms, end_ms) for a trading day based on manual open/close.
    date_str format: YYYY-MM-DD
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        SELECT opening_balance, closing_balance
        FROM daily_balances
        WHERE date = ?
    """, (date_str,))

    row = c.fetchone()
    conn.close()

    if not row:
        return None, None

    # Trading day runs from 00:00 to 23:59 *of that date*
    # BUT it is ONLY valid if the day was CLOSED manually
    start = datetime.strptime(date_str, "%Y-%m-%d").replace(
        hour=0, minute=0, second=0, microsecond=0
    )
    end = start.replace(hour=23, minute=59, second=59, microsecond=999000)

    return int(start.timestamp() * 1000), int(end.timestamp() * 1000)



async def send_weekly_report(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    week_ago = now - timedelta(days=7)

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # Get trading days closed in last 7 days
    c.execute("""
        SELECT date FROM daily_balances
        WHERE closing_balance IS NOT NULL
        AND date >= ?
        ORDER BY date ASC
    """, (week_ago.strftime("%Y-%m-%d"),))

    days = [row[0] for row in c.fetchall()]
    conn.close()

    if not days:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="❌ No completed trading days in the last 7 days."
        )
        return

    total_buys = total_sells = 0.0
    total_profit_ngn = total_profit_usdt = 0.0
    total_buy_count = total_sell_count = 0

    for day in days:
        start_ms, end_ms = get_day_range_by_date(day)

        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()

        c.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM trades
            WHERE side = 0 AND completed_at BETWEEN ? AND ?
        """, (start_ms, end_ms))
        total_buys += c.fetchone()[0]

        c.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM trades
            WHERE side = 1 AND completed_at BETWEEN ? AND ?
        """, (start_ms, end_ms))
        total_sells += c.fetchone()[0]

        conn.close()

        buy_count, sell_count = get_trade_counts(start_ms, end_ms)
        total_buy_count += buy_count
        total_sell_count += sell_count

        profit_ngn, profit_usdt = calculate_simple_spread_profit(start_ms, end_ms)
        total_profitprofit_ngn += profit_ngn
        total_profit_usdt += profit_usdt

    msg = f"""
📊 <b>WEEKLY P2P REPORT</b>
📅 Trading days: {len(days)}

💰 Bought: {total_buys:,.2f} USDT
💵 Sold: {total_sells:,.2f} USDT

🔄 Trades:
• {total_buy_count} Buys
• {total_sell_count} Sells

📈 Profit (NGN): ₦{total_profit_ngn:,.2f}
💎 Profit (USDT): {total_profit_usdt:,.4f} USDT
"""

    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")



async def send_monthly_report(context: ContextTypes.DEFAULT_TYPE):
    now = datetime.now()
    month_start = now.replace(day=1).strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        SELECT date FROM daily_balances
        WHERE closing_balance IS NOT NULL
        AND date >= ?
        ORDER BY date ASC
    """, (month_start,))

    days = [row[0] for row in c.fetchall()]
    conn.close()

    if not days:
        await context.bot.send_message(
            chat_id=CHAT_ID,
            text="❌ No completed trading days this month."
        )
        return

    total_buys = total_sells = 0.0
    total_profit_ngn = total_profit_usdt = 0.0
    total_buy_count = total_sell_count = 0

    for day in days:
        start_ms, end_ms = get_day_range_by_date(day)

        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()

        c.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM trades
            WHERE side = 0 AND completed_at BETWEEN ? AND ?
        """, (start_ms, end_ms))
        total_buys += c.fetchone()[0]

        c.execute("""
            SELECT COALESCE(SUM(amount), 0)
            FROM trades
            WHERE side = 1 AND completed_at BETWEEN ? AND ?
        """, (start_ms, end_ms))
        total_sells += c.fetchone()[0]

        conn.close()

        buy_count, sell_count = get_trade_counts(start_ms, end_ms)
        total_buy_count += buy_count
        total_sell_count += sell_count

        profit_ngn, profit_usdt = calculate_simple_spread_profit(start_ms, end_ms)
        total_profit_ngn += profit_ngn
        total_profit_usdt += profit_usdt

    msg = f"""
📊 <b>MONTHLY P2P REPORT</b>
📅 Trading days: {len(days)}

💰 Bought: {total_buys:,.2f} USDT
💵 Sold: {total_sells:,.2f} USDT

🔄 Trades:
• {total_buy_count} Buys
• {total_sell_count} Sells

📈 Profit (NGN): ₦{total_profit_ngn:,.2f}
💎 Profit (USDT): {total_profit_usdt:,.4f} USDT
"""

    await context.bot.send_message(chat_id=CHAT_ID, text=msg, parse_mode="HTML")

async def startday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_ms = int(time.time() * 1000)

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    # Close any previous open day (safety)
    c.execute("""
        UPDATE trading_day
        SET ended_at = ?
        WHERE ended_at IS NULL
    """, (now_ms,))

    # Start new day
    c.execute("""
        INSERT INTO trading_day (started_at)
        VALUES (?)
    """, (now_ms,))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "✅ Trading day STARTED\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )
async def endday(update: Update, context: ContextTypes.DEFAULT_TYPE):
    now_ms = int(time.time() * 1000)

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        UPDATE trading_day
        SET ended_at = ?
        WHERE ended_at IS NULL
    """ , (now_ms,))

    if c.rowcount == 0:
        conn.close()
        return await update.message.reply_text("❌ No open trading day.")

    conn.commit()
    conn.close()

    await update.message.reply_text(
        "🔒 Trading day ENDED\n"
        f"⏰ {datetime.now().strftime('%Y-%m-%d %H:%M')}"
    )


async def opening_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text.strip()

    if chat_id not in opening_states:
        return

    try:
        amount = float(text)
    except:
        return await update.message.reply_text(
            "❌ Invalid amount. Enter numbers only:"
        )

    today = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        INSERT OR REPLACE INTO daily_balances (date, opening_balance)
        VALUES (?, ?)
    """, (today, amount))
    conn.commit()
    conn.close()

    opening_states.pop(chat_id)

    await update.message.reply_text(
        f"✅ <b>Opening Balance Saved</b>\n\n"
        f"📅 {today}\n"
        f"💰 ₦{amount:,.2f}",
        parse_mode="HTML"
    )




async def addtrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Add an offline trade manually.
    Usage: /addtrade <side 0/1> <usdt_amount> <fiat_amount_ngn> <price>
    """

    if len(context.args) < 4:
        return await update.message.reply_text(
            "Usage:\n/addtrade <side 0=BUY, 1=SELL> <usdt_amount> <fiat_amount_ngn> <price>"
        )

    try:
        side = int(context.args[0])       # BUY = 0, SELL = 1
        amount = float(context.args[1])   # USDT amount
        fiat_amount = float(context.args[2])  # NGN amount
        price = float(context.args[3])    # NGN per USDT

        if side not in (0, 1):
            return await update.message.reply_text("Side must be 0 (BUY) or 1 (SELL).")

    except ValueError:
        return await update.message.reply_text("❌ Invalid input. Use numbers only.")

    completed_at = int(time.time() * 1000)
    trade_id = f"manual_{completed_at}"

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()

    c.execute("""
        INSERT INTO trades (
            id, side, token, amount, fiat_amount, price, fee,
            counterparty, status, created_at, completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    """, (
        trade_id, side, "USDT", amount, fiat_amount, price, 0.0,
        "offline", 50, completed_at, completed_at
    ))

    conn.commit()
    conn.close()

    await update.message.reply_text(
        f"✅ Manual trade added!\n\n"
        f"ID: {trade_id}\n"
        f"Type: {'BUY' if side == 0 else 'SELL'}\n"
        f"USDT: {amount}\n"
        f"NGN: ₦{fiat_amount:,.2f}\n"
        f"Rate: ₦{price:,.2f}"
    )

async def addtrade(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    user_states[chat_id] = AddTradeState.SIDE
    user_data[chat_id] = {}

    keyboard = [
        [
            InlineKeyboardButton("BUY", callback_data="side_0"),
            InlineKeyboardButton("SELL", callback_data="side_1")
        ]
    ]

    await update.message.reply_text(
        "Select trade type:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
async def opening(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    opening_states[chat_id] = OpeningBalanceState.AMOUNT

    await update.message.reply_text(
        "💰 Enter today's OPENING balance (NGN):"
    )

async def addtrade_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    chat_id = query.message.chat_id

    if query.data.startswith("side_"):
        side = int(query.data.split("_")[1])

        user_data[chat_id]["side"] = side
        user_states[chat_id] = AddTradeState.AMOUNT   # ✅ FIXED STATE

        await query.edit_message_text(
            f"Selected: {'BUY' if side == 0 else 'SELL'}\n\nEnter USDT amount:"
        )


async def addtrade_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text.strip()

    if chat_id not in user_states:
        return

    state = user_states[chat_id]

    # ---- USDT INPUT ----
    if state == AddTradeState.AMOUNT:   # ✅ FIXED
        try:
            user_data[chat_id]["amount"] = float(text)
        except:
            return await update.message.reply_text("Invalid amount. Enter USDT number only:")

        user_states[chat_id] = AddTradeState.PRICE
        return await update.message.reply_text("Enter price (NGN per USDT):")

    # ---- PRICE INPUT ----
    if state == AddTradeState.PRICE:
        try:
            price = float(text)
            amount = user_data[chat_id]["amount"]
            side = user_data[chat_id]["side"]
        except:
            return await update.message.reply_text("Invalid price. Enter price again:")

        fiat_amount = round(amount * price, 2)
        completed_at = int(time.time() * 1000)
        trade_id = f"manual_{completed_at}"

        conn = sqlite3.connect(DB_NAME)
        c = conn.cursor()

        c.execute("""
            INSERT INTO trades (
                id, side, token, amount, fiat_amount, price, fee,
                counterparty, status, created_at, completed_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            trade_id,
            side,
            "USDT",
            amount,
            fiat_amount,
            price,
            0.0,
            "offline",
            50,
            completed_at,
            completed_at
        ))

        conn.commit()
        conn.close()

        user_states.pop(chat_id)
        user_data.pop(chat_id)

        return await update.message.reply_text(
            f"✅ Trade Added Successfully!\n\n"
            f"Type: {'BUY' if side == 0 else 'SELL'}\n"
            f"USDT: {amount}\n"
            f"Rate: ₦{price:,.2f}\n"
            f"Total: ₦{fiat_amount:,.2f}"
        )

async def show_commands(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = """
📌 <b>AVAILABLE BOT COMMANDS</b>

📊 <b>Reports</b>
/daily - Get today's report
/weekly - Get this week's report
/monthly - Get this month's report

📄
/exportpdf - Export all matched trades as PDF

💾 <b>Manual Trading</b>
/addtrade - Add a BUY or SELL manually (auto-calculates NGN)


✅ All calculations are automatic.
✅ All exports are audit-ready.
"""

    await update.message.reply_text(text, parse_mode="HTML")

async def closing_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id
    text = update.message.text.strip()

    if chat_id not in closing_states:
        return

    try:
        amount = float(text)
    except:
        return await update.message.reply_text(
            "❌ Invalid amount. Enter numbers only:"
        )

    today = datetime.now().strftime("%Y-%m-%d")

    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        UPDATE daily_balances
        SET closing_balance = ?
        WHERE date = ?
    """, (amount, today))
    conn.commit()
    conn.close()

    closing_states.pop(chat_id)

    await update.message.reply_text(
        f"✅ <b>Closing Balance Saved</b>\n\n"
        f"📅 {today}\n"
        f"💼 ₦{amount:,.2f}",
        parse_mode="HTML"
    )



# ========================= SUMMARY =========================
def summary(period):
    now = datetime.now()

    if period == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "week":
        start = now - timedelta(days=now.weekday())
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)
    elif period == "month":
        start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    else:  # year
        start = now - timedelta(days=365)
        start = start.replace(hour=0, minute=0, second=0, microsecond=0)

    start_ms = int(start.timestamp() * 1000)
    end_ms = int(now.timestamp() * 1000)

    # BUY total inside period
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        SELECT amount FROM trades 
        WHERE side = 0 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    buys_rows = c.fetchall()

    buys = sum([row[0] for row in buys_rows])

    # SELL total inside period
    c.execute("""
        SELECT amount FROM trades 
        WHERE side = 1 AND completed_at BETWEEN ? AND ?
    """, (start_ms, end_ms))
    sells_rows = c.fetchall()

    sells = sum([row[0] for row in sells_rows])

    conn.close()

    # SIMPLE SPREAD PROFIT
    profit = calculate_simple_spread_profit(start_ms, end_ms)

    return f"""
📊 <b>P2P Summary ({period})</b>

💰 Bought: ₦{buys:,.2f}
💵 Sold: ₦{sells:,.2f}
📈 Profit (Spread): ₦{profit:,.2f}
"""





def fix_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE trades SET side = CAST(side AS INTEGER)")
    conn.commit()
    conn.close()

async def fixdb_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Fixes incorrect 'side' values in the database (string → integer)
    """
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("UPDATE trades SET side = CAST(side AS INTEGER)")
    conn.commit()
    conn.close()

    await update.message.reply_text("✅ Database fixed! Side values converted to integers.")
def debug_last_trades():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT id, side, fiat_amount FROM trades ORDER BY completed_at DESC LIMIT 5")
    rows = c.fetchall()
    conn.close()

    msg = "Last 5 trades:\n"
    for r in rows:
        msg += f"ID: {r[0]} | Side: {r[1]} | ₦{r[2]:,.2f}\n"

    return msg



# ========================= TELEGRAM COMMANDS =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Welcome to Bybit P2P Tracker 🚀")


# ========================= MANUAL REPORT COMMANDS =========================

async def manual_daily(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_daily_report(context)
    await update.message.reply_text("✅ Daily report sent!")

async def manual_weekly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_weekly_report(context)
    await update.message.reply_text("✅ Weekly report sent!")

async def manual_monthly(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await send_monthly_report(context)
    await update.message.reply_text("✅ Monthly report sent!")


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(debug_last_trades())
async def exportpdf(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        filename = f"p2p_report_{int(time.time())}.pdf"

        export_trades_to_pdf(filename)

        with open(filename, "rb") as f:
            await context.bot.send_document(
                chat_id=update.effective_chat.id,
                document=f,
                caption="✅ Your P2P Trading Report"
            )

    except Exception as e:
        await update.message.reply_text(f"❌ PDF Export Failed:\n{e}")
async def closing(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat_id = update.message.chat_id

    closing_states[chat_id] = ClosingBalanceState.AMOUNT

    await update.message.reply_text(
        "💼 Enter today's CLOSING balance (NGN):"
    )





# ========================= AUTOSYNC JOB =========================
async def autosync(context: ContextTypes.DEFAULT_TYPE):
    new = sync_completed_orders()
    if new > 0:
        await context.bot.send_message(chat_id=CHAT_ID, text=f"🔄 Auto-sync: {new} new trades")

# ========================= MAIN =========================
if __name__ == "__main__":
    init_db()

    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))

    app.add_handler(CommandHandler("daily", manual_daily))
    app.add_handler(CommandHandler("weekly", manual_weekly))
    app.add_handler(CommandHandler("monthly", manual_monthly))

    app.add_handler(CommandHandler("summarydays", summary_days))
    app.add_handler(CommandHandler("fixdb", fixdb_cmd))
    app.add_handler(CommandHandler("debug", debug))
    app.add_handler(CommandHandler("raw", raw))
    app.add_handler(CommandHandler("yesterday", yesterday))
    app.add_handler(CommandHandler("exportpdf", exportpdf))
    app.add_handler(CommandHandler("command", show_commands))
    app.add_handler(CommandHandler("opening", opening))
    app.add_handler(CommandHandler("closing", closing))
    app.add_handler(CommandHandler("startday", startday))
    app.add_handler(CommandHandler("endday", endday))







    # Addtrade system
    app.add_handler(CommandHandler("addtrade", addtrade))
    app.add_handler(CallbackQueryHandler(addtrade_buttons, pattern="^side_"))

    # TEXT HANDLER MUST BE LAST
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, addtrade_text))


    jq = app.job_queue
    jq.run_repeating(autosync, interval=600, first=30)  # every 10 mins

    print("Bot running…")
    app.run_polling()
