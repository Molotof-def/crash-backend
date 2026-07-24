import asyncio
import random
import math
import time
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ADMIN_ID = "5103088337"

users_db = {}
crash_history_ram = [1.25, 3.40, 1.10, 5.50, 2.10, 1.05]

def get_or_create_user(user_id, user_name="Игрок", avatar="👤"):
    uid = str(user_id)
    if uid not in users_db:
        # Для обычных игроков стартовый баланс = 0
        init_bal = 999999999999 if uid == ADMIN_ID else 0
        init_ton = 9999.0 if uid == ADMIN_ID else 0.0
        
        users_db[uid] = {
            "user_id": uid,
            "balance": init_bal,
            "balance_ton": init_ton,
            "user_name": user_name,
            "avatar": avatar,
            "is_admin": (uid == ADMIN_ID),
            "stats": {"total_games": 0, "max_win": 0, "total_profit": 0, "total_mines_x": 0.0},
            "game_history": []
        }
    else:
        if user_name and user_name != "Игрок": users_db[uid]["user_name"] = user_name
        if avatar and avatar != "👤": users_db[uid]["avatar"] = avatar
    return users_db[uid]

def add_user_history(uid, mode, bet, win, mult, symbol):
    u = get_or_create_user(uid)
    entry = {"mode": mode, "bet": bet, "win": win, "mult": mult, "symbol": symbol, "time": int(time.time())}
    u["game_history"].insert(0, entry)
    u["game_history"] = u["game_history"][:15]
    u["stats"]["total_games"] += 1
    u["stats"]["total_profit"] += (win - bet)
    if win > u["stats"]["max_win"]: u["stats"]["max_win"] = win

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

game = GameState()

async def broadcast(message: dict):
    if not game.connections: return
    msg = json.dumps(message)
    for ws in list(game.connections.values()):
        try: await ws.send_text(msg)
        except: pass

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
            "history": crash_history_ram
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
        crash_history_ram.insert(0, game.target_crash)
        if len(crash_history_ram) > 50: crash_history_ram.pop()

        for uid, bet in list(game.active_bets.items()):
            if bet["status"] == "playing":
                bet["status"] = "crashed"
                sym = "💎" if bet.get("currency") == "ton" else "⭐️"
                add_user_history(uid, "🚀 Краш", bet["amount"], 0, 0.0, sym)

        await broadcast({
            "type": "state_update",
            "state": "crashed",
            "multiplier": game.target_crash,
            "bets": game.active_bets,
            "history": crash_history_ram
        })
        await asyncio.sleep(2.5)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(crash_loop())

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    uid = str(user_id)
    game.connections[uid] = websocket
    u = get_or_create_user(uid)
    
    await websocket.send_json({
        "type": "userData", "balance": u["balance"], "balance_ton": u["balance_ton"],
        "is_admin": u["is_admin"], "stats": u["stats"], "game_history": u["game_history"]
    })
    
    current_elapsed = time.time() - game.start_time
    await websocket.send_json({
        "type": "state_update", 
        "state": game.state,
        "time_left": max(0, game.start_time - time.time()) if game.state == 'idle' else 0,
        "start_timestamp": game.start_time,
        "multiplier": round(math.exp(0.15 * current_elapsed), 2) if game.state == 'running' else 1.0,
        "target_crash": game.target_crash if game.state == 'running' else 0,
        "history": crash_history_ram,
        "bets": game.active_bets
    })
    await websocket.send_json({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())})

    try:
        while True:
            raw_data = await websocket.receive_text()
            data = json.loads(raw_data)
            action = data.get("action")
            u = get_or_create_user(uid, data.get("user_name"), data.get("avatar"))

            # === 👑 АДМИНСКИЕ ФУНКЦИИ (Только для ID 5103088337) ===
            if action == "admin_add_stars":
                if uid == ADMIN_ID:
                    u["balance"] += 1000000000
                    await websocket.send_json({"type": "userData", "balance": u["balance"], "balance_ton": u["balance_ton"]})
                    await websocket.send_json({"type": "notify", "msg": "👑 ВЫДАН БЕСКОНЕЧНЫЙ БАЛАНС STARS!"})

            elif action == "admin_add_ton":
                if uid == ADMIN_ID:
                    u["balance_ton"] += 1000.0
                    await websocket.send_json({"type": "userData", "balance": u["balance"], "balance_ton": u["balance_ton"]})
                    await websocket.send_json({"type": "notify", "msg": "👑 ВЫДАН БЕСКОНЕЧНЫЙ БАЛАНС TON!"})

            # === 🚀 РАКЕТА ===
            elif action == "bet":
                if game.state != "idle": continue
                amount = float(data.get("amount", 0))
                currency = data.get("currency", "stars")
                if amount <= 0: continue

                can_bet = (u["balance_ton"] >= amount) if currency == "ton" else (u["balance"] >= amount)
                if can_bet:
                    if currency == "ton": u["balance_ton"] = round(u["balance_ton"] - amount, 2)
                    else: u["balance"] = int(u["balance"] - amount)
                    
                    game.active_bets[uid] = {
                        "amount": amount, "currency": currency, "status": "playing", "win": 0,
                        "user_name": u["user_name"], "avatar": u["avatar"]
                    }
                    await websocket.send_json({"type": "userData", "balance": u["balance"], "balance_ton": u["balance_ton"]})
                    await broadcast({"type": "bets_update", "bets": game.active_bets})

            elif action == "cashout":
                if game.state != "running": continue
                bet = game.active_bets.get(uid)
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
                if curr == "ton": u["balance_ton"] = round(u["balance_ton"] + win_amount, 2)
                else: u["balance"] = int(u["balance"] + win_amount)

                add_user_history(uid, "🚀 Краш", bet["amount"], win_amount, current_mult, sym)

                await websocket.send_json({"type": "userData", "balance": u["balance"], "balance_ton": u["balance_ton"], "game_history": u["game_history"]})
                await websocket.send_json({"type": "notify", "msg": f"🎯 Забрал {win_amount} {sym}! ({current_mult}x)"})
                await broadcast({"type": "bets_update", "bets": game.active_bets})

            # === 💣 МИНЫ ===
            elif action == "mines_start":
                bet = float(data.get("bet", 0))
                curr = data.get("currency", "stars")
                mines_cnt = int(data.get("mines", 3))
                grid_sz = int(data.get("grid_size", 5))
                total_cells = grid_sz * grid_sz
                bal = u["balance_ton"] if curr == "ton" else u["balance"]

                if bet > 0 and bal >= bet and 1 <= mines_cnt < total_cells:
                    if curr == "ton": u["balance_ton"] = round(u["balance_ton"] - bet, 2)
                    else: u["balance"] = int(u["balance"] - bet)

                    grid = ["gem"] * total_cells
                    for idx in random.sample(range(total_cells), mines_cnt): grid[idx] = "mine"
                    next_m = round(total_cells / (total_cells - mines_cnt) * 0.96, 2)

                    game.mines_games[uid] = {
                        "bet": bet, "curr": curr, "m": mines_cnt, "sz": grid_sz,
                        "total_cells": total_cells, "grid": grid, "opened": [], "status": "playing"
                    }

                    await websocket.send_json({"type": "userData", "balance": u["balance"], "balance_ton": u["balance_ton"]})
                    await websocket.send_json({"type": "mines_state", "status": "playing", "opened": [], "grid": [], "mult": 1.0, "next_mult": next_m, "win": 0, "grid_size": grid_sz})

            elif action == "mines_open":
                idx = data.get("cell")
                gm = game.mines_games.get(uid)
                if gm and gm["status"] == "playing" and idx not in gm["opened"]:
                    curr = gm["curr"]
                    if gm["grid"][idx] == "mine":
                        gm["status"] = "crashed"
                        all_opened = list(set(gm["opened"] + [idx]))
                        sym = "💎" if curr == "ton" else "⭐️"
                        add_user_history(uid, "💣 Мины", gm["bet"], 0, 0.0, sym)
                        await websocket.send_json({"type": "userData", "balance": u["balance"], "balance_ton": u["balance_ton"], "game_history": u["game_history"]})
                        await websocket.send_json({"type": "mines_state", "status": "crashed", "grid": gm["grid"], "opened": all_opened, "grid_size": gm["sz"]})
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
                        await websocket.send_json({"type": "mines_state", "status": "playing", "opened": gm["opened"], "grid": [], "mult": mult, "next_mult": next_mult, "win": win, "grid_size": gm["sz"]})

            elif action == "mines_cashout":
                gm = game.mines_games.get(uid)
                if gm and gm["status"] == "playing" and len(gm["opened"]) > 0:
                    opened_cnt = len(gm["opened"])
                    mult = 1.0
                    for i in range(opened_cnt): mult *= (gm["total_cells"] - i) / (gm["total_cells"] - gm["m"] - i)
                    mult = round(mult * 0.96, 2)

                    win_amount = round(gm["bet"] * mult, 2)
                    curr = gm["curr"]
                    sym = "💎" if curr == "ton" else "⭐️"
                    gm["status"] = "cashed_out"

                    if curr == "ton": u["balance_ton"] = round(u["balance_ton"] + win_amount, 2)
                    else: u["balance"] = int(u["balance"] + win_amount)
                    u["stats"]["total_mines_x"] += mult

                    add_user_history(uid, "💣 Мины", gm["bet"], win_amount, mult, sym)
                    await websocket.send_json({"type": "userData", "balance": u["balance"], "balance_ton": u["balance_ton"], "game_history": u["game_history"]})
                    await websocket.send_json({"type": "mines_state", "status": "cashed_out", "grid": gm["grid"], "opened": gm["opened"], "win": win_amount, "grid_size": gm["sz"]})
                    await websocket.send_json({"type": "notify", "msg": f"💣 МИНЫ: Забрал +{win_amount} {sym} ({mult}x)!"})

            elif action == "get_leaderboard":
                lb = list(users_db.values())
                lb.sort(key=lambda x: x["balance"], reverse=True)
                await websocket.send_json({"type": "leaderboard", "data": lb[:10]})

    except WebSocketDisconnect:
        if uid in game.connections: del game.connections[uid]