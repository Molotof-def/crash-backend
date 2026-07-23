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
        inventory TEXT DEFAULT '[]'
    )
""")
conn.commit()

def get_or_create_user(user_id: int):
    cursor.execute("SELECT balance, inventory FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        return row[0], json.loads(row[1])
    cursor.execute("INSERT INTO users (user_id, balance, inventory) VALUES (?, ?, ?)", (user_id, 10000000000, '[]'))
    conn.commit()
    return 10000000000, []

def update_user_data(user_id: int, balance: int = None, inventory: list = None):
    if balance is not None:
        cursor.execute("UPDATE users SET balance = ? WHERE user_id = ?", (balance, user_id))
    if inventory is not None:
        cursor.execute("UPDATE users SET inventory = ? WHERE user_id = ?", (json.dumps(inventory), user_id))
    conn.commit()

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

game = GameState()

def calculate_mines_mult(mines_count, opened_count):
    if opened_count == 0:
        return 1.0
    mult = 1.0
    total_cells = 25
    for i in range(opened_count):
        mult *= (total_cells - i) / (total_cells - mines_count - i)
    return round(mult * 0.97, 2)

async def broadcast(message: dict):
    for ws in list(game.connections.values()):
        try:
            await ws.send_json(message)
        except:
            pass

async def game_loop():
    while True:
        game.state = "idle"
        game.multiplier = 1.0
        game.active_bets = {}
        await broadcast({
            "type": "state_update", 
            "state": game.state, 
            "multiplier": game.multiplier,
            "bets": game.active_bets
        })
        await asyncio.sleep(5)

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
                for uid, bet in game.active_bets.items():
                    if bet["status"] == "playing":
                        bet["status"] = "crashed"
            else:
                game.multiplier = current_mult
                for uid, bet in list(game.active_bets.items()):
                    if bet["status"] == "playing" and bet["auto_cashout"] and current_mult >= bet["auto_cashout"]:
                        win_amount = int(bet["amount"] * bet["auto_cashout"])
                        bal, inv = get_or_create_user(uid)
                        new_bal = bal + win_amount
                        update_user_data(uid, balance=new_bal)
                        
                        bet["status"] = "cashed_out"
                        bet["win"] = win_amount
                        
                        if uid in game.connections:
                            await game.connections[uid].send_json({"type": "userData", "balance": new_bal, "inventory": inv})
                            await game.connections[uid].send_json({"type": "notify", "msg": f"🎯 Автовывод на {bet['auto_cashout']}x! (+{win_amount} ⭐️)"})

            await broadcast({
                "type": "state_update", 
                "state": game.state, 
                "multiplier": game.multiplier,
                "bets": game.active_bets
            })
            
            if game.state == "crashed":
                break
            
            await asyncio.sleep(0.1)

        await asyncio.sleep(3)

@app.on_event("startup")
async def startup_event():
    asyncio.create_task(game_loop())

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    game.connections[user_id] = websocket
    
    balance, inventory = get_or_create_user(user_id)
    await websocket.send_json({"type": "userData", "balance": balance, "inventory": inventory})
    
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            
            if action == "bet":
                amount = data.get("amount", 0)
                auto_cashout = data.get("auto_cashout", None)
                user_name = data.get("user_name", "Игрок")
                avatar = data.get("avatar", "👤")
                
                bal, inv = get_or_create_user(user_id)
                if game.state == "idle" and amount > 0 and bal >= amount:
                    new_bal = bal - amount
                    update_user_data(user_id, balance=new_bal)
                    
                    game.active_bets[user_id] = {
                        "amount": amount,
                        "auto_cashout": auto_cashout,
                        "status": "playing",
                        "win": 0,
                        "user_name": user_name,
                        "avatar": avatar
                    }
                    await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv})
                    await broadcast({"type": "bets_update", "bets": game.active_bets})
                    
            elif action == "cashout":
                if game.state == "running" and user_id in game.active_bets:
                    bet = game.active_bets[user_id]
                    if bet["status"] == "playing":
                        win_amount = int(bet["amount"] * game.multiplier)
                        bal, inv = get_or_create_user(user_id)
                        new_bal = bal + win_amount
                        update_user_data(user_id, balance=new_bal)
                        
                        bet["status"] = "cashed_out"
                        bet["win"] = win_amount
                        
                        await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv})
                        await websocket.send_json({"type": "notify", "msg": f"Успешно забрал {win_amount} ⭐️"})
                        await broadcast({"type": "bets_update", "bets": game.active_bets})

            # --- МИНЫ ---
            elif action == "mines_start":
                bet = data.get("bet", 0)
                mines_count = data.get("mines", 3)
                bal, inv = get_or_create_user(user_id)
                
                if bet > 0 and bal >= bet and 1 <= mines_count <= 24:
                    new_bal = bal - bet
                    update_user_data(user_id, balance=new_bal)
                    
                    grid = ["gem"] * 25
                    mine_indices = random.sample(range(25), mines_count)
                    for idx in mine_indices:
                        grid[idx] = "mine"
                        
                    game.mines_games[user_id] = {
                        "bet": bet,
                        "mines_count": mines_count,
                        "grid": grid,
                        "opened": [],
                        "status": "playing"
                    }
                    
                    await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv})
                    await websocket.send_json({
                        "type": "mines_state", 
                        "status": "playing", 
                        "opened": [], 
                        "grid": [],
                        "mult": 1.0, 
                        "win": 0
                    })

            elif action == "mines_open":
                cell_idx = data.get("cell")
                m_game = game.mines_games.get(user_id)
                
                if m_game and m_game["status"] == "playing" and cell_idx not in m_game["opened"]:
                    if m_game["grid"][cell_idx] == "mine":
                        m_game["status"] = "crashed"
                        await websocket.send_json({
                            "type": "mines_state", 
                            "status": "crashed", 
                            "grid": m_game["grid"], 
                            "opened": m_game["opened"] + [cell_idx],
                            "mult": 0,
                            "win": 0
                        })
                    else:
                        m_game["opened"].append(cell_idx)
                        mult = calculate_mines_mult(m_game["mines_count"], len(m_game["opened"]))
                        win = int(m_game["bet"] * mult)
                        
                        await websocket.send_json({
                            "type": "mines_state", 
                            "status": "playing", 
                            "opened": m_game["opened"], 
                            "grid": [],
                            "mult": mult, 
                            "win": win
                        })

            elif action == "mines_cashout":
                m_game = game.mines_games.get(user_id)
                if m_game and m_game["status"] == "playing" and len(m_game["opened"]) > 0:
                    mult = calculate_mines_mult(m_game["mines_count"], len(m_game["opened"]))
                    win_amount = int(m_game["bet"] * mult)
                    
                    bal, inv = get_or_create_user(user_id)
                    new_bal = bal + win_amount
                    update_user_data(user_id, balance=new_bal)
                    
                    m_game["status"] = "cashed_out"
                    
                    await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv})
                    await websocket.send_json({
                        "type": "mines_state", 
                        "status": "cashed_out", 
                        "grid": m_game["grid"], 
                        "opened": m_game["opened"],
                        "mult": mult,
                        "win": win_amount
                    })
                    await websocket.send_json({"type": "notify", "msg": f"💣 МИНЫ: Забрал +{win_amount} ⭐️ ({mult}x)!"})

            # --- ПОПОЛНЕНИЕ НА 1 МИЛЛИАРД ---
            elif action == "topup":
                bal, inv = get_or_create_user(user_id)
                new_bal = bal + 1000000000  # Добавляем 1 000 000 000
                update_user_data(user_id, balance=new_bal)
                await websocket.send_json({"type": "userData", "balance": new_bal, "inventory": inv})
                await websocket.send_json({"type": "notify", "msg": "🤑 Начислено +1 000 000 000 ⭐️!"})

            elif action == "save_inventory":
                new_inv = data.get("inventory", [])
                bal, _ = get_or_create_user(user_id)
                update_user_data(user_id, inventory=new_inv)
                await websocket.send_json({"type": "userData", "balance": bal, "inventory": new_inv})
                    
    except WebSocketDisconnect:
        if user_id in game.connections:
            del game.connections[user_id]