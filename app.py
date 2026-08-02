#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Финмонитор Кванта v4.2 — «Долговые часы»
+ редактируемый доход/лимит, правка операций/кредитов/карт, live-счётчик процентов.
"""

import sqlite3
import datetime
import math
import html as htmllib
from fastapi import FastAPI, Request, Form
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
from contextlib import asynccontextmanager

try:
    import requests
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

DB_NAME = "finance.db"
OLLAMA_URL = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "qwen3.5:4b"

CARD_PRESETS = {
    "sber": {"name": "Сбер СберКарта", "credit_limit": 150000, "annual_rate": 27.9,
             "grace_period_days": 120, "min_payment_percent": 2.0, "cashback_percent": 0.5},
    "ozon": {"name": "Ozon Карта", "credit_limit": 100000, "annual_rate": 34.9,
             "grace_period_days": 120, "min_payment_percent": 3.0, "cashback_percent": 1.0},
}


def get_db():
    conn = sqlite3.connect(DB_NAME)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS loans (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            initial_amount REAL NOT NULL, current_debt REAL NOT NULL,
            annual_rate REAL NOT NULL, monthly_payment REAL NOT NULL, start_date TEXT NOT NULL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS cards (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
            credit_limit REAL NOT NULL, current_debt REAL NOT NULL,
            annual_rate REAL NOT NULL, grace_period_days INTEGER NOT NULL,
            min_payment_percent REAL NOT NULL, cashback_percent REAL DEFAULT 0)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT, date TEXT, amount REAL,
            category TEXT, description TEXT, loan_id INTEGER, card_id INTEGER)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS snapshots (
            date TEXT PRIMARY KEY, total_debt REAL, loans_debt REAL,
            cards_debt REAL, income_month REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value REAL)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS recurring_payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL, amount REAL NOT NULL,
            category TEXT NOT NULL, day_of_month INTEGER NOT NULL, loan_id INTEGER, card_id INTEGER,
            last_paid TEXT, active INTEGER DEFAULT 1)''')
        conn.execute('''CREATE TABLE IF NOT EXISTS budget_limits (
            category TEXT PRIMARY KEY, monthly_limit REAL NOT NULL, spent REAL DEFAULT 0)''')
        conn.execute("INSERT OR IGNORE INTO settings (key,value) VALUES ('monthly_income',300000),('daily_limit',1500)")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(transactions)").fetchall()]
        if "card_id" not in cols:
            conn.execute("ALTER TABLE transactions ADD COLUMN card_id INTEGER")
        conn.commit()


def get_setting(conn, key, default):
    row = conn.execute("SELECT value FROM settings WHERE key=?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


# --- РАСЧЁТЫ ---
def months_to_payoff(principal, annual_rate, monthly_payment):
    if monthly_payment <= 0:
        return float('inf')
    if annual_rate == 0:
        return math.ceil(principal / monthly_payment)
    r = annual_rate / 12 / 100
    if monthly_payment <= principal * r:
        return float('inf')
    return math.ceil(-math.log(1 - (principal * r) / monthly_payment) / math.log(1 + r))


def payment_breakdown(principal, annual_rate, monthly_payment):
    r = annual_rate / 12 / 100
    interest = principal * r
    return {"interest": interest, "principal": monthly_payment - interest, "total": monthly_payment}


def total_overpayment(principal, annual_rate, monthly_payment):
    m = months_to_payoff(principal, annual_rate, monthly_payment)
    return float('inf') if m == float('inf') else (monthly_payment * m) - principal


def card_metrics(card):
    debt, limit = card["current_debt"], card["credit_limit"]
    return {
        "available": limit - debt,
        "utilization": (debt / limit) * 100 if limit > 0 else 0,
        "min_payment": debt * card["min_payment_percent"] / 100,
        "monthly_interest": debt * card["annual_rate"] / 12 / 100,
    }


def payoff_forecast(debt, annual_rate, monthly_payment, extra_payment=0):
    """Расчёт прогноза закрытия долга с учётом дополнительных платежей"""
    if monthly_payment + extra_payment <= 0:
        return None, float('inf'), float('inf')
    total_paid = 0
    months = 0
    remaining = debt
    while remaining > 0 and months < 600:  # макс 50 лет
        interest = remaining * annual_rate / 12 / 100
        principal = min(monthly_payment + extra_payment - interest, remaining)
        if principal <= 0:
            break
        remaining -= principal
        total_paid += monthly_payment + extra_payment
        months += 1
    payoff_date = datetime.date.today() + datetime.timedelta(days=months * 30) if months < 600 else None
    overpayment = total_paid - debt
    return payoff_date, months, overpayment


def process_recurring_payments(conn):
    """Автоматическое создание повторяющихся платежей"""
    today = datetime.date.today()
    day = today.day
    month_key = today.strftime("%Y-%m")
    
    recurring = conn.execute("SELECT * FROM recurring_payments WHERE active=1").fetchall()
    for rp in recurring:
        last_paid = rp["last_paid"]
        if last_paid and last_paid.startswith(month_key):
            continue  # уже оплачено в этом месяце
        
        # Создаём транзакцию
        amount = rp["amount"]
        if rp["category"] == "доход":
            amount = -abs(amount)
        else:
            amount = abs(amount)
        
        conn.execute('''INSERT INTO transactions (date, amount, category, description, loan_id, card_id)
                       VALUES (?, ?, ?, ?, ?, ?)''',
                    (today.isoformat(), amount, rp["category"], f"Авто: {rp['name']}", 
                     rp["loan_id"], rp["card_id"]))
        
        # Обновляем долг если это платёж по кредиту/карте
        if rp["loan_id"]:
            conn.execute("UPDATE loans SET current_debt = MAX(0, current_debt - ?) WHERE id=?",
                        (rp["amount"], rp["loan_id"]))
        elif rp["card_id"]:
            conn.execute("UPDATE cards SET current_debt = MAX(0, current_debt - ?) WHERE id=?",
                        (rp["amount"], rp["card_id"]))
        
        # Обновляем бюджет
        if rp["category"] != "доход":
            conn.execute('''INSERT INTO budget_limits (category, monthly_limit, spent)
                           VALUES (?, 0, ?) ON CONFLICT(category) DO UPDATE SET spent = spent + ?''',
                        (rp["category"], rp["amount"], rp["amount"]))
        
        # Обновляем дату последней оплаты
        conn.execute("UPDATE recurring_payments SET last_paid=? WHERE id=?",
                    (today.isoformat(), rp["id"]))
    
    conn.commit()


def ensure_snapshot(conn):
    today = datetime.date.today().isoformat()
    loans_debt = conn.execute("SELECT COALESCE(SUM(current_debt),0) FROM loans").fetchone()[0]
    cards_debt = conn.execute("SELECT COALESCE(SUM(current_debt),0) FROM cards").fetchone()[0]
    income = -(conn.execute(
        "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE category='доход' AND date LIKE ?",
        (today[:7] + "%",)).fetchone()[0])
    conn.execute('''INSERT INTO snapshots (date,total_debt,loans_debt,cards_debt,income_month)
                    VALUES (?,?,?,?,?)
                    ON CONFLICT(date) DO UPDATE SET
                    total_debt=excluded.total_debt, loans_debt=excluded.loans_debt,
                    cards_debt=excluded.cards_debt, income_month=excluded.income_month''',
                 (today, loans_debt + cards_debt, loans_debt, cards_debt, income))
    conn.commit()


def build_debt_chart(snaps, width=860, height=320, pad=55):
    if not snaps:
        return "<p class='muted' style='padding:30px;text-align:center'>Снимки ещё копятся. Зайди завтра — будет красиво.</p>"
    debts = [s["total_debt"] for s in snaps]
    dates = [s["date"] for s in snaps]
    if len(debts) == 1:
        debts = [debts[0], debts[0]]
        dates = [dates[0], dates[0]]
    mn, mx = min(debts), max(debts)
    if mx == mn:
        mx = mn + 1000
    n = len(debts)
    xs = [pad + i * (width - 2 * pad) / (n - 1) for i in range(n)]
    ys = [height - pad - (d - mn) / (mx - mn) * (height - 2 * pad) for d in debts]
    line = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}" for i, (x, y) in enumerate(zip(xs, ys)))
    area = line + f" L{xs[-1]:.1f},{height - pad} L{xs[0]:.1f},{height - pad} Z"
    grid = ""
    for k in range(5):
        gy = pad + k * (height - 2 * pad) / 4
        val = mx - k * (mx - mn) / 4
        grid += f'<line x1="{pad}" y1="{gy:.1f}" x2="{width - pad}" y2="{gy:.1f}" stroke="#212b3a" stroke-width="1"/>'
        grid += f'<text x="{pad - 10}" y="{gy + 4:.1f}" text-anchor="end" fill="#5b6779" font-size="12" font-family="JetBrains Mono,monospace">{val / 1000:.0f}к</text>'
    dots = "".join(
        f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4.5" fill="#00e676" stroke="#0b1017" stroke-width="2">'
        f'<title>{d}: {debts[i]:,.0f} ₽</title></circle>'
        for i, (x, y, d) in enumerate(zip(xs, ys, dates)))
    lbl = ""
    for idx in {0, n // 2, n - 1}:
        lbl += f'<text x="{xs[idx]:.1f}" y="{height - pad + 22}" text-anchor="middle" fill="#5b6779" font-size="12" font-family="JetBrains Mono,monospace">{dates[idx]}</text>'
    return f'''<svg viewBox="0 0 {width} {height}" style="width:100%;height:auto">
      <defs><linearGradient id="g" x1="0" y1="0" x2="0" y2="1">
        <stop offset="0%" stop-color="#ff5252" stop-opacity="0.35"/>
        <stop offset="100%" stop-color="#ff5252" stop-opacity="0"/>
      </linearGradient></defs>
      {grid}
      <path d="{area}" fill="url(#g)"/>
      <path d="{line}" fill="none" stroke="#ff5252" stroke-width="2.5" stroke-linejoin="round" stroke-linecap="round"/>
      {dots}{lbl}
      <line x1="{pad}" y1="{height - pad}" x2="{width - pad}" y2="{height - pad}" stroke="#31405a" stroke-width="1.5"/>
    </svg>'''


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with get_db() as c:
        ensure_snapshot(c)
        process_recurring_payments(c)
    yield

app = FastAPI(lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


def format_currency(value):
    return "∞" if value == float('inf') else f"{value:,.0f}".replace(",", " ")

def format_percent(value):
    return f"{value:.2f}"

templates.env.filters["format_currency"] = format_currency
templates.env.filters["format_percent"] = format_percent


# --- ГЛАВНАЯ ---
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    with get_db() as conn:
        ensure_snapshot(conn)
        loans = conn.execute("SELECT * FROM loans").fetchall()
        cards = conn.execute("SELECT * FROM cards").fetchall()
        monthly_income = get_setting(conn, "monthly_income", 300000)
        daily_limit = get_setting(conn, "daily_limit", 1500)
        today = datetime.date.today().isoformat()
        spent_today = conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE date=? AND amount>0 AND category!='долг'",
            (today,)).fetchone()[0]
        month_income = -(conn.execute(
            "SELECT COALESCE(SUM(amount),0) FROM transactions WHERE category='доход' AND date LIKE ?",
            (today[:7] + "%",)).fetchone()[0])
        
        # Бюджет по категориям
        budget_limits = {row["category"]: row["monthly_limit"] for row in conn.execute("SELECT * FROM budget_limits WHERE monthly_limit > 0").fetchall()}
        spent_by_category = {}
        for row in conn.execute("SELECT category, SUM(amount) as spent FROM transactions WHERE amount > 0 AND category != 'долг' AND date LIKE ? GROUP BY category", (today[:7] + "%",)).fetchall():
            spent_by_category[row["category"]] = row["spent"]

    loans_debt = sum(l["current_debt"] for l in loans)
    cards_debt = sum(c["current_debt"] for c in cards)
    total_debt = loans_debt + cards_debt
    total_initial = sum(l["initial_amount"] for l in loans)
    loan_pay = sum(l["monthly_payment"] for l in loans)
    card_min = sum(c["current_debt"] * c["min_payment_percent"] / 100 for c in cards)
    obligations = loan_pay + card_min
    debt_ratio = obligations / monthly_income * 100 if monthly_income else 0
    total_paid = total_initial - loans_debt
    progress = total_paid / total_initial * 100 if total_initial else 0

    # Прогноз закрытия всех долгов
    avg_rate = weighted_year / total_debt if total_debt > 0 else 0
    payoff_date, months_to_freedom, total_overpayment = payoff_forecast(total_debt, avg_rate, obligations, extra_payment=0)

    # Проценты в реальном времени (для долговых часов)
    weighted_year = sum(l["current_debt"] * l["annual_rate"] for l in loans) + \
                    sum(c["current_debt"] * c["annual_rate"] for c in cards)
    interest_per_year = weighted_year / 100
    interest_per_day = interest_per_year / 365
    interest_per_minute = interest_per_day / 1440
    interest_per_second = interest_per_day / 86400

    return templates.TemplateResponse(request=request, name="index.html", context={
        "request": request, "loans": loans, "cards": cards, "loans_debt": loans_debt,
        "cards_debt": cards_debt, "total_debt": total_debt, "total_paid": total_paid,
        "progress_percent": progress, "loan_pay": loan_pay, "card_min": card_min,
        "obligations": obligations, "debt_ratio": debt_ratio,
        "monthly_income": monthly_income, "fact_income": month_income,
        "daily_limit": daily_limit, "spent_today": spent_today,
        "remaining_today": daily_limit - spent_today,
        "interest_per_second": interest_per_second, "interest_per_minute": interest_per_minute,
        "interest_per_day": interest_per_day,
        "payoff_date": payoff_date.strftime("%d.%m.%Y") if payoff_date else "∞",
        "months_to_freedom": months_to_freedom,
        "budget_limits": budget_limits, "spent_by_category": spent_by_category})


# --- НАСТРОЙКИ (доход + лимит) ---
@app.get("/settings_form")
async def settings_form():
    with get_db() as conn:
        mi = get_setting(conn, "monthly_income", 300000)
        dl = get_setting(conn, "daily_limit", 1500)
    return HTMLResponse(f'''
    <div class="modal-card">
      <a href="/" class="x">✕</a>
      <h3 class="display green" style="font-size:20px;margin-bottom:16px">⚙ Настройки</h3>
      <form method="post" action="/save_settings" class="stack">
        <label class="label">Доход в месяц (план)</label>
        <input class="inp" type="number" step="0.01" name="monthly_income" value="{mi:g}" required>
        <label class="label">Лимит на жизнь в день</label>
        <input class="inp" type="number" step="0.01" name="daily_limit" value="{dl:g}" required>
        <button class="btn btn-green block">Сохранить</button>
      </form>
    </div>''')


@app.post("/save_settings")
async def save_settings(monthly_income: float = Form(...), daily_limit: float = Form(...)):
    with get_db() as conn:
        set_setting(conn, "monthly_income", monthly_income)
        set_setting(conn, "daily_limit", daily_limit)
        conn.commit()
    return RedirectResponse("/", status_code=303)


# --- КРЕДИТЫ ---
@app.get("/loans", response_class=HTMLResponse)
async def loans_list(request: Request):
    with get_db() as conn:
        loans = conn.execute("SELECT * FROM loans ORDER BY id").fetchall()
    data = []
    for ln in loans:
        m = months_to_payoff(ln["current_debt"], ln["annual_rate"], ln["monthly_payment"])
        b = payment_breakdown(ln["current_debt"], ln["annual_rate"], ln["monthly_payment"])
        pd = datetime.date.today() + datetime.timedelta(days=m * 30) if m != float('inf') else None
        data.append({**dict(ln), "months_to_payoff": m,
                     "payoff_date": pd.strftime("%d.%m.%Y") if pd else "Никогда",
                     "interest_payment": b["interest"], "principal_payment": b["principal"],
                     "total_overpayment": total_overpayment(ln["current_debt"], ln["annual_rate"], ln["monthly_payment"]),
                     "progress_percent": (ln["initial_amount"] - ln["current_debt"]) / ln["initial_amount"] * 100 if ln["initial_amount"] else 0})
    return templates.TemplateResponse(request=request, name="loans.html", context={"request": request, "loans": data})


@app.get("/loan/{loan_id}", response_class=HTMLResponse)
async def loan_detail(request: Request, loan_id: int):
    with get_db() as conn:
        ln = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
        if not ln:
            return RedirectResponse("/", status_code=303)
        txs = conn.execute("SELECT * FROM transactions WHERE loan_id=? ORDER BY date DESC,id DESC LIMIT 20", (loan_id,)).fetchall()
    m = months_to_payoff(ln["current_debt"], ln["annual_rate"], ln["monthly_payment"])
    b = payment_breakdown(ln["current_debt"], ln["annual_rate"], ln["monthly_payment"])
    pd = datetime.date.today() + datetime.timedelta(days=m * 30) if m != float('inf') else None
    data = {**dict(ln), "months_to_payoff": m, "payoff_date": pd.strftime("%d.%m.%Y") if pd else "Никогда",
            "interest_payment": b["interest"], "principal_payment": b["principal"],
            "total_overpayment": total_overpayment(ln["current_debt"], ln["annual_rate"], ln["monthly_payment"]),
            "progress_percent": (ln["initial_amount"] - ln["current_debt"]) / ln["initial_amount"] * 100 if ln["initial_amount"] else 0}
    return templates.TemplateResponse(request=request, name="loan_detail.html", context={"request": request, "loan": data, "transactions": txs})


@app.get("/add_loan_form")
async def add_loan_form():
    return HTMLResponse('''
    <div class="modal-card">
      <a href="/" class="x">✕</a>
      <h3 class="display green" style="font-size:20px;margin-bottom:16px">Новый кредит</h3>
      <form method="post" action="/add_loan" class="stack">
        <input class="inp" type="text" name="name" placeholder="Название" required>
        <input class="inp" type="number" step="0.01" name="initial_amount" placeholder="Первоначальная сумма" required>
        <input class="inp" type="number" step="0.01" name="current_debt" placeholder="Текущий остаток" required>
        <input class="inp" type="number" step="0.01" name="annual_rate" placeholder="Годовая ставка %" required>
        <input class="inp" type="number" step="0.01" name="monthly_payment" placeholder="Ежемесячный платёж" required>
        <input class="inp" type="date" name="start_date" required>
        <button class="btn btn-green block">Добавить</button>
      </form>
    </div>''')


@app.post("/add_loan")
async def add_loan(name: str = Form(...), initial_amount: float = Form(...), current_debt: float = Form(...),
                   annual_rate: float = Form(...), monthly_payment: float = Form(...), start_date: str = Form(...)):
    with get_db() as conn:
        conn.execute("INSERT INTO loans (name,initial_amount,current_debt,annual_rate,monthly_payment,start_date) VALUES (?,?,?,?,?,?)",
                     (name, initial_amount, current_debt, annual_rate, monthly_payment, start_date))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse("/", status_code=303)


@app.get("/edit_loan_form/{loan_id}")
async def edit_loan_form(loan_id: int):
    with get_db() as conn:
        ln = conn.execute("SELECT * FROM loans WHERE id=?", (loan_id,)).fetchone()
    if not ln:
        return RedirectResponse("/loans", status_code=303)
    return HTMLResponse(f'''
    <div class="modal-card">
      <a href="/loan/{loan_id}" class="x">✕</a>
      <h3 class="display green" style="font-size:20px;margin-bottom:16px">Правка: {htmllib.escape(ln["name"])}</h3>
      <form method="post" action="/edit_loan/{loan_id}" class="stack">
        <input class="inp" type="text" name="name" value="{htmllib.escape(ln["name"])}" required>
        <input class="inp" type="number" step="0.01" name="initial_amount" value="{ln["initial_amount"]:g}" required>
        <input class="inp" type="number" step="0.01" name="current_debt" value="{ln["current_debt"]:g}" required>
        <input class="inp" type="number" step="0.01" name="annual_rate" value="{ln["annual_rate"]:g}" required>
        <input class="inp" type="number" step="0.01" name="monthly_payment" value="{ln["monthly_payment"]:g}" required>
        <input class="inp" type="date" name="start_date" value="{ln["start_date"]}" required>
        <button class="btn btn-green block">Сохранить</button>
      </form>
    </div>''')


@app.post("/edit_loan/{loan_id}")
async def edit_loan(loan_id: int, name: str = Form(...), initial_amount: float = Form(...), current_debt: float = Form(...),
                    annual_rate: float = Form(...), monthly_payment: float = Form(...), start_date: str = Form(...)):
    with get_db() as conn:
        conn.execute("UPDATE loans SET name=?,initial_amount=?,current_debt=?,annual_rate=?,monthly_payment=?,start_date=? WHERE id=?",
                     (name, initial_amount, current_debt, annual_rate, monthly_payment, start_date, loan_id))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse(f"/loan/{loan_id}", status_code=303)


@app.get("/del_loan/{loan_id}")
async def del_loan(loan_id: int):
    with get_db() as conn:
        conn.execute("UPDATE transactions SET loan_id=NULL WHERE loan_id=?", (loan_id,))
        conn.execute("DELETE FROM loans WHERE id=?", (loan_id,))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse("/loans", status_code=303)


@app.get("/pay_loan_form/{loan_id}")
async def pay_loan_form(loan_id: int):
    with get_db() as conn:
        ln = conn.execute("SELECT name,current_debt FROM loans WHERE id=?", (loan_id,)).fetchone()
    return HTMLResponse(f'''
    <div class="modal-card">
      <a href="/loan/{loan_id}" class="x">✕</a>
      <h3 class="display green" style="font-size:20px;margin-bottom:6px">Платёж: {htmllib.escape(ln["name"])}</h3>
      <p class="dim" style="margin-bottom:14px">Остаток: {ln["current_debt"]:,.0f} ₽. Платёж уменьшит долг и попадёт в историю.</p>
      <form method="post" action="/pay_loan/{loan_id}" class="stack">
        <input class="inp" type="number" step="0.01" name="amount" placeholder="Сумма платежа" required autofocus>
        <input class="inp" type="text" name="description" placeholder="Досрочный / плановый">
        <button class="btn btn-green block">Внести платёж</button>
      </form>
    </div>''')


@app.post("/pay_loan/{loan_id}")
async def pay_loan(loan_id: int, amount: float = Form(...), description: str = Form("Платёж по кредиту")):
    today = datetime.date.today().isoformat()
    with get_db() as conn:
        conn.execute("UPDATE loans SET current_debt = MAX(0, current_debt - ?) WHERE id=?", (amount, loan_id))
        conn.execute("INSERT INTO transactions (date,amount,category,description,loan_id) VALUES (?,?,?,?,?)",
                     (today, amount, "долг", description, loan_id))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse(f"/loan/{loan_id}", status_code=303)


# --- КРЕДИТКИ ---
@app.get("/cards", response_class=HTMLResponse)
async def cards_list(request: Request):
    with get_db() as conn:
        cards = conn.execute("SELECT * FROM cards ORDER BY id").fetchall()
    data = [{**dict(c), **card_metrics(c)} for c in cards]
    return templates.TemplateResponse(request=request, name="cards.html", context={"request": request, "cards": data})


@app.get("/card/{card_id}", response_class=HTMLResponse)
async def card_detail(request: Request, card_id: int):
    with get_db() as conn:
        c = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
        if not c:
            return RedirectResponse("/", status_code=303)
        txs = conn.execute("SELECT * FROM transactions WHERE card_id=? ORDER BY date DESC,id DESC LIMIT 20", (card_id,)).fetchall()
    return templates.TemplateResponse(request=request, name="card_detail.html",
                                      context={"request": request, "card": {**dict(c), **card_metrics(c)}, "transactions": txs})


@app.get("/add_card_form")
async def add_card_form():
    s, o = CARD_PRESETS["sber"], CARD_PRESETS["ozon"]
    return HTMLResponse(f'''
    <div class="modal-card">
      <a href="/" class="x">✕</a>
      <h3 class="display orange" style="font-size:20px;margin-bottom:10px">Новая кредитка</h3>
      <p class="dim" style="margin-bottom:10px">Быстрое заполнение (типовые условия, сверяй с договором!):</p>
      <div class="row" style="margin-bottom:14px">
        <button type="button" onclick="fillCard('sber')" class="btn btn-ghost btn-sm block">Сбер 120д</button>
        <button type="button" onclick="fillCard('ozon')" class="btn btn-ghost btn-sm block">Ozon 120д</button>
      </div>
      <form method="post" action="/add_card" class="stack">
        <input id="cf_name" class="inp" type="text" name="name" placeholder="Название" required>
        <input id="cf_limit" class="inp" type="number" step="0.01" name="credit_limit" placeholder="Кредитный лимит" required>
        <input id="cf_debt" class="inp" type="number" step="0.01" name="current_debt" placeholder="Текущий долг" required>
        <input id="cf_rate" class="inp" type="number" step="0.01" name="annual_rate" placeholder="Ставка вне грейса %" required>
        <input id="cf_grace" class="inp" type="number" name="grace_period_days" placeholder="Грейс-период (дней)" required>
        <input id="cf_min" class="inp" type="number" step="0.01" name="min_payment_percent" placeholder="Мин. платёж % от долга" required>
        <input id="cf_cb" class="inp" type="number" step="0.01" name="cashback_percent" placeholder="Кэшбэк %" value="0">
        <button class="btn btn-orange block">Добавить карту</button>
      </form>
      <script>
        const P = {{"sber": {s}, "ozon": {o}}};
        function fillCard(k) {{
          const p = P[k];
          cf_name.value=p.name; cf_limit.value=p.credit_limit; cf_rate.value=p.annual_rate;
          cf_grace.value=p.grace_period_days; cf_min.value=p.min_payment_percent; cf_cb.value=p.cashback_percent;
          cf_debt.focus();
        }}
      </script>
    </div>''')


@app.post("/add_card")
async def add_card(name: str = Form(...), credit_limit: float = Form(...), current_debt: float = Form(...),
                   annual_rate: float = Form(...), grace_period_days: int = Form(...),
                   min_payment_percent: float = Form(...), cashback_percent: float = Form(0)):
    with get_db() as conn:
        conn.execute("INSERT INTO cards (name,credit_limit,current_debt,annual_rate,grace_period_days,min_payment_percent,cashback_percent) VALUES (?,?,?,?,?,?,?)",
                     (name, credit_limit, current_debt, annual_rate, grace_period_days, min_payment_percent, cashback_percent))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse("/cards", status_code=303)


@app.get("/edit_card_form/{card_id}")
async def edit_card_form(card_id: int):
    with get_db() as conn:
        c = conn.execute("SELECT * FROM cards WHERE id=?", (card_id,)).fetchone()
    if not c:
        return RedirectResponse("/cards", status_code=303)
    return HTMLResponse(f'''
    <div class="modal-card">
      <a href="/card/{card_id}" class="x">✕</a>
      <h3 class="display orange" style="font-size:20px;margin-bottom:16px">Правка: {htmllib.escape(c["name"])}</h3>
      <form method="post" action="/edit_card/{card_id}" class="stack">
        <input class="inp" type="text" name="name" value="{htmllib.escape(c["name"])}" required>
        <input class="inp" type="number" step="0.01" name="credit_limit" value="{c["credit_limit"]:g}" required>
        <input class="inp" type="number" step="0.01" name="current_debt" value="{c["current_debt"]:g}" required>
        <input class="inp" type="number" step="0.01" name="annual_rate" value="{c["annual_rate"]:g}" required>
        <input class="inp" type="number" name="grace_period_days" value="{c["grace_period_days"]}" required>
        <input class="inp" type="number" step="0.01" name="min_payment_percent" value="{c["min_payment_percent"]:g}" required>
        <input class="inp" type="number" step="0.01" name="cashback_percent" value="{c["cashback_percent"]:g}">
        <button class="btn btn-orange block">Сохранить</button>
      </form>
    </div>''')


@app.post("/edit_card/{card_id}")
async def edit_card(card_id: int, name: str = Form(...), credit_limit: float = Form(...), current_debt: float = Form(...),
                    annual_rate: float = Form(...), grace_period_days: int = Form(...),
                    min_payment_percent: float = Form(...), cashback_percent: float = Form(0)):
    with get_db() as conn:
        conn.execute("UPDATE cards SET name=?,credit_limit=?,current_debt=?,annual_rate=?,grace_period_days=?,min_payment_percent=?,cashback_percent=? WHERE id=?",
                     (name, credit_limit, current_debt, annual_rate, grace_period_days, min_payment_percent, cashback_percent, card_id))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse(f"/card/{card_id}", status_code=303)


@app.get("/del_card/{card_id}")
async def del_card(card_id: int):
    with get_db() as conn:
        conn.execute("UPDATE transactions SET card_id=NULL WHERE card_id=?", (card_id,))
        conn.execute("DELETE FROM cards WHERE id=?", (card_id,))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse("/cards", status_code=303)


@app.get("/pay_card_form/{card_id}")
async def pay_card_form(card_id: int):
    with get_db() as conn:
        c = conn.execute("SELECT name,current_debt FROM cards WHERE id=?", (card_id,)).fetchone()
    return HTMLResponse(f'''
    <div class="modal-card">
      <a href="/card/{card_id}" class="x">✕</a>
      <h3 class="display orange" style="font-size:20px;margin-bottom:6px">Платёж: {htmllib.escape(c["name"])}</h3>
      <p class="dim" style="margin-bottom:14px">Долг: {c["current_debt"]:,.0f} ₽. Плати всё до конца грейса!</p>
      <form method="post" action="/pay_card/{card_id}" class="stack">
        <input class="inp" type="number" step="0.01" name="amount" placeholder="Сумма платежа" required autofocus>
        <input class="inp" type="text" name="description" placeholder="Полное погашение / минимум">
        <button class="btn btn-orange block">Внести платёж</button>
      </form>
    </div>''')


@app.post("/pay_card/{card_id}")
async def pay_card(card_id: int, amount: float = Form(...), description: str = Form("Платёж по карте")):
    today = datetime.date.today().isoformat()
    with get_db() as conn:
        conn.execute("UPDATE cards SET current_debt = MAX(0, current_debt - ?) WHERE id=?", (amount, card_id))
        conn.execute("INSERT INTO transactions (date,amount,category,description,card_id) VALUES (?,?,?,?,?)",
                     (today, amount, "долг", description, card_id))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse(f"/card/{card_id}", status_code=303)


# --- ДОХОД ---
@app.get("/income_form")
async def income_form():
    return HTMLResponse('''
    <div class="modal-card">
      <a href="/" class="x">✕</a>
      <h3 class="display green" style="font-size:20px;margin-bottom:16px">Доход</h3>
      <form method="post" action="/add_transaction" class="stack">
        <input type="hidden" name="is_income" value="1">
        <input class="inp" type="number" step="0.01" name="amount" placeholder="Сумма" required autofocus>
        <input class="inp" type="text" name="description" placeholder="Зарплата / премия / подработка">
        <button class="btn btn-green block">Записать доход</button>
      </form>
    </div>''')


# --- ТРАНЗАКЦИИ ---
@app.get("/add_form")
async def add_form():
    with get_db() as conn:
        loans = conn.execute("SELECT id,name FROM loans").fetchall()
        cards = conn.execute("SELECT id,name FROM cards").fetchall()
    lo = '<option value="">Кредит: нет</option>' + "".join(f'<option value="{l["id"]}">{htmllib.escape(l["name"])}</option>' for l in loans)
    co = '<option value="">Карта: нет</option>' + "".join(f'<option value="{c["id"]}">💳 {htmllib.escape(c["name"])}</option>' for c in cards)
    return HTMLResponse(f'''
    <div class="modal-card">
      <a href="/" class="x">✕</a>
      <h3 class="display green" style="font-size:20px;margin-bottom:16px">Транзакция</h3>
      <form method="post" action="/add_transaction" class="stack">
        <input class="inp" type="number" step="0.01" name="amount" placeholder="+ расход, - доход" required autofocus>
        <select class="inp" name="category"><option value="еда">Еда</option><option value="транспорт">Транспорт</option><option value="разное">Разное</option><option value="доход">Доход</option></select>
        <select class="inp" name="loan_id">{lo}</select>
        <select class="inp" name="card_id">{co}</select>
        <input class="inp" type="text" name="description" placeholder="Описание">
        <button class="btn btn-green block">Сохранить</button>
      </form>
    </div>''')


@app.post("/add_transaction")
async def add_transaction(amount: float = Form(...), category: str = Form("разное"),
                          description: str = Form(""), loan_id: str = Form(""), card_id: str = Form(""),
                          is_income: str = Form("")):
    if is_income:
        category = "доход"
        amount = -abs(amount)
    today = datetime.date.today().isoformat()
    lid = int(loan_id) if loan_id else None
    cid = int(card_id) if card_id else None
    with get_db() as conn:
        conn.execute("INSERT INTO transactions (date,amount,category,description,loan_id,card_id) VALUES (?,?,?,?,?,?)",
                     (today, amount, category, description, lid, cid))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse("/", status_code=303)


@app.get("/edit_tx_form/{tx_id}")
async def edit_tx_form(tx_id: int):
    with get_db() as conn:
        tx = conn.execute("SELECT * FROM transactions WHERE id=?", (tx_id,)).fetchone()
    if not tx:
        return RedirectResponse("/", status_code=303)
    cats = ["еда", "транспорт", "разное", "долг", "доход"]
    opts = "".join(f'<option value="{c}" {"selected" if c == tx["category"] else ""}>{c}</option>' for c in cats)
    return HTMLResponse(f'''
    <div class="modal-card">
      <a href="/" class="x">✕</a>
      <h3 class="display green" style="font-size:20px;margin-bottom:16px">Правка операции</h3>
      <form method="post" action="/edit_tx/{tx_id}" class="stack">
        <input class="inp" type="number" step="0.01" name="amount" value="{abs(tx["amount"]):g}" required>
        <select class="inp" name="category">{opts}</select>
        <input class="inp" type="text" name="description" value="{htmllib.escape(tx["description"] or "")}">
        <button class="btn btn-green block">Сохранить</button>
      </form>
    </div>''')


@app.post("/edit_tx/{tx_id}")
async def edit_tx(tx_id: int, amount: float = Form(...), category: str = Form(...), description: str = Form("")):
    amount = -abs(amount) if category == "доход" else abs(amount)
    with get_db() as conn:
        conn.execute("UPDATE transactions SET amount=?,category=?,description=? WHERE id=?",
                     (amount, category, description, tx_id))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse("/", status_code=303)


@app.get("/del_tx/{tx_id}")
async def del_tx(tx_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM transactions WHERE id=?", (tx_id,))
        conn.commit()
        ensure_snapshot(conn)
    return RedirectResponse("/", status_code=303)


@app.get("/transactions_table")
async def transactions_table():
    with get_db() as conn:
        rows = conn.execute('''SELECT t.*, l.name loan_name, c.name card_name
            FROM transactions t LEFT JOIN loans l ON t.loan_id=l.id LEFT JOIN cards c ON t.card_id=c.id
            ORDER BY t.date DESC, t.id DESC LIMIT 12''').fetchall()
    if not rows:
        return HTMLResponse('<p class="muted" style="padding:30px;text-align:center">Пока пусто.</p>')
    out = '<table><thead><tr><th>Дата</th><th>Категория</th><th>Описание</th><th>Источник</th><th class="r">Сумма</th><th></th></tr></thead><tbody>'
    for r in rows:
        color = "red" if r["amount"] > 0 else "green"
        sign = "+" if r["amount"] > 0 else ""
        if r["loan_name"]:
            badge = f'<span class="badge b-blue">{htmllib.escape(r["loan_name"])}</span>'
        elif r["card_name"]:
            badge = f'<span class="badge b-orange">💳 {htmllib.escape(r["card_name"])}</span>'
        else:
            badge = '<span class="badge b-gray">—</span>'
        out += f'<tr><td class="num">{r["date"]}</td><td><span class="badge b-gray">{r["category"]}</span></td><td class="muted">{htmllib.escape(r["description"] or "-")}</td><td>{badge}</td><td class="r num {color}" style="font-weight:700">{sign}{r["amount"]:,.0f} ₽</td><td class="r" style="white-space:nowrap"><a href="#" hx-get="/edit_tx_form/{r["id"]}" hx-target="#modal" hx-swap="innerHTML" onclick="modal.classList.remove(\'hidden\');return false;" style="color:var(--blue);text-decoration:none;font-size:12px;margin-right:8px">✎</a><a href="/del_tx/{r["id"]}" style="color:var(--red);text-decoration:none;font-size:12px">✕</a></td></tr>'
    out += '</tbody></table>'
    return HTMLResponse(out)


# --- ОТЧЁТ ---
@app.get("/report", response_class=HTMLResponse)
async def report(request: Request):
    with get_db() as conn:
        ensure_snapshot(conn)
        snaps = conn.execute("SELECT * FROM snapshots ORDER BY date").fetchall()
        loan_pay = conn.execute("SELECT COALESCE(SUM(monthly_payment),0) FROM loans").fetchone()[0]
        cur_debt = conn.execute("SELECT COALESCE(SUM(current_debt),0) FROM loans").fetchone()[0] + \
                   conn.execute("SELECT COALESCE(SUM(current_debt),0) FROM cards").fetchone()[0]
    chart = build_debt_chart(snaps)
    first = snaps[0] if snaps else None
    last = snaps[-1] if snaps else None
    reduced = (first["total_debt"] - last["total_debt"]) if (first and last) else 0
    days = 1
    if first and last:
        d0 = datetime.date.fromisoformat(first["date"])
        d1 = datetime.date.fromisoformat(last["date"])
        days = max((d1 - d0).days, 1)
    avg_daily = reduced / days if days else 0
    months_left = months_to_payoff(cur_debt, 12.0, loan_pay) if loan_pay else float('inf')
    freedom = (datetime.date.today() + datetime.timedelta(days=months_left * 30)).strftime("%d.%m.%Y") if months_left != float('inf') else "∞"
    return templates.TemplateResponse(request=request, name="report.html", context={
        "request": request, "chart": chart, "snaps": snaps, "reduced": reduced, "days": days,
        "avg_daily": avg_daily, "freedom_date": freedom, "current_debt": cur_debt})


# --- OLLAMA AI ---
@app.get("/advice")
def advice():
    if not HAS_REQUESTS:
        return HTMLResponse('<div class="red">Нужен requests: <code>pip install requests</code></div>')
    with get_db() as conn:
        loans = conn.execute("SELECT name,current_debt,annual_rate,monthly_payment FROM loans").fetchall()
        cards = conn.execute("SELECT name,current_debt,annual_rate,credit_limit FROM cards").fetchall()
        mi = get_setting(conn, "monthly_income", 300000)
        dl = get_setting(conn, "daily_limit", 1500)
    lines = [f"Доход: {mi:g} ₽/мес. Дневной лимит на жизнь: {dl:g} ₽."]
    for l in loans:
        lines.append(f"Кредит '{l['name']}': долг {l['current_debt']:,.0f} ₽, ставка {l['annual_rate']}%, платёж {l['monthly_payment']:,.0f} ₽/мес.")
    for c in cards:
        lines.append(f"Кредитка '{c['name']}': долг {c['current_debt']:,.0f} ₽ из лимита {c['credit_limit']:,.0f}, ставка {c['annual_rate']}%.")
    if not loans and not cards:
        lines.append("Долгов нет.")
    snapshot = "\n".join(lines)
    prompt = (f"Ты — финансовый советник-анархист, ненавидишь банки и буржуев. Помоги инженеру Кванту выбраться из кредитного рабства.\n"
              f"Его финансовый снимок:\n{snapshot}\n\n"
              f"Дай 4 конкретных совета на русском, коротко, по делу, с лёгким матом. Фокус: как быстрее закрыть долги с минимальной переплатой (метод лавины/снежного кома, грейс-периоды).")
    try:
        r = requests.post(OLLAMA_URL, json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False}, timeout=180)
        text = r.json().get("response", "Пустой ответ от модели.")
    except Exception as e:
        text = f"Не удалось связаться с Ollama ({e}). Проверь: ollama serve и ollama pull {OLLAMA_MODEL}"
    safe = htmllib.escape(text).replace("\n", "<br>")
    return HTMLResponse(f'<div style="line-height:1.7">{safe}</div>')


# --- ПЛАНИРОВЩИК БЮДЖЕТА ---
@app.get("/budget_form")
async def budget_form():
    with get_db() as conn:
        limits = conn.execute("SELECT * FROM budget_limits").fetchall()
    cats = ["еда", "транспорт", "развлечения", "одежда", "здоровье", "образование", "разное"]
    rows = ""
    for cat in cats:
        existing = next((l for l in limits if l["category"] == cat), None)
        limit_val = existing["monthly_limit"] if existing else 0
        rows += f'''<tr><td>{cat}</td><td><input type="number" step="0.01" name="limit_{cat}" value="{limit_val}" style="width:120px"></td></tr>'''
    return HTMLResponse(f'''
    <div class="modal-card">
      <a href="/" class="x">✕</a>
      <h3 class="display green" style="font-size:20px;margin-bottom:16px">📊 Лимиты бюджета</h3>
      <form method="post" action="/save_budget" class="stack">
        <table style="width:100%"><tbody>{rows}</tbody></table>
        <button class="btn btn-green block">Сохранить лимиты</button>
      </form>
    </div>''')


@app.post("/save_budget")
async def save_budget(request: Request):
    form_data = await request.form()
    with get_db() as conn:
        for key, value in form_data.items():
            if key.startswith("limit_"):
                cat = key[6:]
                limit = float(value) if value else 0
                if limit > 0:
                    conn.execute('''INSERT INTO budget_limits (category, monthly_limit, spent) 
                                   VALUES (?, ?, 0) ON CONFLICT(category) DO UPDATE SET monthly_limit=excluded.monthly_limit''',
                                (cat, limit))
                else:
                    conn.execute("DELETE FROM budget_limits WHERE category=?", (cat,))
        conn.commit()
    return RedirectResponse("/", status_code=303)


# --- ПОВТОРЯЮЩИЕСЯ ПЛАТЕЖИ ---
@app.get("/recurring_form")
async def recurring_form():
    with get_db() as conn:
        recurring = conn.execute("SELECT * FROM recurring_payments ORDER BY id").fetchall()
        loans = conn.execute("SELECT id, name FROM loans").fetchall()
        cards = conn.execute("SELECT id, name FROM cards").fetchall()
    
    loan_opts = '<option value="">Нет</option>' + "".join(f'<option value="{l["id"]}">{htmllib.escape(l["name"])}</option>' for l in loans)
    card_opts = '<option value="">Нет</option>' + "".join(f'<option value="{c["id"]}">💳 {htmllib.escape(c["name"])}</option>' for c in cards)
    
    rows = ""
    for rp in recurring:
        status = "✅" if rp["active"] else "❌"
        rows += f'''<tr><td>{status}</td><td>{htmllib.escape(rp["name"])}</td><td class="num">{rp["amount"]:,.0f} ₽</td><td>{rp["day_of_month"]}-е число</td>
        <td><a href="/toggle_recurring/{rp["id"]}" style="color:var(--blue);font-size:12px">переключить</a> | <a href="/del_recurring/{rp["id"]}" style="color:var(--red);font-size:12px">удалить</a></td></tr>'''
    
    return HTMLResponse(f'''
    <div class="modal-card" style="max-width:700px">
      <a href="/" class="x">✕</a>
      <h3 class="display green" style="font-size:20px;margin-bottom:10px">🔁 Повторяющиеся платежи</h3>
      <p class="dim" style="margin-bottom:14px">Автоматически создаются каждый месяц в указанную дату</p>
      {"<table style=\"width:100%;margin-bottom:16px\"><thead><tr><th></th><th>Название</th><th>Сумма</th><th>Дата</th><th></th></tr></thead><tbody>" + rows + "</tbody></table>" if rows else "<p class=\"muted\" style=\"padding:20px;text-align:center\">Пока нет повторяющихся платежей</p>"}
      <hr style="border-color:#212b3a;margin:16px 0">
      <h4 style="font-size:16px;margin-bottom:10px">Добавить новый</h4>
      <form method="post" action="/add_recurring" class="stack">
        <input class="inp" type="text" name="name" placeholder="Название (аренда, подписка)" required>
        <input class="inp" type="number" step="0.01" name="amount" placeholder="Сумма" required>
        <select class="inp" name="category"><option value="еда">Еда</option><option value="транспорт">Транспорт</option><option value="развлечения">Развлечения</option><option value="разное">Разное</option><option value="доход">Доход</option></select>
        <input class="inp" type="number" name="day_of_month" placeholder="День месяца (1-31)" min="1" max="31" required>
        <select class="inp" name="loan_id">{loan_opts}</select>
        <select class="inp" name="card_id">{card_opts}</select>
        <button class="btn btn-green block">Добавить</button>
      </form>
    </div>''')


@app.post("/add_recurring")
async def add_recurring(name: str = Form(...), amount: float = Form(...), category: str = Form(...),
                        day_of_month: int = Form(...), loan_id: str = Form(""), card_id: str = Form("")):
    lid = int(loan_id) if loan_id else None
    cid = int(card_id) if card_id else None
    with get_db() as conn:
        conn.execute('''INSERT INTO recurring_payments (name, amount, category, day_of_month, loan_id, card_id, active)
                       VALUES (?, ?, ?, ?, ?, ?, 1)''',
                    (name, amount, category, day_of_month, lid, cid))
        conn.commit()
    return RedirectResponse("/recurring_form", status_code=303)


@app.get("/toggle_recurring/{rp_id}")
async def toggle_recurring(rp_id: int):
    with get_db() as conn:
        rp = conn.execute("SELECT active FROM recurring_payments WHERE id=?", (rp_id,)).fetchone()
        if rp:
            conn.execute("UPDATE recurring_payments SET active=? WHERE id=?", (0 if rp["active"] else 1, rp_id))
            conn.commit()
    return RedirectResponse("/recurring_form", status_code=303)


@app.get("/del_recurring/{rp_id}")
async def del_recurring(rp_id: int):
    with get_db() as conn:
        conn.execute("DELETE FROM recurring_payments WHERE id=?", (rp_id,))
        conn.commit()
    return RedirectResponse("/recurring_form", status_code=303)


if __name__ == "__main__":
    import uvicorn
    print("🚀 Финмонитор v4.3 на http://127.0.0.1:8000")
    print(f"🤖 ИИ: {OLLAMA_MODEL} через Ollama")
    print("📊 Бюджет: лимиты по категориям")
    print("🔁 Планировщик: авто-платежи")
    print("💀 Смотри на долговые часы и злись. Злость — топливо для досрочных платежей.")
    uvicorn.run(app, host="127.0.0.1", port=8000)
