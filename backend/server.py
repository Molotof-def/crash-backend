import asyncio
import random
import sqlite3
import math
import time
import json
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware

# Настройка базы данных SQLite
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

# Настройка FastAPI
app = FastAPI()
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], # Разрешаем подключения откуда угодно
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Глобальное состояние игры
class GameState:
    def __init__(self):
        self.state = "idle" # idle, running, crashed
        self.multiplier = 1.0
        self.target_crash = 1.0
        self.active_bets = {} # user_id: bet_amount
        self.connections = [] # список всех активных вебсокетов

game = GameState()

async def broadcast(message: dict):
    """Отправка сообщения всем подключенным игрокам"""
    for connection in game.connections:
        try:
            await connection.send_json(message)
        except:
            pass

async def game_loop():
    """Основной цикл игры, который работает бесконечно на фоне"""
    while True:
        # 1. Ждем ставки (5 секунд)
        game.state = "idle"
        game.multiplier = 1.0
        game.active_bets = {}
        await broadcast({"type": "state_update", "state": game.state, "multiplier": game.multiplier})
        await asyncio.sleep(5)

        # 2. Генерация точки краша
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
            else:
                game.multiplier = current_mult
                
            await broadcast({"type": "state_update", "state": game.state, "multiplier": game.multiplier})
            
            if game.state == "crashed":
                break
            
            await asyncio.sleep(0.1) # Обновляем 10 раз в секунду

        # 4. Пауза после краша (3 секунды)
        await asyncio.sleep(3)

# Запускаем игровой цикл при старте сервера
@app.on_event("startup")
async def startup_event():
    asyncio.create_task(game_loop())

@app.websocket("/ws/{user_id}")
async def websocket_endpoint(websocket: WebSocket, user_id: int):
    await websocket.accept()
    game.connections.append(websocket)
    
    # Отправляем юзеру его баланс при входе
    balance = get_or_create_user(user_id)
    await websocket.send_json({"type": "balance_update", "balance": balance})
    
    try:
        while True:
            data = await websocket.receive_json()
            action = data.get("action")
            
            if action == "bet":
                amount = data.get("amount", 0)
                if game.state == "idle" and amount > 0 and balance >= amount:
                    update_balance(user_id, -amount)
                    balance -= amount
                    game.active_bets[user_id] = amount
                    await websocket.send_json({"type": "balance_update", "balance": balance})
                    
            elif action == "cashout":
                if game.state == "running" and user_id in game.active_bets:
                    win_amount = int(game.active_bets[user_id] * game.multiplier)
                    update_balance(user_id, win_amount)
                    balance += win_amount
                    del game.active_bets[user_id] # Удаляем ставку, чтобы не забрал дважды
                    await websocket.send_json({"type": "balance_update", "balance": balance})
                    await websocket.send_json({"type": "notify", "msg": f"Успешно забрал {win_amount} ⭐️"})
                    
    except WebSocketDisconnect:
        game.connections.remove(websocket)