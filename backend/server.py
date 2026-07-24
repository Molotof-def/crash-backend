import asyncio
import random
import sqlite3
import math
import time
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from concurrent.futures import ThreadPoolExecutor

# Настройка БД
conn = sqlite3.connect("casino.db", check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL") # Ускоряет запись
conn.execute("PRAGMA synchronous=NORMAL")
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 10000000000,
        balance_ton REAL DEFAULT 100.0,
        inventory TEXT DEFAULT '[]',
        last_daily INTEGER DEFAULT 0,
        user_name TEXT DEFAULT 'Игрок',
        avatar TEXT DEFAULT '👤',
        total_games INTEGER DEFAULT 0,
        max_win REAL DEFAULT 0,
        total_profit REAL DEFAULT 0,
        custom_title TEXT DEFAULT '',
        frame_color TEXT DEFAULT '',
        total_mines_x REAL DEFAULT 0.0,
        game_history TEXT DEFAULT '[]'
    )
""")

cursor.execute("""
    CREATE TABLE IF NOT EXISTS crash_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        multiplier REAL,
        timestamp INTEGER
    )
""")
conn.commit()

# Пул для асинхронных операций с БД
db_pool = ThreadPoolExecutor(max_workers=1)

# КЭШ ИСТОРИИ (RAM)
cursor.execute("SELECT multiplier FROM crash_history ORDER BY id DESC LIMIT 50")
crash_history_cache = [r[0] for r in cursor.fetchall()]

def db_execute(func, *args):
    """Выполняет функцию БД в отдельном потоке, чтобы не блокировать WebSocket"""
    return db_pool.submit(func, *args).result()

def save_crash_result_db(mult):
    global crash_history_cache
    try:
        c = conn.cursor()
        c.execute("INSERT INTO crash_history (multiplier, timestamp) VALUES (?, ?)", (mult, int(time.time())))
        conn.commit()
        crash_history_cache.insert(0, mult)
        if len(crash_history_cache) > 50:
            crash_history_cache = crash_history_cache[:50]
    except Exception as e: print(f"DB Error save_crash: {e}")

def get_user_db(user_id, user_name=None, avatar=None):
    try:
        c = conn.cursor()
        c.execute("SELECT balance, balance_ton, inventory, last_daily, user_name, avatar, total_games, max_win, total_profit, custom_title, frame_color, total_mines_x, game_history FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if row:
            b, b_ton, inv, ld, un, av, tg, mw, tp, ct, fc, tmx, gh = row[0], row[1] or 100.0, json.loads(row[2]), row[3], row[4], row[5], row[6], row[7], row[8], row[9] or '', row[10] or '', row[11] or 0.0, json.loads(row[12] or '[]')
            if user_name or avatar:
                un = user_name or un
                av = avatar or av
                c.execute("UPDATE users SET user_name = ?, avatar = ? WHERE user_id = ?", (un, av, user_id))
                conn.commit()
            return b, b_ton, inv, ld, un, av, tg, mw, tp, ct, fc, tmx, gh
        
        un = user_name or f"Игрок #{str(user_id)[-4:]}"
        av = avatar or "👤"
        c.execute("INSERT INTO users (user_id, balance, balance_ton, inventory, last_daily, user_name, avatar, total_games, max_win, total_profit, custom_title, frame_color, total_mines_x, game_history) VALUES (?, ?, 100.0, '[]', 0, ?, ?, 0, 0, 0, '', '', 0.0, '[]')",
                       (user_id, 10000000000, un, av))
        conn.commit()
        return 10000000000, 100.0, [], 0, un, av, 0, 0, 0, '', '', 0.0, []
    except Exception as e: print(f"DB Error get_user: {e}"); return None

def update_user_bulk_db(user_id, data_tuples, game_entry=None):
    """Обновляет множество полей юзера и историю игр за одну транзакцию"""
    try:
        c = conn.cursor()
        # Получаем текущие данные для истории и max_win
        c.execute("SELECT game_history, max_win, total_profit, balance, balance_ton, total_games, total_mines_x FROM users WHERE user_id = ?", (user_id,))
        row = c.fetchone()
        if not row: return
        
        gh, cur_mw, cur_tp, cur_b, cur_bt, cur_tg, cur_tmx = json.loads(row[0] or '[]'), row[1], row[2], row[3], row[4], row[5], row[6]
        
        # Собираем UPDATE запрос
        set_parts = []
        params = []
        new_b, new_bt = cur_b, cur_bt

        # Флаги для вычислений
        games_add = 0
        win_amount = 0
        profit_add = 0
        add_mines_x = 0

        for col, val, op in data_tuples:
            if op == 'set':
                set_parts.append(f"{col} = ?")
                params.append(val)
                if col == 'balance': new_b = val
                if col == 'balance_ton': new_bt = val
            elif op == 'add':
                set_parts.append(f"{col} = {col} + ?")
                params.append(val)
                if col == 'total_games': games_add = val
                if col == 'total_profit': profit_add = val
                if col == 'total_mines_x': add_mines_x = val
            elif op == 'win': # Спец хак для win_amount
                win_amount = val

        # Расчет истории игр
        if game_entry:
            game_entry["time"] = int(time.time())
            gh.insert(0, game_entry)
            gh = gh[:15]
            set_parts.append("game_history = ?")
            params.append(json.dumps(gh))

        # Расчет Max Win
        if win_amount > cur_mw:
            set_parts.append("max_win = ?")
            params.append(win_amount)

        params.append(user_id)
        query = f"UPDATE users SET {', '.join(set_parts)} WHERE user_id = ?"
        c.execute(query, params)
        conn.commit()
    except Exception as e: print(f"DB Error bulk_update: {e}")


app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class GameState:
    def __init__(self):
        self.state = "idle" # idle, running, crashed
        self.start_time = 0
        self.target_crash = 1.0
        self.active_bets = {}
        self.connections = {} 
        self.mines_games = {}
        self.coinflip_rooms = {}
        self.chat_messages = []

game = GameState()

async def broadcast(message: dict):
    if not game.connections: return
    msg = json.dumps(message)
    # Используем wait для быстрой отправки всем без блокировки
    await asyncio.gather(*(ws.send_text(msg) for ws in game.connections.values()), return_exceptions=True)

# 📈 Радикально новая логика Краша
async def crash_loop():
    while True:
        # 1. Фаза Ожидания (Idle)
        game.state = "idle"
        game.active_bets = {}
        game.start_time = time.time() + 5.0 # Время старта в будущем

        await broadcast({
            "type": "state_update", 
            "state": "idle", 
            "time_left": 5.0,
            "start_timestamp": game.start_time,
            "bets": {},
            "history": crash_history_cache
        })
        await asyncio.sleep(5.0)

        # 2. Расчет точки взрыва
        rand = random.random()
        game.target_crash = 1.0
        if rand > 0.03: # 3% instant crash
            # Популярная формула Краша
            game.target_crash = round(max(1.0, 0.99 / (1 - random.random())), 2)
            if game.target_crash > 100: game.target_crash = round(100 + random.random()*50, 2)

        # Расчет времени полета (Экономит трафик, убирает лаги анимации!)
        # multiplier = exp(0.15 * elapsed) -> elapsed = log(multiplier) / 0.15
        flight_duration = math.log(game.target_crash) / 0.15
        game.start_time = time.time()
        game.state = "running"

        # Шлем один пакет о начале полета
        await broadcast({
            "type": "state_update",
            "state": "running",
            "start_timestamp": game.start_time,
            "target_crash": game.target_crash, # Клиент сам анимирует до этого числа
            "flight_duration": flight_duration,
            "bets": game.active_bets
        })

        # Ждем пока ракета летит
        await asyncio.sleep(flight_duration)

        # 3. Взрыв (Crashed)
        game.state = "crashed"
        db_execute(save_crash_result_db, game.target_crash)

        # Обработка проигравших ставок (асинхронно, чтобы не лагало)
        for uid, bet in game.active_bets.items():
            if bet["status"] == "playing":
                bet["status"] = "crashed"
                sym = "💎" if bet.get("currency")=="ton" else "⭐️"
                g_entry = {"mode": "🚀 Краш", "bet": bet["amount"], "win": 0, "mult": 0, "symbol": sym}
                
                # Обновляем профит и историю за 1 запрос
                db_execute(update_user_bulk_db, uid, [('total_games', 1, 'add'), ('total_profit', -bet["amount"], 'add')], g_entry)

        await broadcast({
            "type": "state_update",
            "state": "crashed",
            "multiplier": game.target_crash,
            "bets": game.active_bets,
            "history": crash_history_cache
        })
        await asyncio.sleep(2.5) # Пауза после взрыва

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(crash_loop())

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    game.connections[user_id] = websocket
    
    # Быстрое получение юзера
    ud = db_execute(get_user_db, user_id)
    if not ud: await websocket.close(); return
    
    b, b_ton, inv, ld, u_name, u_ava, tg, mw, tp, ct, fc, tmx, gh = ud
    await websocket.send_json({
        "type": "userData", "balance": b, "balance_ton": b_ton, "inventory": inv,
        "stats": {"total_games": tg, "max_win": mw, "total_profit": tp, "total_mines_x": tmx},
        "game_history": gh, "custom_title": ct, "frame_color": fc
    })
    # Шлем текущее состояние игры
    current_elapsed = time.time() - game.start_time
    await websocket.send_json({
        "type": "state_update", 
        "state": game.state,
        "time_left": max(0, game.start_time - time.time()) if game.state == 'idle' else 0,
        "start_timestamp": game.start_time,
        "multiplier": round(math.exp(0.15 * current_elapsed), 2) if game.state == 'running' else 1.0,
        "target_crash": game.target_crash if game.state == 'running' else 0,
        "history": crash_history_cache,
        "bets": game.active_bets
    })
    await websocket.send_json({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())})

    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            action = data.get("action")
            
            if action == "bet":
                if game.state != "idle": continue
                amount = float(data.get("amount", 0))
                currency = data.get("currency", "stars")
                if amount <= 0: continue

                # Получаем свежий баланс БЕЗ пула (нужна скорость)
                ud = get_user_db(user_id, data.get("user_name"), data.get("avatar"))
                if not ud: continue
                b, b_ton = ud[0], ud[1]

                can_bet = (b_ton >= amount) if currency == "ton" else (b >= amount)
                if can_bet:
                    # Списываем бабки (асинхронно)
                    if currency == "ton":
                        db_execute(update_user_bulk_db, user_id, [('balance_ton', round(b_ton - amount, 2), 'set')])
                    else:
                        db_execute(update_user_bulk_db, user_id, [('balance', int(b - amount), 'set')])
                    
                    game.active_bets[user_id] = {
                        "amount": amount, "currency": currency, "status": "playing", "win": 0,
                        "user_name": data.get("user_name"), "avatar": data.get("avatar")
                    }
                    # Шлем обновленный баланс только игроку
                    new_b, new_bt = (int(b - amount), b_ton) if currency == 'stars' else (b, round(b_ton - amount, 2))
                    await websocket.send_json({"type": "userData", "balance": new_b, "balance_ton": new_bt})
                    # Обновляем список ставок всем
                    await broadcast({"type": "bets_update", "bets": game.active_bets})
                    
            elif action == "cashout":
                if game.state != "running": continue
                bet = game.active_bets.get(user_id)
                if not bet or bet["status"] != "playing": continue

                # Расчет X на момент получения сервером (Мгновенно!)
                elapsed = time.time() - game.start_time
                current_mult = round(math.exp(0.15 * elapsed), 2)

                if current_mult >= game.target_crash: continue # Опоздал, ракета уже взорвалась

                # Успешный вывод
                win_amount = round(bet["amount"] * current_mult, 2)
                curr = bet["currency"]
                bet["status"] = "cashed_out"
                bet["win"] = win_amount
                bet["m"] = current_mult # Фиксируем X вывода

                # Начисляем бабки и историю в фоне
                db_op = []
                if curr == "ton": db_op.append(('balance_ton', win_amount, 'add')) # Используем 'add' в кастом update
                else: db_op.append(('balance', int(win_amount), 'add'))
                
                db_op.append(('total_games', 1, 'add'))
                db_op.append(('total_profit', win_amount - bet["amount"], 'add'))
                db_op.append(('win', win_amount, 'win')) # Для max_win

                sym = "💎" if curr=="ton" else "⭐️"
                g_entry = {"mode": "🚀 Краш", "bet": bet["amount"], "win": win_amount, "mult": current_mult, "symbol": sym}
                
                # Сложное обновление в БД в один приход пула
                db_execute(complete_cashout_db, user_id, curr, win_amount, g_entry)

                # Шлем юзеру уведомление мгновенно
                await websocket.send_json({"type": "notify", "msg": f"🎯 Забрал {win_amount} {sym}! ({current_mult}x)"})
                await broadcast({"type": "bets_update", "bets": game.active_bets})

            # --- Остальные режимы вынесены в пул ---
            elif action == "mines_start":
                db_execute(handle_mines_start, user_id, data, websocket)

            elif action == "mines_open":
                db_execute(handle_mines_open, user_id, data, websocket)

            elif action == "mines_cashout":
                db_execute(handle_mines_cashout, user_id, websocket)

            elif action == "create_coinflip":
                db_execute(handle_create_coinflip, user_id, data, websocket)

            elif action == "join_coinflip":
                db_execute(handle_join_coinflip, user_id, data, websocket)

            elif action == "topup_ton":
                db_execute(update_user_data, user_id, balance_ton=round(get_user_db(user_id)[1] + 10.0, 2))
                new_ud = get_user_db(user_id)
                await websocket.send_json({"type": "userData", "balance": new_ud[0], "balance_ton": new_ud[1]})
                await websocket.send_json({"type": "notify", "msg": "💎 +10 TON!"})

    except WebSocketDisconnect:
        if user_id in game.connections: del game.connections[user_id]


# Вспомогательные функции БД, чтобы не блокировать майн луп
def complete_cashout_db(user_id, curr, win_amount, g_entry):
    c = conn.cursor()
    c.execute("SELECT balance, balance_ton, max_win, total_profit, game_history FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row: return
    b, bt, mw, tp, gh = row[0], row[1], row[2], row[3], json.loads(row[4] or '[]')
    
    new_mw = max(mw, win_amount)
    gh.insert(0, g_entry); gh = gh[:15]
    
    if curr == 'ton':
        c.execute("UPDATE users SET balance_ton = ?, max_win = ?, total_profit = ?, total_games = total_games + 1, game_history = ? WHERE user_id = ?",
                  (round(bt + win_amount, 2), new_mw, tp + (win_amount - g_entry['bet']), json.dumps(gh), user_id))
    else:
        c.execute("UPDATE users SET balance = ?, max_win = ?, total_profit = ?, total_games = total_games + 1, game_history = ? WHERE user_id = ?",
                  (int(b + win_amount), new_mw, tp + (win_amount - g_entry['bet']), json.dumps(gh), user_id))
    conn.commit()

# --- Мины и Коинфлип (упрощено и в пуле) ---
def handle_mines_start(uid, data, ws):
    bet, curr = float(data.get("bet", 0)), data.get("currency")
    m_cnt, g_sz = int(data.get("mines", 3)), int(data.get("grid_size", 5))
    ud = get_user_db(uid)
    bal = ud[1] if curr == 'ton' else ud[0]
    if bet <= 0 or bal < bet: return

    # Списание
    c = conn.cursor()
    if curr == 'ton': c.execute("UPDATE users SET balance_ton = ? WHERE user_id = ?", (round(bal - bet, 2), uid))
    else: c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (int(bal - bet), uid))
    conn.commit()
    
    grid = ["gem"] * (g_sz*g_sz)
    for idx in random.sample(range(g_sz*g_sz), m_cnt): grid[idx] = "mine"
    next_m = calculate_mines_mult(g_sz*g_sz, m_cnt, 1)

    game.mines_games[uid] = {"bet": bet, "curr": curr, "m": m_cnt, "sz": g_sz, "grid": grid, "open": [], "status": "playing"}
    
    asyncio.run_coroutine_threadsafe(ws.send_json({"type": "userData", "balance": get_user_db(uid)[0], "balance_ton": get_user_db(uid)[1]}), asyncio.get_event_loop())
    asyncio.run_coroutine_threadsafe(ws.send_json({"type": "mines_state", "status": "playing", "mult": 1.0, "next_mult": next_m, "win": 0, "grid_size": g_sz}), asyncio.get_event_loop())

def handle_mines_open(uid, data, ws):
    gm = game.mines_games.get(uid)
    idx = data.get("cell")
    if not gm or gm["status"] != "playing" or idx in gm["open"]: return
    
    if gm["grid"][idx] == "mine":
        gm["status"] = "crashed"
        g_entry = {"mode": "💣 Мины", "bet": gm["bet"], "win": 0, "mult": 0, "symbol": "💎" if gm["curr"]=="ton" else "⭐️"}
        update_user_bulk_db(uid, [('total_games', 1, 'add'), ('total_profit', -gm["bet"], 'add')], g_entry)
        asyncio.run_coroutine_threadsafe(ws.send_json({"type": "mines_state", "status": "crashed", "grid": gm["grid"], "opened": gm["open"]+[idx]}), asyncio.get_event_loop())
    else:
        gm["open"].append(idx)
        mult = calculate_mines_mult(gm["sz"]*gm["sz"], gm["m"], len(gm["open"]))
        next_m = calculate_mines_mult(gm["sz"]*gm["sz"], gm["m"], len(gm["open"])+1)
        win = round(gm["bet"] * mult, 2)
        asyncio.run_coroutine_threadsafe(ws.send_json({"type": "mines_state", "status": "playing", "opened": gm["open"], "mult": mult, "next_mult": next_m, "win": win}), asyncio.get_event_loop())

def handle_mines_cashout(uid, ws):
    gm = game.mines_games.get(uid)
    if not gm or gm["status"] != "playing" or not gm["open"]: return
    mult = calculate_mines_mult(gm["sz"]*gm["sz"], gm["m"], len(gm["open"]))
    win = round(gm["bet"] * mult, 2)
    gm["status"] = "cashed"
    
    sym = "💎" if gm["curr"]=="ton" else "⭐️"
    g_entry = {"mode": "💣 Мины", "bet": gm["bet"], "win": win, "mult": mult, "symbol": sym}
    
    # Зачисление
    c = conn.cursor()
    ud = get_user_db(uid)
    if gm["curr"] == 'ton': c.execute("UPDATE users SET balance_ton = ? WHERE user_id = ?", (round(ud[1] + win, 2), uid))
    else: c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (int(ud[0] + win), uid))
    conn.commit()
    
    update_user_bulk_db(uid, [('total_games', 1, 'add'), ('total_profit', win-gm["bet"], 'add'), ('total_mines_x', mult, 'add')], g_entry)
    
    new_ud = get_user_db(uid)
    asyncio.run_coroutine_threadsafe(ws.send_json({"type": "userData", "balance": new_ud[0], "balance_ton": new_ud[1]}), asyncio.get_event_loop())
    asyncio.run_coroutine_threadsafe(ws.send_json({"type": "mines_state", "status": "cashed", "grid": gm["grid"], "win": win}), asyncio.get_event_loop())

def handle_create_coinflip(uid, data, ws):
    bet, curr, side = float(data.get("amount", 0)), data.get("currency"), data.get("side")
    ud = get_user_db(uid)
    bal = ud[1] if curr == 'ton' else ud[0]
    if bet <= 0 or bal < bet: return
    
    # Списание
    c = conn.cursor()
    if curr == 'ton': c.execute("UPDATE users SET balance_ton = ? WHERE user_id = ?", (round(bal - bet, 2), uid))
    else: c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (int(bal - bet), uid))
    conn.commit()
    
    r_id = f"cf_{uid}_{int(time.time())}"
    game.coinflip_rooms[r_id] = {"room_id": r_id, "creator_id": uid, "creator_name": ud[4], "creator_avatar": ud[5], "amount": bet, "currency": curr, "side": side}
    
    asyncio.run_coroutine_threadsafe(ws.send_json({"type": "userData", "balance": get_user_db(uid)[0], "balance_ton": get_user_db(uid)[1]}), asyncio.get_event_loop())
    asyncio.run_coroutine_threadsafe(broadcast({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())}), asyncio.get_event_loop())

def handle_join_coinflip(uid, data, ws):
    r_id = data.get("room_id")
    rm = game.coinflip_rooms.get(r_id)
    if not rm or rm["creator_id"] == uid: return
    
    curr = rm["currency"]
    ud = get_user_db(uid)
    bal = ud[1] if curr == 'ton' else ud[0]
    if bal < rm["amount"]: return
    
    # Списание у зашедшего
    c = conn.cursor()
    if curr == 'ton': c.execute("UPDATE users SET balance_ton = ? WHERE user_id = ?", (round(bal - rm["amount"], 2), uid))
    else: c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (int(bal - rm["amount"]), uid))
    conn.commit()
    
    # Игра
    win_side = random.choice(["eagle", "tails"])
    pot = round(rm["amount"] * 2, 2)
    win_id = rm["creator_id"] if win_side == rm["side"] else uid
    los_id = uid if win_id == rm["creator_id"] else rm["creator_id"]
    
    # Начисление победителю
    w_ud = get_user_db(win_id)
    sym = "💎" if curr=="ton" else "⭐️"
    g_e = {"mode": "⚔️ Монетка", "bet": rm["amount"], "win": pot, "mult": 2.0, "symbol": sym}
    
    c = conn.cursor()
    if curr == 'ton': c.execute("UPDATE users SET balance_ton = ? WHERE user_id = ?", (round(w_ud[1] + pot, 2), win_id))
    else: c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (int(w_ud[0] + pot), win_id))
    conn.commit()
    
    update_user_bulk_db(win_id, [('total_games', 1, 'add'), ('total_profit', rm["amount"], 'add')], g_e)
    update_user_bulk_db(los_id, [('total_games', 1, 'add'), ('total_profit', -rm["amount"], 'add')], {"mode": "⚔️ Монетка", "bet": rm["amount"], "win": 0, "mult": 0, "symbol": sym})
    
    del game.coinflip_rooms[r_id]
    
    # Обновляем балансы (если онлайн)
    for target_id in [win_id, los_id]:
        if target_id in game.connections:
            new_ud = get_user_db(target_id)
            asyncio.run_coroutine_threadsafe(game.connections[target_id].send_json({"type": "userData", "balance": new_ud[0], "balance_ton": new_ud[1]}), asyncio.get_event_loop())

    asyncio.run_coroutine_threadsafe(broadcast({"type": "coinflip_result", "room_id": r_id, "winning_side": win_side, "winner_name": w_ud[4], "pot": pot, "symbol": sym}), asyncio.get_event_loop())
    asyncio.run_coroutine_threadsafe(broadcast({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())}), asyncio.get_event_loop())