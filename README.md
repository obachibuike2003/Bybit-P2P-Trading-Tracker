# MullaBot — Bybit P2P Trading Tracker 🤖📊

A Python-powered Telegram bot that automatically syncs completed P2P trades from the Bybit API, calculates spread-based profits using FIFO matching, and delivers performance reports — built for NGN-based traders.

---

## Features

- **Auto Trade Sync** — Pulls completed Bybit P2P orders every 10 minutes via HMAC-authenticated REST API
- **FIFO Profit Matching** — Matches buys to sells in order, calculates net spread profit accounting for trading fees
- **Daily / Weekly / Monthly Reports** — Automated and on-demand performance summaries
- **Manual Trade Entry** — Add offline trades via conversational Telegram flow
- **PDF Export** — Audit-ready matched trade report with buy/sell pairing and profit breakdown
- **Balance Tracking** — Record opening and closing NGN balances per trading day
- **Trading Day Control** — Start and end trading sessions to scope reports accurately
- **NGN-Native** — All reporting in Nigerian Naira (₦)

---

## Tech Stack

| Layer | Technology |
|---|---|
| Language | Python 3.11+ |
| Bot Framework | python-telegram-bot |
| Database | SQLite |
| API Auth | HMAC-SHA256 |
| Exchange | Bybit REST API v5 |
| PDF Generation | ReportLab |
| Config | python-dotenv |

---

## Setup

### 1. Clone the repository
```bash
git clone https://github.com/yourusername/mullabot.git
cd mullabot
```

### 2. Install dependencies
```bash
pip install -r requirements.txt
```

### 3. Configure environment variables

Create a `.env` file in the root directory:

```env
BYBIT_API_KEY=your_bybit_api_key
BYBIT_API_SECRET=your_bybit_api_secret
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_telegram_chat_id
DEFAULT_FIAT=NGN
```

### 4. Run the bot
```bash
python bot.py
```

---

## Commands

| Command | Description |
|---|---|
| `/startday` | Start a new trading session |
| `/endday` | End the current trading session |
| `/daily` | Get today's P2P report |
| `/weekly` | Get this week's report |
| `/monthly` | Get this month's report |
| `/yesterday` | Get yesterday's summary |
| `/summarydays <n>` | Summary for the last N days |
| `/addtrade` | Manually add a BUY or SELL trade |
| `/opening` | Record today's opening NGN balance |
| `/closing` | Record today's closing NGN balance |
| `/exportpdf` | Export matched trades as a PDF report |
| `/debug` | View last 5 trades in the database |
| `/raw` | View raw Bybit API response |

---

## How Profit Is Calculated

MullaBot uses **FIFO (First In, First Out)** matching:

1. Each BUY is queued in order of completion
2. When a SELL occurs, it is matched against the oldest BUY first
3. Net profit = `(sell_price - buy_price) × matched_USDT - buy_fee_NGN`
4. Partial matches are supported — leftover BUY quantity is requeued

---

## Database Schema

```
trades          → id, side, token, amount, fiat_amount, price, fee, counterparty, status, timestamps
daily_balances  → date, opening_balance, closing_balance
trading_day     → started_at, ended_at
expenses        → date, description, amount
```

---

## Requirements

```
python-telegram-bot
requests
python-dotenv
reportlab
```

---

## License

MIT License — free to use and modify.

---

> Built for active Bybit P2P traders who want clear, automated insight into their daily trading performance.
