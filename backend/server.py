import asyncio
import random
import math
import time
import json
import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

BOT_TOKEN = "8945990343:AAEteeBQgW_qYc3QtvndTxvu58bcUf_OqIA"
ADMIN_ID = "5103088337"

app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

users_db = {}
crash_history_ram = [1.25, 3.40, 1.10, 5.50, 2.10, 1.05]

def get_or_create_user(user_id, user_name="Игрок", avatar="👤"):
    uid = str(user_id)
    if uid not in users_db:
        init_bal = 1000000000 if uid == ADMIN_ID else 100
        users_db[uid] = {
            "user_id": uid,
            "balance": init_bal,
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

def add_user_history(uid, mode, bet, win, mult):
    u = get_or_create_user(uid)
    entry = {"mode": mode, "bet": bet, "win": win, "mult": mult, "symbol": "⭐️", "time": int(time.time())}
    u["game_history"].insert(0, entry)
    u["game_history"] = u["game_history"][:15]
    u["stats"]["total_games"] += 1
    u["stats"]["total_profit"] += (win - bet)
    if win > u["stats"]["max_win"]: u["stats"]["max_win"] = win

# Генерация официальной ссылки оплаты Telegram Stars (XTR)
async def create_star_invoice_link(stars_amount: int, user_id: str):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/createInvoiceLink"
    payload = {
        "title": "Пополнение баланса",
        "description": f"Пополнение {stars_amount} ⭐️ в DOOM GIFT",
        "payload": json.dumps({"user_id": user_id, "stars": stars_amount}),
        "provider_token": "", # Пусто для Telegram Stars!
        "currency": "XTR",   # Валюта Telegram Stars
        "prices": [{"label": "Stars", "amount": stars_amount}]
    }
    async with httpx.AsyncClient() as client:
        res = await client.post(url, json=payload)
        data = res.json()
        if data.get("ok"):
            return data.get("result")
    return None

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
        self.jackpot_state = "idle"
        self.jackpot_bets = {}
        self.jackpot_timer = 15

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
        if rand > 0.05:
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
                add_user_history(uid, "🚀 Краш", bet["amount"], 0, 0.0)

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
        "type": "userData", "balance": u["balance"], "is_admin": u["is_admin"],
        "stats": u["stats"], "game_history": u["game_history"]
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

            # 💳 ЗАПРОС ОПЛАТЫ TELEGRAM STARS
            if action == "buy_stars_invoice":
                amount = int(data.get("stars", 50))
                invoice_link = await create_star_invoice_link(amount, uid)
                if invoice_link:
                    await websocket.send_json({"type": "open_invoice", "url": invoice_link, "stars": amount})
                else:
                    await websocket.send_json({"type": "notify", "msg": "❌ Ошибка создания счёта!"})

            # 👑 АДМИНКА (Только ID 5103088337)
            elif action == "admin_add_stars" and uid == ADMIN_ID:
                u["balance"] += 1000000000
                await websocket.send_json({"type": "userData", "balance": u["balance"]})
                await websocket.send_json({"type": "notify", "msg": "👑 ВЫДАН БАЛАНС 1.000.000.000 ⭐️!"})

            # 🚀 КРАШ
            elif action == "bet":
                if game.state != "idle": continue
                amount = int(data.get("amount", 0))
                if amount <= 0 or u["balance"] < amount: continue

                u["balance"] -= amount
                game.active_bets[uid] = {
                    "amount": amount, "status": "playing", "win": 0,
                    "user_name": u["user_name"], "avatar": u["avatar"]
                }
                await websocket.send_json({"type": "userData", "balance": u["balance"]})
                await broadcast({"type": "bets_update", "bets": game.active_bets})

            elif action == "cashout":
                if game.state != "running": continue
                bet = game.active_bets.get(uid)
                if not bet or bet["status"] != "playing": continue

                elapsed = time.time() - game.start_time
                current_mult = round(math.exp(0.15 * elapsed), 2)
                if current_mult >= game.target_crash: continue

                win_amount = int(bet["amount"] * current_mult)
                bet["status"] = "cashed_out"
                bet["win"] = win_amount
                bet["m"] = current_mult

                u["balance"] += win_amount
                add_user_history(uid, "🚀 Краш", bet["amount"], win_amount, current_mult)

                await websocket.send_json({"type": "userData", "balance": u["balance"], "game_history": u["game_history"]})
                await websocket.send_json({"type": "notify", "msg": f"🎯 Забрал {win_amount} ⭐️! ({current_mult}x)"})
                await broadcast({"type": "bets_update", "bets": game.active_bets})

            # 💣 МИНЫ
            elif action == "mines_start":
                bet = int(data.get("bet", 0))
                mines_cnt = int(data.get("mines", 3))
                grid_sz = int(data.get("grid_size", 5))
                total_cells = grid_sz * grid_sz

                if bet > 0 and u["balance"] >= bet and 1 <= mines_cnt < total_cells:
                    u["balance"] -= bet
                    grid = ["gem"] * total_cells
                    for idx in random.sample(range(total_cells), mines_cnt): grid[idx] = "mine"
                    
                    next_m = round(total_cells / (total_cells - mines_cnt) * 0.96, 2)
                    game.mines_games[uid] = {
                        "bet": bet, "m": mines_cnt, "sz": grid_sz,
                        "total_cells": total_cells, "grid": grid, "opened": [], "status": "playing"
                    }

                    await websocket.send_json({"type": "userData", "balance": u["balance"]})
                    await websocket.send_json({"type": "mines_state", "status": "playing", "opened": [], "grid": [], "mult": 1.0, "next_mult": next_m, "win": 0, "grid_size": grid_sz})

            elif action == "mines_open":
                idx = data.get("cell")
                gm = game.mines_games.get(uid)
                if gm and gm["status"] == "playing" and idx not in gm["opened"]:
                    if gm["grid"][idx] == "mine":
                        gm["status"] = "crashed"
                        all_opened = list(set(gm["opened"] + [idx]))
                        add_user_history(uid, "💣 Мины", gm["bet"], 0, 0.0)
                        
                        await websocket.send_json({"type": "userData", "balance": u["balance"], "game_history": u["game_history"]})
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

                        win = int(gm["bet"] * mult)
                        await websocket.send_json({"type": "mines_state", "status": "playing", "opened": gm["opened"], "grid": [], "mult": mult, "next_mult": next_mult, "win": win, "grid_size": gm["sz"]})

            elif action == "mines_cashout":
                gm = game.mines_games.get(uid)
                if gm and gm["status"] == "playing" and len(gm["opened"]) > 0:
                    opened_cnt = len(gm["opened"])
                    mult = 1.0
                    for i in range(opened_cnt): mult *= (gm["total_cells"] - i) / (gm["total_cells"] - gm["m"] - i)
                    mult = round(mult * 0.96, 2)

                    win_amount = int(gm["bet"] * mult)
                    gm["status"] = "cashed_out"

                    u["balance"] += win_amount
                    u["stats"]["total_mines_x"] += mult
                    add_user_history(uid, "💣 Мины", gm["bet"], win_amount, mult)

                    await websocket.send_json({"type": "userData", "balance": u["balance"], "game_history": u["game_history"]})
                    await websocket.send_json({"type": "mines_state", "status": "cashed_out", "grid": gm["grid"], "opened": gm["opened"], "win": win_amount, "grid_size": gm["sz"]})
                    await websocket.send_json({"type": "notify", "msg": f"💣 МИНЫ: Забрал +{win_amount} ⭐️ ({mult}x)!"})

            # ⚔️ COINFLIP
            elif action == "create_coinflip":
                amount = int(data.get("amount", 0))
                side = data.get("side", "eagle")

                if amount > 0 and u["balance"] >= amount:
                    u["balance"] -= amount
                    r_id = f"room_{uid}_{int(time.time())}"
                    game.coinflip_rooms[r_id] = {
                        "room_id": r_id, "creator_id": uid, "creator_name": u["user_name"],
                        "creator_avatar": u["avatar"], "amount": amount, "side": side
                    }
                    await websocket.send_json({"type": "userData", "balance": u["balance"]})
                    await broadcast({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())})

            elif action == "join_coinflip":
                r_id = data.get("room_id")
                rm = game.coinflip_rooms.get(r_id)
                if rm and rm["creator_id"] != uid and u["balance"] >= rm["amount"]:
                    u["balance"] -= rm["amount"]
                    win_side = random.choice(["eagle", "tails"])
                    total_pot = rm["amount"] * 2
                    
                    winner_id = rm["creator_id"] if win_side == rm["side"] else uid
                    loser_id = uid if winner_id == rm["creator_id"] else rm["creator_id"]

                    w_u = get_or_create_user(winner_id)
                    w_u["balance"] += total_pot

                    add_user_history(winner_id, "⚔️ Монетка", rm["amount"], total_pot, 2.0)
                    add_user_history(loser_id, "⚔️ Монетка", rm["amount"], 0, 0.0)

                    del game.coinflip_rooms[r_id]

                    for target_uid in [winner_id, loser_id]:
                        if target_uid in game.connections:
                            usr_obj = get_or_create_user(target_uid)
                            await game.connections[target_uid].send_json({"type": "userData", "balance": usr_obj["balance"], "game_history": usr_obj["game_history"]})

                    await broadcast({
                        "type": "coinflip_result", "room_id": r_id, "winning_side": win_side,
                        "winner_id": winner_id, "winner_name": w_u["user_name"], "pot": total_pot
                    })
                    await broadcast({"type": "coinflip_rooms", "rooms": list(game.coinflip_rooms.values())})

            elif action == "get_leaderboard":
                lb = list(users_db.values())
                lb.sort(key=lambda x: x["balance"], reverse=True)
                res = [{"user_id": x["user_id"], "user_name": x["user_name"], "avatar": x["avatar"], "balance": x["balance"]} for x in lb[:10]]
                await websocket.send_json({"type": "leaderboard", "data": res})

    except WebSocketDisconnect:
        if uid in game.connections: del game.connections[uid]