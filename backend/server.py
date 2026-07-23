import asyncio
import random
import sqlite3
import math
import time
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

conn = sqlite3.connect("casino.db", check_same_thread=False)
cursor = conn.cursor()
cursor.execute("""
    CREATE TABLE IF NOT EXISTS users (
        user_id INTEGER PRIMARY KEY,
        balance INTEGER DEFAULT 5000
    )
""")
conn.commit()

def get_or_create_user(user_id: int):
    cursor.execute("SELECT balance FROM users WHERE user_id = ?", (user_id,))
    row = cursor.fetchone()
    if row:
        return row[0]
    cursor.execute("INSERT INTO users (user_id, balance) VALUES (?, ?)", (user_id, 5000))
    conn.commit()
    return 5000

def update_balance(user_id: int, amount: int):
    cursor.execute("UPDATE users SET balance = balance + ? WHERE user_id = ?", (amount, user_id))
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

game = GameState()

async def broadcast(message: dict):
    for ws in list(game.connections.values()):
        try:
            await ws.send_json(message)
        except:
            pass

async def game_loop():
    while True:
        # 1. Ожидание ставок
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

        # 2. Генерация краша
        rand = random.random()
        game.target_crash = 1.0
        if rand > 0.05:
            game.target_crash = round(1 + math.pow(random.random(), 3) * 30, 2)

        # 3. Полет ракеты
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
                # Автовывод
                for uid, bet in list(game.active_bets.items()):
                    if bet["status"] == "playing" and bet["auto_cashout"] and current_mult >= bet["auto_cashout"]:
                        win_amount = int(bet["amount"] * bet["auto_cashout"])
                        update_balance(uid, win_amount)
                        bet["status"] = "cashed_out"
                        bet["win"] = win_amount
                        
                        if uid in game.connections:
                            bal = get_or_create_user(uid)
                            await game.connections[uid].send_json({"type": "balance_update", "balance": bal})
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
    
    balance = get_or_create_user(user_id)
    await websocket.send_json({"type": "balance_update", "balance": balance})
    
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            
            if action == "bet":
                amount = data.get("amount", 0)
                auto_cashout = data.get("auto_cashout", None)
                # Получаем ник и аву от клиента
                user_name = data.get("user_name", f"Игрок #{str(user_id)[-4:]}")
                avatar = data.get("avatar", "👤")
                
                if game.state == "idle" and amount > 0 and balance >= amount:
                    update_balance(user_id, -amount)
                    balance -= amount
                    game.active_bets[user_id] = {
                        "amount": amount,
                        "auto_cashout": auto_cashout,
                        "status": "playing",
                        "win": 0,
                        "user_name": user_name,
                        "avatar": avatar
                    }
                    await websocket.send_json({"type": "balance_update", "balance": balance})
                    await broadcast({"type": "bets_update", "bets": game.active_bets})
                    
            elif action == "cashout":
                if game.state == "running" and user_id in game.active_bets:
                    bet = game.active_bets[user_id]
                    if bet["status"] == "playing":
                        win_amount = int(bet["amount"] * game.multiplier)
                        update_balance(user_id, win_amount)
                        balance += win_amount
                        bet["status"] = "cashed_out"
                        bet["win"] = win_amount
                        
                        await websocket.send_json({"type": "balance_update", "balance": balance})
                        await websocket.send_json({"type": "notify", "msg": f"Успешно забрал {win_amount} ⭐️"})
                        await broadcast({"type": "bets_update", "bets": game.active_bets})
                        
            elif action == "topup":
                update_balance(user_id, 10000)
                balance += 10000
                await websocket.send_json({"type": "balance_update", "balance": balance})
                await websocket.send_json({"type": "notify", "msg": "🤑 Начислено +10 000 ⭐️!"})
                    
    except WebSocketDisconnect:
        if user_id in game.connections:
            del game.connections[user_id]