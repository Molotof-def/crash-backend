import asyncio
import random
import sqlite3
import math
import time
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

conn = sqlite3.connect("casino.db", check_same_thread=False)
cursor = conn.cursor()

cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 10000000000,
        inventory TEXT DEFAULT '[]',
        last_daily INTEGER DEFAULT 0,
        user_name TEXT DEFAULT 'Игрок',
        avatar TEXT DEFAULT '👤',
        total_games INTEGER DEFAULT 0,
        max_win INTEGER DEFAULT 0,
        total_profit INTEGER DEFAULT 0,
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

for col in [
    "user_name TEXT DEFAULT 'Игрок'", "avatar TEXT DEFAULT '👤'", 
    "total_games INTEGER DEFAULT 0", "max_win INTEGER DEFAULT 0", 
    "total_profit INTEGER DEFAULT 0", "custom_title TEXT DEFAULT ''", 
    "frame_color TEXT DEFAULT ''", "total_mines_x REAL DEFAULT 0.0",
    "game_history TEXT DEFAULT '[]'"
]:
    try:
        cursor.execute(f"ALTER TABLE users ADD COLUMN {col}")
        conn.commit()
    except:
        pass

def save_crash_result(mult: float):
    cursor.execute("INSERT INTO crash_history (multiplier, timestamp) VALUES (?, ?)", (mult, int(time.time())))
    conn.commit()

def get_recent_history(limit=50):
    cursor.execute("SELECT multiplier FROM crash_history ORDER BY id DESC LIMIT ?", (limit,))
    rows = cursor.fetchall()
    return [r[0] for r in rows]

def get_or_create_user(user_id: int, user_name: str = None, avatar: str = None):
    cursor.execute("SELECT balance, inventory, last_daily, user_name, avatar, total_games, max_win, total_profit, custom_title, frame_color, total_mines_x, game_history FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        b, inv, ld, un, av, tg, mw, tp, ct, fc, tmx, gh = row[0], json.loads(row[1]), row[2], row[3], row[4], row[5], row[6], row[7], row[8] or '', row[9] or '', row[10] or 0.0, json.loads(row[11] or '[]')
        if user_name or avatar:
            un = user_name or un
            av = avatar or av
            cursor.execute("UPDATE users SET user_name = ?, avatar = ? WHERE user_id = ?", (un, av, user_id))
            conn.commit()
        return b, inv, ld, un, av, tg, mw, tp, ct, fc, tmx, gh
    
    un = user_name or f"Игрок #{str(user_id)[-4:]}"
    av = avatar or "👤"
    cursor.execute("INSERT INTO users (user_id, balance, inventory, last_daily, user_name, avatar, total_games, max_win, total_profit, custom_title, frame_color, total_mines_x, game_history) VALUES (?, ?, ?, ?, ?, ?, 0, 0, 0, '', '', 0.0, '[]')",
                   (user_id, 10000000000, '[]', 0, un, av))
    conn.commit()
    return 10000000000, [], 0, un, av, 0, 0, 0, '', '', 0.0, []

def add_user_game_history(user_id: int, mode: str, bet: int, win: int, mult: float):
    b, inv, ld, un, av, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id)
    new_entry = {
        "mode": mode,
        "bet": bet,
        "win": win,
        "mult": mult,
        "time": int(time.time())
    }
    gh.insert(0, new_entry)
    gh = gh[:15] # храним последние 15 игр
    cursor.execute("UPDATE users SET game_history = ? WHERE user_id = ?", (json.dumps(gh), user_id))
    conn.commit()

def update_user_data(user_id: int, balance: int = None, inventory: list = None, last_daily: int = None, user_name: str = None, avatar: str = None, games_add: int = 0, win_amount: int = 0, profit_add: int = 0, custom_title: str = None, frame_color: str = None, add_mines_x: float = 0.0):
    cursor.execute("SELECT balance, total_games, max_win, total_profit, custom_title, frame_color, total_mines_x FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if not row:
        return
    cur_bal, cur_tg, cur_mw, cur_tp, cur_ct, cur_fc, cur_tmx = row[0], row[1] or 0, row[2] or 0, row[3] or 0, row[4] or '', row[5] or '', row[6] or 0.0

    new_bal = balance if balance is not None else cur_bal
    new_tg = cur_tg + games_add
    new_mw = max(cur_mw, win_amount)
    new_tp = cur_tp + profit_add
    new_ct = custom_title if custom_title is not None else cur_ct
    new_fc = frame_color if frame_color is not None else cur_fc
    new_tmx = cur_tmx + add_mines_x

    cursor.execute("""
        UPDATE users 
        SET balance = ?, total_games = ?, max_win = ?, total_profit = ?, custom_title = ?, frame_color = ?, total_mines_x = ?
        WHERE user_id = ?
    """, (new_bal, new_tg, new_mw, new_tp, new_ct, new_fc, new_tmx, user_id))
    
    if inventory is not None:
        cursor.execute("UPDATE users SET inventory = ? WHERE user_id = ?", (json.dumps(inventory), user_id))
    if last_daily is not None:
        cursor.execute("UPDATE users SET last_daily = ? WHERE user_id = ?", (last_daily, user_id))
    if user_name is not None:
        cursor.execute("UPDATE users SET user_name = ? WHERE user_id = ?", (user_name, user_id))
    if avatar is not None:
        cursor.execute("UPDATE users SET avatar = ? WHERE user_id = ?", (avatar, user_id))
    conn.commit()

def get_leaderboard():
    cursor.execute("SELECT user_id, balance, user_name, avatar, custom_title, frame_color FROM users ORDER BY balance DESC LIMIT 10")
    rows = cursor.fetchall()
    return [{"user_id": r[0], "balance": r[1], "user_name": r[2], "avatar": r[3], "custom_title": r[4], "frame_color": r[5]} for r in rows]

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

class GameState:
    def __init__(self):
        self.state = "idle"
        self.multiplier = 1.0
        self.target_crash = 1.0
        self.active_bets = {}
        
        self.connections = {} 
        self.mines_games = {}
        self.coinflip_rooms = {}
        self.chat_messages = []
        
        self.jackpot_state = "idle"
        self.jackpot_bets = {}      
        self.jackpot_timer = 15
        self.jackpot_winner = None

game = GameState()

def calculate_mines_mult(total_cells, mines_count, opened_count):
    if opened_count == 0:
        return 1.0
    mult = 1.0
    for i in range(opened_count):
        mult *= (total_cells - i) / (total_cells - mines_count - i)
    return round(mult * 0.96, 2)

async def broadcast(message: dict):
    for ws in list(game.connections.values()):
        try:
            await ws.send_json(message)
        except:
            pass

async def crash_loop():
    while True:
        game.state = "idle"
        game.multiplier = 1.0
        game.active_bets = {}
        
        idle_duration = 5.0
        start_idle = time.time()
        while time.time() - start_idle < idle_duration:
            time_left = round(idle_duration - (time.time() - start_idle), 1)
            await broadcast({
                "type": "state_update", 
                "state": "idle", 
                "multiplier": 1.0, 
                "time_left": time_left, 
                "bets": game.active_bets
            })
            await asyncio.sleep(0.1)

        rand = random.random()
        game.target_crash = 1.0
        if rand > 0.05:
            game.target_crash = round(1 + math.pow(random.random(), 3) * 30, 2)

        game.state = "running"
        start_time = time.time()
        
        while game.state == "running":
            elapsed = time.time() - start_time
            current_mult = round(math.exp(0.15 * elapsed), 2)

            if current_mult >= game.target_crash:
                game.multiplier = game.target_crash
                game.state = "crashed"
                save_crash_result(game.target_crash)
                
                for uid, bet in game.active_bets.items():
                    if bet["status"] == "playing":
                        bet["status"] = "crashed"
                        update_user_data(uid, games_add=1, profit_add=-bet["amount"])
                        add_user_game_history(uid, "🚀 Краш", bet["amount"], 0, 0.0)
            else:
                game.multiplier = current_mult
                for uid, bet in list(game.active_bets.items()):
                    if bet["status"] == "playing" and bet["auto_cashout"] and current_mult >= bet["auto_cashout"]:
                        win_amount = int(bet["amount"] * bet["auto_cashout"])
                        b, inv, ld, un, av, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(uid)
                        new_bal = b + win_amount
                        profit = win_amount - bet["amount"]
                        update_user_data(uid, balance=new_bal, games_add=1, win_amount=win_amount, profit_add=profit)
                        add_user_game_history(uid, "🚀 Краш", bet["amount"], win_amount, bet["auto_cashout"])
                        
                        bet["status"] = "cashed_out"
                        bet["win"] = win_amount
                        if uid in game.connections:
                            b_cur, _, _, _, _, tg_c, mw_c, tp_c, ct_c, fc_c, tmx_c, gh_c = get_or_create_user(uid)
                            await game.connections[uid].send_json({
                                "type": "userData", "balance": b_cur, "inventory": inv,
                                "stats": {"total_games": tg_c, "max_win": mw_c, "total_profit": tp_c, "total_mines_x": tmx_c},
                                "game_history": gh_c, "custom_title": ct_c, "frame_color": fc_c
                            })
                            await game.connections[uid].send_json({"type": "notify", "msg": f"🎯 Автовывод на {bet['auto_cashout']}x! (+{win_amount} ⭐️)"})

            await broadcast({
                "type": "state_update", 
                "state": game.state, 
                "multiplier": game.multiplier, 
                "bets": game.active_bets,
                "history": get_recent_history()
            })
            if game.state == "crashed":
                break
            await asyncio.sleep(0.15)

        await asyncio.sleep(2.5)

async def jackpot_loop():
    while True:
        if game.jackpot_state == "idle":
            if len(game.jackpot_bets) >= 2:
                for t in range(15, -1, -1):
                    game.jackpot_timer = t
                    await broadcast({"type": "jackpot_update", "state": "idle", "bets": game.jackpot_bets, "timer": t})
                    await asyncio.sleep(1)
                
                game.jackpot_state = "spinning"
                total_pot = sum(b["amount"] for b in game.jackpot_bets.values())
                
                pick = random.randint(1, total_pot)
                current = 0
                winner_id = None
                
                for uid, bet in game.jackpot_bets.items():
                    current += bet["amount"]
                    if pick <= current:
                        winner_id = uid
                        break

                winner_bet = game.jackpot_bets[winner_id]
                game.jackpot_winner = winner_bet

                await broadcast({
                    "type": "jackpot_update", 
                    "state": "spinning", 
                    "bets": game.jackpot_bets, 
                    "winner": winner_bet,
                    "pot": total_pot
                })
                
                await asyncio.sleep(6)

                b, inv, ld, un, av, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(winner_id)
                new_bal = b + total_pot
                profit = total_pot - winner_bet["amount"]
                update_user_data(winner_id, balance=new_bal, games_add=1, win_amount=total_pot, profit_add=profit)
                add_user_game_history(winner_id, "🥏 Карта Зон", winner_bet["amount"], total_pot, round(total_pot / winner_bet["amount"], 2))

                for uid, bet in game.jackpot_bets.items():
                    if uid != winner_id:
                        add_user_game_history(uid, "🥏 Карта Зон", bet["amount"], 0, 0.0)

                if winner_id in game.connections:
                    b_cur, _, _, _, _, tg_c, mw_c, tp_c, ct_c, fc_c, tmx_c, gh_c = get_or_create_user(winner_id)
                    await game.connections[winner_id].send_json({
                        "type": "userData", "balance": b_cur, "inventory": inv,
                        "stats": {"total_games": tg_c, "max_win": mw_c, "total_profit": tp_c, "total_mines_x": tmx_c},
                        "game_history": gh_c, "custom_title": ct_c, "frame_color": fc_c
                    })
                    await game.connections[winner_id].send_json({"type": "notify", "msg": f"👑 ШАЙБА ВЫБРАЛА ТЕБЯ!\nЗабрал банк: +{total_pot} ⭐️!"})

                game.jackpot_bets = {}
                game.jackpot_state = "idle"
                game.jackpot_timer = 15
            else:
                await broadcast({"type": "jackpot_update", "state": "waiting", "bets": game.jackpot_bets, "timer": 15})
                await asyncio.sleep(1)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(crash_loop())
    asyncio.create_task(jackpot_loop())

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    game.connections[user_id] = websocket
    
    b, inv, ld, u_name, u_ava, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id)
    await websocket.send_json({
        "type": "userData", "balance": b, "inventory": inv,
        "stats": {"total_games": tg, "max_win": mw, "total_profit": tp, "total_mines_x": tmx},
        "game_history": gh, "custom_title": ct, "frame_color": fc
    })
    await websocket.send_json({"type": "chat_history", "messages": game.chat_messages})
    await websocket.send_json({"type": "leaderboard", "data": get_leaderboard()})
    await websocket.send_json({"type": "crash_history_init", "history": get_recent_history()})
    await websocket.send_json({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())})

    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            
            user_name = data.get("user_name", u_name)
            avatar = data.get("avatar", u_ava)

            if action == "bet":
                amount = data.get("amount", 0)
                auto_cashout = data.get("auto_cashout", None)
                
                b, inv, ld, _, _, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id, user_name, avatar)
                if game.state == "idle" and amount > 0 and b >= amount:
                    new_bal = b - amount
                    update_user_data(user_id, balance=new_bal)
                    
                    game.active_bets[user_id] = {
                        "amount": amount, "auto_cashout": auto_cashout,
                        "status": "playing", "win": 0,
                        "user_name": user_name, "avatar": avatar
                    }
                    await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv, "stats": {"total_games": tg, "max_win": mw, "total_profit": tp, "total_mines_x": tmx}, "game_history": gh, "custom_title": ct, "frame_color": fc})
                    await broadcast({"type": "bets_update", "bets": game.active_bets})
                    
            elif action == "cashout":
                if game.state == "running" and user_id in game.active_bets:
                    bet = game.active_bets[user_id]
                    if bet["status"] == "playing":
                        win_amount = int(bet["amount"] * game.multiplier)
                        b, inv, ld, _, _, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id)
                        new_bal = b + win_amount
                        profit = win_amount - bet["amount"]
                        update_user_data(user_id, balance=new_bal, games_add=1, win_amount=win_amount, profit_add=profit)
                        add_user_game_history(user_id, "🚀 Краш", bet["amount"], win_amount, game.multiplier)
                        
                        bet["status"] = "cashed_out"
                        bet["win"] = win_amount
                        
                        b_cur, _, _, _, _, tg_c, mw_c, tp_c, ct_c, fc_c, tmx_c, gh_c = get_or_create_user(user_id)
                        await websocket.send_json({
                            "type": "userData", "balance": b_cur, "inventory": inv,
                            "stats": {"total_games": tg_c, "max_win": mw_c, "total_profit": tp_c, "total_mines_x": tmx_c},
                            "game_history": gh_c, "custom_title": ct_c, "frame_color": fc_c
                        })
                        await websocket.send_json({"type": "notify", "msg": f"Успешно забрал {win_amount} ⭐️"})
                        await broadcast({"type": "bets_update", "bets": game.active_bets})

            elif action == "mines_start":
                bet = data.get("bet", 0)
                mines_count = data.get("mines", 3)
                grid_size = data.get("grid_size", 5)
                total_cells = grid_size * grid_size
                
                b, inv, ld, _, _, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id, user_name, avatar)
                
                if bet > 0 and b >= bet and 1 <= mines_count < total_cells:
                    new_bal = b - bet
                    update_user_data(user_id, balance=new_bal)
                    
                    grid = ["gem"] * total_cells
                    for idx in random.sample(range(total_cells), mines_count):
                        grid[idx] = "mine"
                        
                    next_mult = calculate_mines_mult(total_cells, mines_count, 1)

                    game.mines_games[user_id] = {
                        "bet": bet, "mines_count": mines_count, "grid_size": grid_size, 
                        "total_cells": total_cells, "grid": grid, "opened": [], "status": "playing"
                    }
                    
                    await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv, "stats": {"total_games": tg, "max_win": mw, "total_profit": tp, "total_mines_x": tmx}, "game_history": gh, "custom_title": ct, "frame_color": fc})
                    await websocket.send_json({
                        "type": "mines_state", "status": "playing", "opened": [], 
                        "grid": [], "mult": 1.0, "next_mult": next_mult, "win": 0, "grid_size": grid_size
                    })

            elif action == "mines_open":
                cell_idx = data.get("cell")
                m_game = game.mines_games.get(user_id)
                if m_game and m_game["status"] == "playing" and cell_idx not in m_game["opened"]:
                    if m_game["grid"][cell_idx] == "mine":
                        m_game["status"] = "crashed"
                        update_user_data(user_id, games_add=1, profit_add=-m_game["bet"])
                        add_user_game_history(user_id, "💣 Мины", m_game["bet"], 0, 0.0)
                        
                        b_cur, _, _, _, _, tg_c, mw_c, tp_c, ct_c, fc_c, tmx_c, gh_c = get_or_create_user(user_id)
                        await websocket.send_json({"type": "userData", "balance": b_cur, "inventory": inv, "stats": {"total_games": tg_c, "max_win": mw_c, "total_profit": tp_c, "total_mines_x": tmx_c}, "game_history": gh_c, "custom_title": ct_c, "frame_color": fc_c})
                        await websocket.send_json({"type": "mines_state", "status": "crashed", "grid": m_game["grid"], "opened": m_game["opened"] + [cell_idx], "mult": 0, "next_mult": 0, "win": 0, "grid_size": m_game["grid_size"]})
                    else:
                        m_game["opened"].append(cell_idx)
                        mult = calculate_mines_mult(m_game["total_cells"], m_game["mines_count"], len(m_game["opened"]))
                        next_mult = calculate_mines_mult(m_game["total_cells"], m_game["mines_count"], len(m_game["opened"]) + 1)
                        win = int(m_game["bet"] * mult)
                        
                        await websocket.send_json({
                            "type": "mines_state", "status": "playing", "opened": m_game["opened"], 
                            "grid": [], "mult": mult, "next_mult": next_mult, "win": win, "grid_size": m_game["grid_size"]
                        })

            elif action == "mines_cashout":
                m_game = game.mines_games.get(user_id)
                if m_game and m_game["status"] == "playing" and len(m_game["opened"]) > 0:
                    mult = calculate_mines_mult(m_game["total_cells"], m_game["mines_count"], len(m_game["opened"]))
                    win_amount = int(m_game["bet"] * mult)
                    b, inv, ld, _, _, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id)
                    new_bal = b + win_amount
                    new_tmx = tmx + mult
                    
                    update_user_data(user_id, balance=new_bal, games_add=1, win_amount=win_amount, profit_add=win_amount - m_game["bet"], add_mines_x=mult)
                    add_user_game_history(user_id, "💣 Мины", m_game["bet"], win_amount, mult)
                    m_game["status"] = "cashed_out"
                    
                    b_cur, _, _, _, _, tg_c, mw_c, tp_c, ct_c, fc_c, tmx_c, gh_c = get_or_create_user(user_id)
                    await websocket.send_json({"type": "userData", "balance": b_cur, "inventory": inv, "stats": {"total_games": tg_c, "max_win": mw_c, "total_profit": tp_c, "total_mines_x": tmx_c}, "game_history": gh_c, "custom_title": ct_c, "frame_color": fc_c})
                    await websocket.send_json({"type": "mines_state", "status": "cashed_out", "grid": m_game["grid"], "opened": m_game["opened"], "mult": mult, "next_mult": 0, "win": win_amount, "grid_size": m_game["grid_size"]})
                    await websocket.send_json({"type": "notify", "msg": f"💣 МИНЫ: Забрал +{win_amount} ⭐️ ({mult}x)!"})

            elif action == "create_coinflip":
                amount = data.get("amount", 0)
                side = data.get("side", "eagle")
                b, inv, ld, _, _, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id, user_name, avatar)
                if amount > 0 and b >= amount:
                    new_bal = b - amount
                    update_user_data(user_id, balance=new_bal)
                    room_id = f"room_{user_id}_{int(time.time())}"
                    
                    game.coinflip_rooms[room_id] = {
                        "room_id": room_id,
                        "creator_id": user_id,
                        "creator_name": user_name,
                        "creator_avatar": avatar,
                        "amount": amount,
                        "side": side
                    }
                    
                    await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv, "stats": {"total_games": tg, "max_win": mw, "total_profit": tp, "total_mines_x": tmx}, "game_history": gh, "custom_title": ct, "frame_color": fc})
                    await broadcast({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())})

            elif action == "join_coinflip":
                room_id = data.get("room_id")
                room = game.coinflip_rooms.get(room_id)
                b, inv, ld, _, _, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id, user_name, avatar)
                
                if room and user_id != room["creator_id"] and b >= room["amount"]:
                    new_bal = b - room["amount"]
                    update_user_data(user_id, balance=new_bal)
                    
                    winning_side = random.choice(["eagle", "tails"])
                    total_pot = room["amount"] * 2
                    
                    winner_id = room["creator_id"] if winning_side == room["side"] else user_id
                    loser_id = user_id if winner_id == room["creator_id"] else room["creator_id"]
                    
                    wb, winv, wld, wun, wav, wtg, wmw, wtp, wct, wfc, wtmx, wgh = get_or_create_user(winner_id)
                    update_user_data(winner_id, balance=wb + total_pot, games_add=1, win_amount=total_pot, profit_add=room["amount"])
                    update_user_data(loser_id, games_add=1, profit_add=-room["amount"])

                    add_user_game_history(winner_id, "⚔️ Монетка", room["amount"], total_pot, 2.0)
                    add_user_game_history(loser_id, "⚔️ Монетка", room["amount"], 0, 0.0)

                    del game.coinflip_rooms[room_id]

                    await broadcast({
                        "type": "coinflip_result",
                        "room_id": room_id,
                        "winning_side": winning_side,
                        "winner_id": winner_id,
                        "winner_name": wun if winner_id == room["creator_id"] else user_name,
                        "pot": total_pot
                    })
                    await broadcast({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())})

            elif action == "use_promo":
                code = data.get("code", "").strip().lower()
                b, inv, ld, _, _, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id)
                promos = {"starbet": 100000000, "durov": 500000000, "casino": 1000000000, "ivan_vozduxan": 1000000000000}
                if code in promos:
                    bonus = promos[code]
                    new_bal = b + bonus
                    update_user_data(user_id, balance=new_bal)
                    await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv, "stats": {"total_games": tg, "max_win": mw, "total_profit": tp, "total_mines_x": tmx}, "game_history": gh, "custom_title": ct, "frame_color": fc})
                    await websocket.send_json({"type": "notify", "msg": f"🎉 ПРОМОКОД СРАБОТАЛ!\nНачислено +{bonus:,} ⭐️!"})
                else:
                    await websocket.send_json({"type": "notify", "msg": "❌ Неверный промокод!"})

            elif action == "topup":
                b, inv, ld, _, _, tg, mw, tp, ct, fc, tmx, gh = get_or_create_user(user_id)
                new_bal = b + 1000000000
                update_user_data(user_id, balance=new_bal)
                await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv, "stats": {"total_games": tg, "max_win": mw, "total_profit": tp, "total_mines_x": tmx}, "game_history": gh, "custom_title": ct, "frame_color": fc})
                await websocket.send_json({"type": "notify", "msg": "🤑 Начислено +1 000 000 000 ⭐️!"})

            elif action == "get_leaderboard":
                await websocket.send_json({"type": "leaderboard", "data": get_leaderboard()})

    except WebSocketDisconnect:
        if user_id in game.connections:
            del game.connections[user_id]