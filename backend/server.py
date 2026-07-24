import asyncio
import random
import sqlite3
import math
import time
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# Настройка БД в сверхбыстром режиме WAL
conn = sqlite3.connect("casino.db", check_same_thread=False)
conn.execute("PRAGMA journal_mode=WAL")
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

# Кэш истории (RAM)
cursor.execute("SELECT multiplier FROM crash_history ORDER BY id DESC LIMIT 50")
crash_history_cache = [r[0] for r in cursor.fetchall()]

def db_get_user(user_id, user_name=None, avatar=None):
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

def db_update_balance(user_id, balance=None, balance_ton=None):
    c = conn.cursor()
    if balance is not None:
        c.execute("UPDATE users SET balance = ? WHERE user_id = ?", (int(balance), user_id))
    if balance_ton is not None:
        c.execute("UPDATE users SET balance_ton = ? WHERE user_id = ?", (round(balance_ton, 2), user_id))
    conn.commit()

def db_save_crash(mult):
    global crash_history_cache
    c = conn.cursor()
    c.execute("INSERT INTO crash_history (multiplier, timestamp) VALUES (?, ?)", (mult, int(time.time())))
    conn.commit()
    crash_history_cache.insert(0, mult)
    crash_history_cache = crash_history_cache[:50]

def db_add_history_and_stats(user_id, mode, bet, win, mult, symbol, curr):
    c = conn.cursor()
    c.execute("SELECT balance, balance_ton, max_win, total_profit, total_games, game_history, total_mines_x FROM users WHERE user_id = ?", (user_id,))
    row = c.fetchone()
    if not row: return
    b, b_ton, mw, tp, tg, gh_json, tmx = row[0], row[1] or 100.0, row[2] or 0, row[3] or 0, row[4] or 0, row[5] or '[]', row[6] or 0.0
    
    gh = json.loads(gh_json)
    gh.insert(0, {"mode": mode, "bet": bet, "win": win, "mult": mult, "symbol": symbol, "time": int(time.time())})
    gh = gh[:15]
    
    profit = win - bet
    new_mw = max(mw, win)
    new_tg = tg + 1
    new_tp = tp + profit
    new_tmx = tmx + (mult if mode == "💣 Мины" and win > 0 else 0)

    if curr == "ton":
        new_bton = round(b_ton + win, 2)
        c.execute("UPDATE users SET balance_ton = ?, max_win = ?, total_profit = ?, total_games = ?, total_mines_x = ?, game_history = ? WHERE user_id = ?",
                  (new_bton, new_mw, new_tp, new_tg, new_tmx, json.dumps(gh), user_id))
    else:
        new_b = int(b + win)
        c.execute("UPDATE users SET balance = ?, max_win = ?, total_profit = ?, total_games = ?, total_mines_x = ?, game_history = ? WHERE user_id = ?",
                  (new_b, new_mw, new_tp, new_tg, new_tmx, json.dumps(gh), user_id))
    conn.commit()

def db_get_leaderboard():
    c = conn.cursor()
    c.execute("SELECT user_id, balance, balance_ton, user_name, avatar, custom_title, frame_color FROM users ORDER BY balance DESC LIMIT 10")
    rows = c.fetchall()
    return [{"user_id": r[0], "balance": r[1], "balance_ton": r[2] or 100.0, "user_name": r[3], "avatar": r[4], "custom_title": r[5], "frame_color": r[6]} for r in rows]

app = FastAPI()
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])

class GameState:
    def __init__(self):
        self.state = "idle"
        self.start_time = 0
        self.target_crash = 1.0
        self.flight_duration = 0
        self.active_bets = {}
        self.connections = {} 
        self.mines_games = {}
        self.coinflip_rooms = {}
        self.chat_messages = []
        self.jackpot_state = "idle"
        self.jackpot_bets = {}
        self.jackpot_timer = 15

game = GameState()

async def broadcast(message: dict):
    if not game.connections: return
    msg = json.dumps(message)
    for ws in list(game.connections.values()):
        try:
            await ws.send_text(msg)
        except:
            pass

# 🚀 АСИНХРОННЫЙ КРАШ ЛУП
async def crash_loop():
    while True:
        game.state = "idle"
        game.active_bets = {}
        game.start_time = time.time() + 5.0

        await broadcast({
            "type": "state_update", 
            "state": "idle", 
            "time_left": 5.0,
            "start_timestamp": game.start_time,
            "bets": {},
            "history": crash_history_cache
        })
        await asyncio.sleep(5.0)

        rand = random.random()
        game.target_crash = 1.0
        if rand > 0.03:
            game.target_crash = round(max(1.0, 0.99 / (1 - random.random())), 2)
            if game.target_crash > 100: game.target_crash = round(100 + random.random() * 50, 2)

        game.flight_duration = math.log(game.target_crash) / 0.15
        game.start_time = time.time()
        game.state = "running"

        await broadcast({
            "type": "state_update",
            "state": "running",
            "start_timestamp": game.start_time,
            "target_crash": game.target_crash,
            "flight_duration": game.flight_duration,
            "bets": game.active_bets
        })

        await asyncio.sleep(game.flight_duration)

        game.state = "crashed"
        await asyncio.to_thread(db_save_crash, game.target_crash)

        # Обработка сгоревших ставок
        for uid, bet in list(game.active_bets.items()):
            if bet["status"] == "playing":
                bet["status"] = "crashed"
                sym = "💎" if bet.get("currency") == "ton" else "⭐️"
                await asyncio.to_thread(db_add_history_and_stats, uid, "🚀 Краш", bet["amount"], 0, 0.0, sym, bet.get("currency"))

        await broadcast({
            "type": "state_update",
            "state": "crashed",
            "multiplier": game.target_crash,
            "bets": game.active_bets,
            "history": crash_history_cache
        })
        await asyncio.sleep(2.5)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(crash_loop())

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    game.connections[user_id] = websocket
    
    ud = await asyncio.to_thread(db_get_user, user_id)
    b, b_ton, inv, ld, u_name, u_ava, tg, mw, tp, ct, fc, tmx, gh = ud
    
    await websocket.send_json({
        "type": "userData", "balance": b, "balance_ton": b_ton, "inventory": inv,
        "stats": {"total_games": tg, "max_win": mw, "total_profit": tp, "total_mines_x": tmx},
        "game_history": gh, "custom_title": ct, "frame_color": fc
    })
    
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
            
            user_name = data.get("user_name", u_name)
            avatar = data.get("avatar", u_ava)

            # === 🚀 РАКЕТА ===
            if action == "bet":
                if game.state != "idle": continue
                amount = float(data.get("amount", 0))
                currency = data.get("currency", "stars")
                if amount <= 0: continue

                ud = await asyncio.to_thread(db_get_user, user_id, user_name, avatar)
                cur_b, cur_bton = ud[0], ud[1]

                can_bet = (cur_bton >= amount) if currency == "ton" else (cur_b >= amount)
                if can_bet:
                    new_b = cur_b if currency == "ton" else int(cur_b - amount)
                    new_bton = round(cur_bton - amount, 2) if currency == "ton" else cur_bton
                    
                    await asyncio.to_thread(db_update_balance, user_id, new_b if currency != "ton" else None, new_bton if currency == "ton" else None)
                    
                    game.active_bets[user_id] = {
                        "amount": amount, "currency": currency, "status": "playing", "win": 0,
                        "user_name": user_name, "avatar": avatar
                    }
                    
                    await websocket.send_json({"type": "userData", "balance": new_b, "balance_ton": new_bton})
                    await broadcast({"type": "bets_update", "bets": game.active_bets})

            elif action == "cashout":
                if game.state != "running": continue
                bet = game.active_bets.get(user_id)
                if not bet or bet["status"] != "playing": continue

                elapsed = time.time() - game.start_time
                current_mult = round(math.exp(0.15 * elapsed), 2)

                if current_mult >= game.target_crash: continue

                win_amount = round(bet["amount"] * current_mult, 2)
                curr = bet["currency"]
                bet["status"] = "cashed_out"
                bet["win"] = win_amount
                bet["m"] = current_mult

                sym = "💎" if curr == "ton" else "⭐️"
                await asyncio.to_thread(db_add_history_and_stats, user_id, "🚀 Краш", bet["amount"], win_amount, current_mult, sym, curr)

                ud_new = await asyncio.to_thread(db_get_user, user_id)
                await websocket.send_json({"type": "userData", "balance": ud_new[0], "balance_ton": ud_new[1], "game_history": ud_new[12]})
                await websocket.send_json({"type": "notify", "msg": f"🎯 Забрал {win_amount} {sym}! ({current_mult}x)"})
                await broadcast({"type": "bets_update", "bets": game.active_bets})

            # === 💣 МИНЫ ===
            elif action == "mines_start":
                bet = float(data.get("bet", 0))
                curr = data.get("currency", "stars")
                mines_cnt = int(data.get("mines", 3))
                grid_sz = int(data.get("grid_size", 5))
                total_cells = grid_sz * grid_sz

                ud = await asyncio.to_thread(db_get_user, user_id, user_name, avatar)
                bal = ud[1] if curr == "ton" else ud[0]

                if bet > 0 and bal >= bet and 1 <= mines_cnt < total_cells:
                    new_b = ud[0] if curr == "ton" else int(ud[0] - bet)
                    new_bton = round(ud[1] - bet, 2) if curr == "ton" else ud[1]
                    
                    await asyncio.to_thread(db_update_balance, user_id, new_b if curr != "ton" else None, new_bton if curr == "ton" else None)

                    grid = ["gem"] * total_cells
                    for idx in random.sample(range(total_cells), mines_cnt): grid[idx] = "mine"
                    
                    next_m = round(total_cells / (total_cells - mines_cnt) * 0.96, 2)

                    game.mines_games[user_id] = {
                        "bet": bet, "curr": curr, "m": mines_cnt, "sz": grid_sz,
                        "total_cells": total_cells, "grid": grid, "opened": [], "status": "playing"
                    }

                    await websocket.send_json({"type": "userData", "balance": new_b, "balance_ton": new_bton})
                    await websocket.send_json({"type": "mines_state", "status": "playing", "opened": [], "mult": 1.0, "next_mult": next_m, "win": 0, "grid_size": grid_sz})

            elif action == "mines_open":
                idx = data.get("cell")
                gm = game.mines_games.get(user_id)
                if gm and gm["status"] == "playing" and idx not in gm["opened"]:
                    curr = gm["curr"]
                    if gm["grid"][idx] == "mine":
                        gm["status"] = "crashed"
                        sym = "💎" if curr == "ton" else "⭐️"
                        await asyncio.to_thread(db_add_history_and_stats, user_id, "💣 Мины", gm["bet"], 0, 0.0, sym, curr)
                        
                        ud_new = await asyncio.to_thread(db_get_user, user_id)
                        await websocket.send_json({"type": "userData", "balance": ud_new[0], "balance_ton": ud_new[1], "game_history": ud_new[12]})
                        await websocket.send_json({"type": "mines_state", "status": "crashed", "grid": gm["grid"], "opened": gm["opened"] + [idx]})
                    else:
                        gm["opened"].append(idx)
                        opened_cnt = len(gm["opened"])
                        
                        mult = 1.0
                        for i in range(opened_cnt): mult *= (gm["total_cells"] - i) / (gm["total_cells"] - gm["m"] - i)
                        mult = round(mult * 0.96, 2)

                        next_mult = 1.0
                        for i in range(opened_cnt + 1): next_mult *= (gm["total_cells"] - i) / (gm["total_cells"] - gm["m"] - i)
                        next_mult = round(next_mult * 0.96, 2)

                        win = round(gm["bet"] * mult, 2)
                        await websocket.send_json({"type": "mines_state", "status": "playing", "opened": gm["opened"], "mult": mult, "next_mult": next_mult, "win": win})

            elif action == "mines_cashout":
                gm = game.mines_games.get(user_id)
                if gm and gm["status"] == "playing" and len(gm["opened"]) > 0:
                    opened_cnt = len(gm["opened"])
                    mult = 1.0
                    for i in range(opened_cnt): mult *= (gm["total_cells"] - i) / (gm["total_cells"] - gm["m"] - i)
                    mult = round(mult * 0.96, 2)

                    win_amount = round(gm["bet"] * mult, 2)
                    curr = gm["curr"]
                    sym = "💎" if curr == "ton" else "⭐️"
                    gm["status"] = "cashed_out"

                    await asyncio.to_thread(db_add_history_and_stats, user_id, "💣 Мины", gm["bet"], win_amount, mult, sym, curr)
                    
                    ud_new = await asyncio.to_thread(db_get_user, user_id)
                    await websocket.send_json({"type": "userData", "balance": ud_new[0], "balance_ton": ud_new[1], "game_history": ud_new[12]})
                    await websocket.send_json({"type": "mines_state", "status": "cashed_out", "grid": gm["grid"], "win": win_amount})
                    await websocket.send_json({"type": "notify", "msg": f"💣 МИНЫ: Забрал +{win_amount} {sym} ({mult}x)!"})

            # === ⚔️ COINFLIP ===
            elif action == "create_coinflip":
                amount = float(data.get("amount", 0))
                curr = data.get("currency", "stars")
                side = data.get("side", "eagle")

                ud = await asyncio.to_thread(db_get_user, user_id, user_name, avatar)
                bal = ud[1] if curr == "ton" else ud[0]

                if amount > 0 and bal >= amount:
                    new_b = ud[0] if curr == "ton" else int(ud[0] - amount)
                    new_bton = round(ud[1] - amount, 2) if curr == "ton" else ud[1]
                    await asyncio.to_thread(db_update_balance, user_id, new_b if curr != "ton" else None, new_bton if curr == "ton" else None)

                    r_id = f"room_{user_id}_{int(time.time())}"
                    game.coinflip_rooms[r_id] = {
                        "room_id": r_id, "creator_id": user_id, "creator_name": user_name,
                        "creator_avatar": avatar, "amount": amount, "currency": curr, "side": side
                    }

                    await websocket.send_json({"type": "userData", "balance": new_b, "balance_ton": new_bton})
                    await broadcast({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())})

            elif action == "join_coinflip":
                r_id = data.get("room_id")
                rm = game.coinflip_rooms.get(r_id)
                if rm and rm["creator_id"] != user_id:
                    curr = rm["currency"]
                    ud = await asyncio.to_thread(db_get_user, user_id, user_name, avatar)
                    bal = ud[1] if curr == "ton" else ud[0]

                    if bal >= rm["amount"]:
                        new_b = ud[0] if curr == "ton" else int(ud[0] - rm["amount"])
                        new_bton = round(ud[1] - rm["amount"], 2) if curr == "ton" else ud[1]
                        await asyncio.to_thread(db_update_balance, user_id, new_b if curr != "ton" else None, new_bton if curr == "ton" else None)

                        win_side = random.choice(["eagle", "tails"])
                        total_pot = round(rm["amount"] * 2, 2)
                        
                        winner_id = rm["creator_id"] if win_side == rm["side"] else user_id
                        loser_id = user_id if winner_id == rm["creator_id"] else rm["creator_id"]

                        sym = "💎" if curr == "ton" else "⭐️"
                        await asyncio.to_thread(db_add_history_and_stats, winner_id, "⚔️ Монетка", rm["amount"], total_pot, 2.0, sym, curr)
                        await asyncio.to_thread(db_add_history_and_stats, loser_id, "⚔️ Монетка", rm["amount"], 0, 0.0, sym, curr)

                        del game.coinflip_rooms[r_id]

                        for uid in [winner_id, loser_id]:
                            if uid in game.connections:
                                ud_u = await asyncio.to_thread(db_get_user, uid)
                                await game.connections[uid].send_json({"type": "userData", "balance": ud_u[0], "balance_ton": ud_u[1], "game_history": ud_u[12]})

                        w_ud = await asyncio.to_thread(db_get_user, winner_id)
                        await broadcast({
                            "type": "coinflip_result", "room_id": r_id, "winning_side": win_side,
                            "winner_id": winner_id, "winner_name": w_ud[4], "pot": total_pot, "symbol": sym
                        })
                        await broadcast({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())})

            # === 💎 ПОПОЛНЕНИЯ И ТОП ===
            elif action == "topup_ton":
                ud = await asyncio.to_thread(db_get_user, user_id)
                new_bton = round(ud[1] + 10.0, 2)
                await asyncio.to_thread(db_update_balance, user_id, None, new_bton)
                await websocket.send_json({"type": "userData", "balance": ud[0], "balance_ton": new_bton})
                await websocket.send_json({"type": "notify", "msg": "💎 Начислено +10.0 TON!"})

            elif action == "topup":
                ud = await asyncio.to_thread(db_get_user, user_id)
                new_b = ud[0] + 1000000000
                await asyncio.to_thread(db_update_balance, user_id, new_b, None)
                await websocket.send_json({"type": "userData", "balance": new_b, "balance_ton": ud[1]})
                await websocket.send_json({"type": "notify", "msg": "🤑 Начислено +1 000 000 000 ⭐️!"})

            elif action == "get_leaderboard":
                lb = await asyncio.to_thread(db_get_leaderboard)
                await websocket.send_json({"type": "leaderboard", "data": lb})

    except WebSocketDisconnect:
        if user_id in game.connections: del game.connections[user_id]