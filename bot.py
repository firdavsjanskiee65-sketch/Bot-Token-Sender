import asyncio
import json
import logging
import os
import random
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)
from telegram.error import BadRequest, NetworkError, RetryAfter, TelegramError


BOT_TOKEN = os.getenv("BOT_TOKEN") or os.getenv("TELEGRAM_BOT_TOKEN")
ADMIN_ID_RAW = os.getenv("ADMIN_ID", "")
DATABASE_PATH = os.getenv("GAVA_DATABASE", "gava.sqlite3")
MAX_GAVA = 9_000_000_000_000_000_000
DAILY_BONUS = 100_000
MINES_BOMBS = 6
MINES_SIZE = 6
MINES_MULTIPLIERS = [
    (0, 1.00),
    (1, 1.10),
    (2, 1.25),
    (3, 1.45),
    (4, 1.70),
    (5, 2.00),
    (6, 2.40),
    (7, 2.85),
    (8, 3.35),
    (9, 3.90),
    (10, 4.50),
    (11, 5.15),
    (12, 5.85),
    (13, 6.60),
    (14, 7.40),
    (15, 8.25),
    (16, 9.15),
    (17, 10.10),
    (18, 11.10),
    (19, 12.15),
    (20, 13.25),
    (21, 14.40),
    (22, 15.60),
    (23, 16.85),
    (24, 18.15),
    (25, 19.50),
    (26, 20.90),
    (27, 22.35),
    (28, 23.85),
    (29, 25.40),
    (30, 27.00),
]

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
)
LOGGER = logging.getLogger("gava-bot")
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)

try:
    ADMIN_ID = int(ADMIN_ID_RAW)
except ValueError:
    ADMIN_ID = 0


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().isoformat()


def parse_iso(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def fmt_gava(amount: int) -> str:
    return f"{amount:,} GAVA"


def parse_amount(text: str) -> Optional[int]:
    normalized = text.replace(",", "").replace(" ", "").replace("_", "")
    if not normalized.isdigit():
        return None
    amount = int(normalized)
    if amount <= 0 or amount > MAX_GAVA:
        return None
    return amount


def display_name(user: Any) -> str:
    username = getattr(user, "username", None)
    if username:
        return f"@{username}"
    name = " ".join(
        part for part in [getattr(user, "first_name", ""), getattr(user, "last_name", "")]
        if part
    ).strip()
    return name or f"ID {getattr(user, 'id', '')}"


def user_label(row: sqlite3.Row) -> str:
    if row["username"]:
        return f"@{row['username']}"
    return row["first_name"] or f"ID {row['telegram_id']}"


class Database:
    def __init__(self, path: str):
        self.connection = sqlite3.connect(path, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self.connection.execute("PRAGMA busy_timeout=10000")
        self.lock = threading.RLock()
        self.setup()

    def setup(self) -> None:
        with self.lock, self.connection:
            self.connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    telegram_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT NOT NULL DEFAULT '',
                    balance INTEGER NOT NULL DEFAULT 0 CHECK(balance >= 0),
                    created_at TEXT NOT NULL,
                    last_bonus_at TEXT
                );
                CREATE TABLE IF NOT EXISTS transactions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    telegram_id INTEGER NOT NULL,
                    type TEXT NOT NULL,
                    amount INTEGER NOT NULL,
                    description TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
                );
                CREATE TABLE IF NOT EXISTS games (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    wager INTEGER NOT NULL,
                    payout INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    finished_at TEXT,
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
                );
                CREATE INDEX IF NOT EXISTS idx_games_user_status
                    ON games(telegram_id, status);
                CREATE TABLE IF NOT EXISTS duels (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    challenger_id INTEGER NOT NULL,
                    challenged_id INTEGER NOT NULL,
                    chat_id INTEGER NOT NULL,
                    message_id INTEGER,
                    wager INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    turn_user_id INTEGER,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    finished_at TEXT
                );
                CREATE TABLE IF NOT EXISTS promo_codes (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    code TEXT NOT NULL UNIQUE COLLATE NOCASE,
                    reward INTEGER NOT NULL,
                    usage_limit INTEGER NOT NULL,
                    uses INTEGER NOT NULL DEFAULT 0,
                    expires_at TEXT,
                    active INTEGER NOT NULL DEFAULT 1,
                    creator_id INTEGER NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS promo_redemptions (
                    promo_id INTEGER NOT NULL,
                    telegram_id INTEGER NOT NULL,
                    redeemed_at TEXT NOT NULL,
                    PRIMARY KEY(promo_id, telegram_id),
                    FOREIGN KEY(promo_id) REFERENCES promo_codes(id),
                    FOREIGN KEY(telegram_id) REFERENCES users(telegram_id)
                );
                CREATE TABLE IF NOT EXISTS processed_updates (
                    update_id INTEGER PRIMARY KEY,
                    processed_at TEXT NOT NULL
                );
                """
            )

    def ensure_user(self, user: Any) -> sqlite3.Row:
        now = iso_now()
        with self.lock, self.connection:
            self.connection.execute(
                """
                INSERT INTO users(telegram_id, username, first_name, created_at)
                VALUES(?, ?, ?, ?)
                ON CONFLICT(telegram_id) DO UPDATE SET
                    username=excluded.username,
                    first_name=excluded.first_name
                """,
                (user.id, user.username, user.first_name or "", now),
            )
            return self.connection.execute(
                "SELECT * FROM users WHERE telegram_id=?", (user.id,)
            ).fetchone()

    def user(self, telegram_id: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM users WHERE telegram_id=?", (telegram_id,)
            ).fetchone()

    def credit(
        self, telegram_id: int, amount: int, tx_type: str, description: str
    ) -> bool:
        if amount <= 0 or amount > MAX_GAVA:
            return False
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    "SELECT balance FROM users WHERE telegram_id=?", (telegram_id,)
                ).fetchone()
                if not row or row["balance"] + amount > MAX_GAVA:
                    self.connection.rollback()
                    return False
                self.connection.execute(
                    "UPDATE users SET balance=balance+? WHERE telegram_id=?",
                    (amount, telegram_id),
                )
                self.connection.execute(
                    """
                    INSERT INTO transactions(telegram_id, type, amount, description, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (telegram_id, tx_type, amount, description, iso_now()),
                )
                self.connection.commit()
                return True
            except sqlite3.Error:
                self.connection.rollback()
                LOGGER.exception("Ошибка начисления средств")
                return False

    def debit(
        self, telegram_id: int, amount: int, tx_type: str, description: str
    ) -> bool:
        if amount <= 0 or amount > MAX_GAVA:
            return False
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    "SELECT balance FROM users WHERE telegram_id=?", (telegram_id,)
                ).fetchone()
                if not row or row["balance"] < amount:
                    self.connection.rollback()
                    return False
                self.connection.execute(
                    "UPDATE users SET balance=balance-? WHERE telegram_id=?",
                    (amount, telegram_id),
                )
                self.connection.execute(
                    """
                    INSERT INTO transactions(telegram_id, type, amount, description, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    (telegram_id, tx_type, -amount, description, iso_now()),
                )
                self.connection.commit()
                return True
            except sqlite3.Error:
                self.connection.rollback()
                LOGGER.exception("Ошибка списания средств")
                return False

    def transfer(self, sender_id: int, receiver_id: int, amount: int) -> bool:
        if amount <= 0 or amount > MAX_GAVA or sender_id == receiver_id:
            return False
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                sender = self.connection.execute(
                    "SELECT balance FROM users WHERE telegram_id=?", (sender_id,)
                ).fetchone()
                receiver = self.connection.execute(
                    "SELECT balance FROM users WHERE telegram_id=?", (receiver_id,)
                ).fetchone()
                if not sender or not receiver:
                    self.connection.rollback()
                    return False
                if sender["balance"] < amount or receiver["balance"] + amount > MAX_GAVA:
                    self.connection.rollback()
                    return False
                now = iso_now()
                self.connection.execute(
                    "UPDATE users SET balance=balance-? WHERE telegram_id=?",
                    (amount, sender_id),
                )
                self.connection.execute(
                    "UPDATE users SET balance=balance+? WHERE telegram_id=?",
                    (amount, receiver_id),
                )
                self.connection.executemany(
                    """
                    INSERT INTO transactions(telegram_id, type, amount, description, created_at)
                    VALUES(?, ?, ?, ?, ?)
                    """,
                    [
                        (sender_id, "transfer_out", -amount, f"Перевод пользователю {receiver_id}", now),
                        (receiver_id, "transfer_in", amount, f"Перевод от пользователя {sender_id}", now),
                    ],
                )
                self.connection.commit()
                return True
            except sqlite3.Error:
                self.connection.rollback()
                LOGGER.exception("Ошибка перевода")
                return False

    def claim_bonus(self, telegram_id: int) -> tuple[bool, Optional[timedelta]]:
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    "SELECT last_bonus_at FROM users WHERE telegram_id=?", (telegram_id,)
                ).fetchone()
                if not row:
                    self.connection.rollback()
                    return False, None
                previous = parse_iso(row["last_bonus_at"])
                now = utc_now()
                if previous and now - previous < timedelta(hours=24):
                    self.connection.rollback()
                    return False, timedelta(hours=24) - (now - previous)
                if not self.credit_in_transaction(
                    telegram_id, DAILY_BONUS, "daily_bonus", "Ежедневный бонус"
                ):
                    self.connection.rollback()
                    return False, None
                self.connection.execute(
                    "UPDATE users SET last_bonus_at=? WHERE telegram_id=?",
                    (now.isoformat(), telegram_id),
                )
                self.connection.commit()
                return True, None
            except sqlite3.Error:
                self.connection.rollback()
                LOGGER.exception("Ошибка ежедневного бонуса")
                return False, None

    def credit_in_transaction(
        self, telegram_id: int, amount: int, tx_type: str, description: str
    ) -> bool:
        row = self.connection.execute(
            "SELECT balance FROM users WHERE telegram_id=?", (telegram_id,)
        ).fetchone()
        if not row or amount <= 0 or row["balance"] + amount > MAX_GAVA:
            return False
        self.connection.execute(
            "UPDATE users SET balance=balance+? WHERE telegram_id=?",
            (amount, telegram_id),
        )
        self.connection.execute(
            """
            INSERT INTO transactions(telegram_id, type, amount, description, created_at)
            VALUES(?, ?, ?, ?, ?)
            """,
            (telegram_id, tx_type, amount, description, iso_now()),
        )
        return True

    def create_game(
        self,
        kind: str,
        telegram_id: int,
        chat_id: int,
        wager: int,
        data: dict[str, Any],
        message_id: Optional[int] = None,
    ) -> Optional[int]:
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                row = self.connection.execute(
                    "SELECT balance FROM users WHERE telegram_id=?", (telegram_id,)
                ).fetchone()
                if not row or row["balance"] < wager:
                    self.connection.rollback()
                    return None
                now = iso_now()
                self.connection.execute(
                    "UPDATE users SET balance=balance-? WHERE telegram_id=?",
                    (wager, telegram_id),
                )
                self.connection.execute(
                    """
                    INSERT INTO transactions(telegram_id, type, amount, description, created_at)
                    VALUES(?, 'game_wager', ?, ?, ?)
                    """,
                    (telegram_id, -wager, f"Ставка в игре {kind}", now),
                )
                cursor = self.connection.execute(
                    """
                    INSERT INTO games(kind, telegram_id, chat_id, message_id, wager, status, data_json, created_at)
                    VALUES(?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (kind, telegram_id, chat_id, message_id, wager, json.dumps(data), now),
                )
                game_id = int(cursor.lastrowid)
                self.connection.commit()
                return game_id
            except sqlite3.Error:
                self.connection.rollback()
                LOGGER.exception("Ошибка создания игры")
                return None

    def update_game_message(self, game_id: int, message_id: int) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE games SET message_id=? WHERE id=?", (message_id, game_id)
            )

    def game(self, game_id: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM games WHERE id=?", (game_id,)
            ).fetchone()

    def update_game_data(self, game_id: int, data: dict[str, Any]) -> None:
        with self.lock, self.connection:
            self.connection.execute(
                "UPDATE games SET data_json=? WHERE id=? AND status='active'",
                (json.dumps(data), game_id),
            )

    def finish_game(self, game_id: int, payout: int, description: str) -> bool:
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                game = self.connection.execute(
                    "SELECT * FROM games WHERE id=?", (game_id,)
                ).fetchone()
                if not game or game["status"] != "active":
                    self.connection.rollback()
                    return False
                if payout < 0 or payout > MAX_GAVA:
                    self.connection.rollback()
                    return False
                if payout and not self.credit_in_transaction(
                    game["telegram_id"], payout, "game_payout", description
                ):
                    self.connection.rollback()
                    return False
                self.connection.execute(
                    "UPDATE games SET status='finished', payout=?, finished_at=? WHERE id=?",
                    (payout, iso_now(), game_id),
                )
                self.connection.commit()
                return True
            except sqlite3.Error:
                self.connection.rollback()
                LOGGER.exception("Ошибка завершения игры")
                return False

    def create_duel(
        self,
        challenger_id: int,
        challenged_id: int,
        chat_id: int,
        wager: int,
        message_id: Optional[int],
    ) -> Optional[int]:
        with self.lock, self.connection:
            try:
                cursor = self.connection.execute(
                    """
                    INSERT INTO duels(
                        challenger_id, challenged_id, chat_id, message_id, wager,
                        status, data_json, created_at
                    ) VALUES(?, ?, ?, ?, ?, 'pending', '{}', ?)
                    """,
                    (challenger_id, challenged_id, chat_id, message_id, wager, iso_now()),
                )
                return int(cursor.lastrowid)
            except sqlite3.Error:
                LOGGER.exception("Ошибка создания дуэли")
                return None

    def duel(self, duel_id: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM duels WHERE id=?", (duel_id,)
            ).fetchone()

    def accept_duel(self, duel_id: int, user_id: int) -> tuple[str, Optional[sqlite3.Row]]:
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                duel = self.connection.execute(
                    "SELECT * FROM duels WHERE id=?", (duel_id,)
                ).fetchone()
                if not duel:
                    self.connection.rollback()
                    return "missing", None
                if duel["status"] != "pending":
                    self.connection.rollback()
                    return "closed", duel
                if duel["challenged_id"] != user_id:
                    self.connection.rollback()
                    return "forbidden", duel
                challenger = self.connection.execute(
                    "SELECT balance FROM users WHERE telegram_id=?",
                    (duel["challenger_id"],),
                ).fetchone()
                challenged = self.connection.execute(
                    "SELECT balance FROM users WHERE telegram_id=?",
                    (duel["challenged_id"],),
                ).fetchone()
                if (
                    not challenger
                    or not challenged
                    or challenger["balance"] < duel["wager"]
                    or challenged["balance"] < duel["wager"]
                ):
                    self.connection.rollback()
                    return "funds", duel
                now = iso_now()
                for player_id in [duel["challenger_id"], duel["challenged_id"]]:
                    self.connection.execute(
                        "UPDATE users SET balance=balance-? WHERE telegram_id=?",
                        (duel["wager"], player_id),
                    )
                    self.connection.execute(
                        """
                        INSERT INTO transactions(telegram_id, type, amount, description, created_at)
                        VALUES(?, 'duel_wager', ?, ?, ?)
                        """,
                        (player_id, -duel["wager"], f"Ставка в дуэли #{duel_id}", now),
                    )
                self.connection.execute(
                    """
                    UPDATE duels SET status='active', turn_user_id=?, data_json='{}'
                    WHERE id=?
                    """,
                    (duel["challenger_id"], duel_id),
                )
                self.connection.commit()
                return "accepted", self.connection.execute(
                    "SELECT * FROM duels WHERE id=?", (duel_id,)
                ).fetchone()
            except sqlite3.Error:
                self.connection.rollback()
                LOGGER.exception("Ошибка принятия дуэли")
                return "error", None

    def reject_duel(self, duel_id: int, user_id: int) -> str:
        with self.lock, self.connection:
            duel = self.connection.execute(
                "SELECT * FROM duels WHERE id=?", (duel_id,)
            ).fetchone()
            if not duel:
                return "missing"
            if duel["status"] != "pending":
                return "closed"
            if duel["challenged_id"] != user_id:
                return "forbidden"
            self.connection.execute(
                "UPDATE duels SET status='rejected', finished_at=? WHERE id=?",
                (iso_now(), duel_id),
            )
            return "rejected"

    def shoot_duel(self, duel_id: int, user_id: int) -> tuple[str, Optional[sqlite3.Row]]:
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                duel = self.connection.execute(
                    "SELECT * FROM duels WHERE id=?", (duel_id,)
                ).fetchone()
                if not duel:
                    self.connection.rollback()
                    return "missing", None
                if duel["status"] != "active":
                    self.connection.rollback()
                    return "closed", duel
                if duel["turn_user_id"] != user_id:
                    self.connection.rollback()
                    return "turn", duel
                hit = random.random() < 0.35
                if hit:
                    winner = user_id
                    payout = duel["wager"] * 2
                    if not self.credit_in_transaction(
                        winner, payout, "duel_payout", f"Победа в дуэли #{duel_id}"
                    ):
                        self.connection.rollback()
                        return "error", duel
                    self.connection.execute(
                        """
                        UPDATE duels SET status='finished', data_json=?, finished_at=?
                        WHERE id=?
                        """,
                        (json.dumps({"winner": winner, "hit_by": user_id}), iso_now(), duel_id),
                    )
                    self.connection.commit()
                    return "hit", self.connection.execute(
                        "SELECT * FROM duels WHERE id=?", (duel_id,)
                    ).fetchone()
                next_player = (
                    duel["challenged_id"]
                    if user_id == duel["challenger_id"]
                    else duel["challenger_id"]
                )
                self.connection.execute(
                    "UPDATE duels SET turn_user_id=? WHERE id=?",
                    (next_player, duel_id),
                )
                self.connection.commit()
                return "miss", self.connection.execute(
                    "SELECT * FROM duels WHERE id=?", (duel_id,)
                ).fetchone()
            except sqlite3.Error:
                self.connection.rollback()
                LOGGER.exception("Ошибка выстрела в дуэли")
                return "error", None

    def redeem_promo(self, code: str, telegram_id: int) -> tuple[str, int]:
        with self.lock:
            try:
                self.connection.execute("BEGIN IMMEDIATE")
                promo = self.connection.execute(
                    "SELECT * FROM promo_codes WHERE lower(code)=lower(?)", (code,)
                ).fetchone()
                if not promo or not promo["active"]:
                    self.connection.rollback()
                    return "missing", 0
                if promo["uses"] >= promo["usage_limit"]:
                    self.connection.rollback()
                    return "limit", 0
                expires = parse_iso(promo["expires_at"])
                if expires and utc_now() >= expires:
                    self.connection.rollback()
                    return "expired", 0
                try:
                    self.connection.execute(
                        """
                        INSERT INTO promo_redemptions(promo_id, telegram_id, redeemed_at)
                        VALUES(?, ?, ?)
                        """,
                        (promo["id"], telegram_id, iso_now()),
                    )
                except sqlite3.IntegrityError:
                    self.connection.rollback()
                    return "used", 0
                self.connection.execute(
                    "UPDATE promo_codes SET uses=uses+1 WHERE id=? AND uses<usage_limit",
                    (promo["id"],),
                )
                if not self.credit_in_transaction(
                    telegram_id, promo["reward"], "promo", f"Промокод #{promo['code']}"
                ):
                    self.connection.rollback()
                    return "error", 0
                self.connection.commit()
                return "ok", promo["reward"]
            except sqlite3.Error:
                self.connection.rollback()
                LOGGER.exception("Ошибка активации промокода")
                return "error", 0

    def create_promo(
        self,
        code: str,
        reward: int,
        usage_limit: int,
        expires_at: Optional[str],
        creator_id: int,
    ) -> bool:
        with self.lock, self.connection:
            try:
                self.connection.execute(
                    """
                    INSERT INTO promo_codes(code, reward, usage_limit, expires_at, creator_id, created_at)
                    VALUES(?, ?, ?, ?, ?, ?)
                    """,
                    (code, reward, usage_limit, expires_at, creator_id, iso_now()),
                )
                return True
            except sqlite3.IntegrityError:
                return False

    def promo(self, promo_id: int) -> Optional[sqlite3.Row]:
        with self.lock:
            return self.connection.execute(
                "SELECT * FROM promo_codes WHERE id=?", (promo_id,)
            ).fetchone()

    def toggle_promo(self, promo_id: int) -> bool:
        with self.lock, self.connection:
            cursor = self.connection.execute(
                "UPDATE promo_codes SET active=CASE active WHEN 1 THEN 0 ELSE 1 END WHERE id=?",
                (promo_id,),
            )
            return cursor.rowcount > 0

    def top_users(self, limit: int = 10) -> list[sqlite3.Row]:
        with self.lock:
            return list(
                self.connection.execute(
                    "SELECT * FROM users ORDER BY balance DESC, telegram_id ASC LIMIT ?",
                    (limit,),
                ).fetchall()
            )

    def all_promos(self) -> list[sqlite3.Row]:
        with self.lock:
            return list(
                self.connection.execute(
                    "SELECT * FROM promo_codes ORDER BY created_at DESC LIMIT 30"
                ).fetchall()
            )

    def stats(self) -> dict[str, int]:
        with self.lock:
            queries = {
                "users": "SELECT COUNT(*) AS value FROM users",
                "circulation": "SELECT COALESCE(SUM(balance), 0) AS value FROM users",
                "active_games": "SELECT COUNT(*) AS value FROM games WHERE status='active'",
                "active_duels": "SELECT COUNT(*) AS value FROM duels WHERE status='active'",
                "promos": "SELECT COUNT(*) AS value FROM promo_codes WHERE active=1",
            }
            return {
                key: int(self.connection.execute(query).fetchone()["value"])
                for key, query in queries.items()
            }


DB = Database(DATABASE_PATH)
ADMIN_STATES: dict[int, dict[str, Any]] = {}


def ensure_user(update: Update) -> Optional[sqlite3.Row]:
    if not update.effective_user:
        return None
    return DB.ensure_user(update.effective_user)


def start_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("🎁 Получить бонус", callback_data="bonus")],
            [
                InlineKeyboardButton("💰 Баланс", callback_data="balance"),
                InlineKeyboardButton("🏆 Топ-10", callback_data="top"),
            ],
        ]
    )


def top_text() -> str:
    rows = DB.top_users()
    lines = ["🏆 Топ-10 игроков по GAVA", ""]
    for index, row in enumerate(rows, start=1):
        lines.append(f"{index}. {user_label(row)} — {fmt_gava(row['balance'])}")
    if not rows:
        lines.append("Пока игроков нет.")
    return "\n".join(lines)


def mines_keyboard(game_id: int, revealed: list[int], finished: bool = False) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    for row in range(MINES_SIZE):
        buttons = []
        for column in range(MINES_SIZE):
            cell = row * MINES_SIZE + column
            label = "✅" if cell in revealed else "⬜"
            buttons.append(
                InlineKeyboardButton(
                    label,
                    callback_data=f"mines:{game_id}:{cell}" if not finished and cell not in revealed else "noop",
                )
            )
        rows.append(buttons)
    if not finished:
        rows.append([InlineKeyboardButton("💰 Забрать выигрыш", callback_data=f"mines_cash:{game_id}")])
    return InlineKeyboardMarkup(rows)


def mines_text(game: sqlite3.Row, data: dict[str, Any], extra: str = "") -> str:
    revealed = len(data.get("revealed", []))
    multiplier = data.get("multiplier", 1.0)
    text = (
        "💣 MINES\n"
        f"💰 Ставка: {fmt_gava(game['wager'])}\n"
        f"✅ Открыто безопасных клеток: {revealed}\n"
        f"📈 Множитель: {multiplier:.2f}x\n"
        f"💵 Можно забрать: {fmt_gava(int(data.get('current_reward', 0)))}\n"
    )
    if extra:
        text += f"\n{extra}"
    return text


def reveal_mines(data: dict[str, Any]) -> str:
    bombs = set(data.get("bombs", []))
    revealed = set(data.get("revealed", []))
    chars = []
    for cell in range(MINES_SIZE * MINES_SIZE):
        if cell in bombs:
            chars.append("💣")
        elif cell in revealed:
            chars.append("✅")
        else:
            chars.append("⬜")
    return "\n".join("".join(chars[index:index + MINES_SIZE]) for index in range(0, 36, 6))


def admin_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Создать промокод", callback_data="admin:create")],
            [
                InlineKeyboardButton("📋 Промокоды", callback_data="admin:list"),
                InlineKeyboardButton("📊 Статистика", callback_data="admin:stats"),
            ],
        ]
    )


async def safe_edit(query: Any, text: str, markup: Optional[InlineKeyboardMarkup] = None) -> None:
    try:
        await query.edit_message_text(text=text, reply_markup=markup)
    except BadRequest as error:
        if "Message is not modified" not in str(error):
            LOGGER.warning("Не удалось изменить сообщение: %s", error)
    except TelegramError:
        LOGGER.exception("Ошибка изменения сообщения")


async def safe_answer(query: Any, text: str, show_alert: bool = False) -> None:
    try:
        await query.answer(text=text, show_alert=show_alert)
    except TelegramError:
        LOGGER.debug("Не удалось ответить на callback", exc_info=True)


async def send_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    row = ensure_user(update)
    if not row or not update.effective_message:
        return
    await update.effective_message.reply_text(
        "🎮 Добро пожаловать в GAVA!\n"
        f"💰 Твой баланс: {fmt_gava(row['balance'])}\n\n"
        "Игры запускаются текстом: мина, кубик, монета, дартс или слот + сумма.\n"
        "Для перевода ответь на сообщение пользователя: п 100000",
        reply_markup=start_keyboard(),
    )


async def show_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    ensure_user(update)
    if not update.effective_message:
        return
    if update.effective_user and update.effective_user.id == ADMIN_ID:
        await update.effective_message.reply_text("🔐 Панель администратора", reply_markup=admin_keyboard())
    else:
        await update.effective_message.reply_text("❌ Доступ запрещён.")


async def handle_bonus(query: Any) -> None:
    ok, remaining = DB.claim_bonus(query.from_user.id)
    if ok:
        row = DB.user(query.from_user.id)
        await safe_answer(query, "Бонус начислен!")
        await safe_edit(
            query,
            f"🎁 Ежедневный бонус: +{fmt_gava(DAILY_BONUS)}\n"
            f"💰 Твой баланс: {fmt_gava(row['balance'] if row else DAILY_BONUS)}",
            start_keyboard(),
        )
    else:
        if remaining:
            total_seconds = max(0, int(remaining.total_seconds()))
            hours, remainder = divmod(total_seconds, 3600)
            minutes = remainder // 60
            await safe_answer(query, f"Следующий бонус через {hours} ч. {minutes} мин.", True)
        else:
            await safe_answer(query, "Не удалось получить бонус. Попробуй позже.", True)


async def handle_mines_callback(query: Any, game_id: int, cell: int) -> None:
    game = DB.game(game_id)
    if not game or game["kind"] != "mines":
        await safe_answer(query, "Игра не найдена.", True)
        return
    if game["telegram_id"] != query.from_user.id:
        await safe_answer(query, "Это не твоя игра.", True)
        return
    if game["status"] != "active":
        await safe_answer(query, "Эта игра уже завершена.", True)
        return
    data = json.loads(game["data_json"])
    revealed = set(data.get("revealed", []))
    bombs = set(data.get("bombs", []))
    if cell < 0 or cell >= 36 or cell in revealed:
        await safe_answer(query, "Эта клетка уже открыта.", True)
        return
    revealed.add(cell)
    data["revealed"] = sorted(revealed)
    if cell in bombs:
        DB.update_game_data(game_id, data)
        DB.finish_game(game_id, 0, "Проигрыш в MINES")
        await safe_answer(query, "Бомба! Ставка проиграна.", True)
        await safe_edit(
            query,
            mines_text(game, data, "💥 Бомба! Игра завершена.\n\n" + reveal_mines(data)),
            mines_keyboard(game_id, data["revealed"], True),
        )
        return
    safe_count = len(revealed)
    multiplier = dict(MINES_MULTIPLIERS).get(
        safe_count, MINES_MULTIPLIERS[-1][1] + (safe_count - MINES_MULTIPLIERS[-1][0]) * 0.9
    )
    data["multiplier"] = multiplier
    data["current_reward"] = min(MAX_GAVA, int(game["wager"] * multiplier))
    DB.update_game_data(game_id, data)
    await safe_answer(query, "Безопасно!")
    await safe_edit(query, mines_text(game, data), mines_keyboard(game_id, data["revealed"]))


async def handle_mines_cashout(query: Any, game_id: int) -> None:
    game = DB.game(game_id)
    if not game or game["kind"] != "mines":
        await safe_answer(query, "Игра не найдена.", True)
        return
    if game["telegram_id"] != query.from_user.id:
        await safe_answer(query, "Это не твоя игра.", True)
        return
    if game["status"] != "active":
        await safe_answer(query, "Эта игра уже завершена.", True)
        return
    data = json.loads(game["data_json"])
    payout = int(data.get("current_reward", 0))
    if not DB.finish_game(game_id, payout, "Забранный выигрыш MINES"):
        await safe_answer(query, "Игра уже обработана.", True)
        return
    await safe_answer(query, "Выигрыш начислен!")
    await safe_edit(
        query,
        mines_text(game, data, f"✅ Ты забрал выигрыш: {fmt_gava(payout)}"),
        mines_keyboard(game_id, data.get("revealed", []), True),
    )


async def handle_coin_callback(query: Any, game_id: int, choice: str) -> None:
    game = DB.game(game_id)
    if not game or game["kind"] != "coin":
        await safe_answer(query, "Игра не найдена.", True)
        return
    if game["telegram_id"] != query.from_user.id:
        await safe_answer(query, "Это не твоя игра.", True)
        return
    if game["status"] != "active":
        await safe_answer(query, "Эта игра уже завершена.", True)
        return
    result = random.choice(["heads", "tails"])
    result_text = "Орёл" if result == "heads" else "Решка"
    choice_text = "Орёл" if choice == "heads" else "Решка"
    win = result == choice
    payout = game["wager"] * 19 // 10 if win else 0
    if not DB.finish_game(game_id, payout, "Выигрыш в МОНЕТЕ" if win else "Проигрыш в МОНЕТЕ"):
        await safe_answer(query, "Игра уже обработана.", True)
        return
    await safe_edit(
        query,
        "🪙 МОНЕТА\n"
        f"Твой выбор: {choice_text}\n"
        f"Результат: {result_text}\n\n"
        + (f"✅ Победа! Начислено: {fmt_gava(payout)}" if win else "❌ Не угадал. Ставка проиграна."),
    )


async def handle_duel_callback(query: Any, duel_id: int, action: str) -> None:
    if action == "accept":
        status, duel = DB.accept_duel(duel_id, query.from_user.id)
        if status == "forbidden":
            await safe_answer(query, "Это приглашение не для тебя.", True)
            return
        if status == "closed":
            await safe_answer(query, "Приглашение уже закрыто.", True)
            return
        if status == "funds":
            await safe_answer(query, "У одного из игроков недостаточно GAVA.", True)
            return
        if status != "accepted" or not duel:
            await safe_answer(query, "Не удалось принять дуэль.", True)
            return
        await safe_answer(query, "Дуэль принята!")
        await safe_edit(
            query,
            "⚔️ ДУЭЛЬ НАЧАЛАСЬ!\n"
            f"💰 Банк: {fmt_gava(duel['wager'] * 2)}\n"
            "🔫 Сейчас стреляет вызывающий игрок.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔫 ВЫСТРЕЛИТЬ", callback_data=f"duel:shoot:{duel_id}")]]
            ),
        )
        return
    if action == "reject":
        status = DB.reject_duel(duel_id, query.from_user.id)
        if status == "forbidden":
            await safe_answer(query, "Это приглашение не для тебя.", True)
        elif status == "rejected":
            await safe_answer(query, "Дуэль отклонена.")
            await safe_edit(query, "❌ Дуэль отклонена.")
        else:
            await safe_answer(query, "Приглашение уже закрыто.", True)
        return
    status, duel = DB.shoot_duel(duel_id, query.from_user.id)
    if status == "turn":
        await safe_answer(query, "Сейчас очередь другого игрока.", True)
        return
    if status in {"closed", "missing"} or not duel:
        await safe_answer(query, "Дуэль уже завершена.", True)
        return
    if status == "hit":
        winner_id = json.loads(duel["data_json"]).get("winner")
        await safe_answer(query, "Попадание!")
        await safe_edit(
            query,
            "💥 ПОПАДАНИЕ!\n"
            f"🏆 Победитель: {'ты' if winner_id == query.from_user.id else 'соперник'}\n"
            f"💰 Выигрыш: {fmt_gava(duel['wager'] * 2)}",
        )
    else:
        next_name = (
            "вызывающего игрока"
            if duel["turn_user_id"] == duel["challenger_id"]
            else "принявшего вызов игрока"
        )
        await safe_answer(query, "Промах!")
        await safe_edit(
            query,
            f"🔫 Промах!\nТеперь стреляет {next_name}.",
            InlineKeyboardMarkup(
                [[InlineKeyboardButton("🔫 ВЫСТРЕЛИТЬ", callback_data=f"duel:shoot:{duel_id}")]]
            ),
        )


async def admin_callback(query: Any, action: str) -> None:
    if query.from_user.id != ADMIN_ID:
        await safe_answer(query, "Доступ запрещён.", True)
        return
    if action == "create":
        ADMIN_STATES[query.from_user.id] = {"step": "code"}
        await safe_answer(query, "Начинаем создание промокода.")
        await safe_edit(query, "Введите промокод, например #GAVA100:")
    elif action == "list":
        promos = DB.all_promos()
        if not promos:
            text = "📋 Промокодов пока нет."
            markup = admin_keyboard()
        else:
            lines = ["📋 Промокоды:"]
            buttons = []
            for promo in promos:
                state = "включён" if promo["active"] else "выключен"
                lines.append(
                    f"#{promo['code']} — {fmt_gava(promo['reward'])}, "
                    f"{promo['uses']}/{promo['usage_limit']}, {state}"
                )
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"{'Выключить' if promo['active'] else 'Включить'} #{promo['code']}",
                            callback_data=f"admin:toggle:{promo['id']}",
                        )
                    ]
                )
            buttons.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:back")])
            text, markup = "\n".join(lines), InlineKeyboardMarkup(buttons)
        await safe_edit(query, text, markup)
    elif action == "stats":
        stats = DB.stats()
        await safe_edit(
            query,
            "📊 Статистика бота\n\n"
            f"👥 Пользователей: {stats['users']}\n"
            f"💰 GAVA в обороте: {fmt_gava(stats['circulation'])}\n"
            f"🎮 Активных игр: {stats['active_games']}\n"
            f"⚔️ Активных дуэлей: {stats['active_duels']}\n"
            f"🎟 Активных промокодов: {stats['promos']}",
            admin_keyboard(),
        )
    elif action == "back":
        ADMIN_STATES.pop(query.from_user.id, None)
        await safe_edit(query, "🔐 Панель администратора", admin_keyboard())


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    ensure_user(update)
    data = query.data or ""
    if data == "noop":
        await safe_answer(query, "Эта кнопка уже неактивна.", True)
    elif data == "bonus":
        await handle_bonus(query)
    elif data == "balance":
        row = DB.user(query.from_user.id)
        await safe_answer(query, f"Твой баланс: {fmt_gava(row['balance'] if row else 0)}", True)
    elif data == "top":
        await safe_edit(query, top_text(), start_keyboard())
    elif data.startswith("mines:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[1].isdigit() and parts[2].isdigit():
            await handle_mines_callback(query, int(parts[1]), int(parts[2]))
    elif data.startswith("mines_cash:") and data.split(":")[-1].isdigit():
        await handle_mines_cashout(query, int(data.split(":")[-1]))
    elif data.startswith("coin:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[1].isdigit():
            await handle_coin_callback(query, int(parts[1]), parts[2])
    elif data.startswith("duel:"):
        parts = data.split(":")
        if len(parts) == 3 and parts[2].isdigit():
            await handle_duel_callback(query, int(parts[2]), parts[1])
    elif data.startswith("admin:toggle:") and data.split(":")[-1].isdigit():
        if query.from_user.id == ADMIN_ID:
            DB.toggle_promo(int(data.split(":")[-1]))
            await safe_answer(query, "Статус промокода изменён.")
            await admin_callback(query, "list")
        else:
            await safe_answer(query, "Доступ запрещён.", True)
    elif data.startswith("admin:confirm"):
        await finish_promo_creation(query)
    elif data.startswith("admin:cancel"):
        ADMIN_STATES.pop(query.from_user.id, None)
        await safe_edit(query, "Создание промокода отменено.", admin_keyboard())
    elif data.startswith("admin:"):
        await admin_callback(query, data.split(":")[1])


async def finish_promo_creation(query: Any) -> None:
    if query.from_user.id != ADMIN_ID:
        await safe_answer(query, "Доступ запрещён.", True)
        return
    state = ADMIN_STATES.get(query.from_user.id)
    if not state or state.get("step") != "confirm":
        await safe_answer(query, "Сессия создания промокода истекла.", True)
        return
    if DB.create_promo(
        state["code"],
        state["reward"],
        state["usage_limit"],
        state.get("expires_at"),
        query.from_user.id,
    ):
        text = f"✅ Промокод #{state['code']} создан."
    else:
        text = "❌ Такой промокод уже существует."
    ADMIN_STATES.pop(query.from_user.id, None)
    await safe_answer(query, text)
    await safe_edit(query, text, admin_keyboard())


async def handle_admin_input(update: Update, text: str) -> bool:
    user = update.effective_user
    message = update.effective_message
    if not user or not message or user.id != ADMIN_ID:
        return False
    state = ADMIN_STATES.get(user.id)
    if not state:
        return False
    step = state.get("step")
    if step == "code":
        code = text.strip().lstrip("#").lower()
        if not re.fullmatch(r"[a-z0-9]+", code):
            await message.reply_text("❌ Код должен содержать только латинские буквы и цифры. Попробуй ещё раз:")
            return True
        state.update(code=code, step="reward")
        await message.reply_text("Введите награду в GAVA целым положительным числом:")
        return True
    if step == "reward":
        reward = parse_amount(text)
        if not reward:
            await message.reply_text("❌ Некорректная награда. Введите положительное число:")
            return True
        state.update(reward=reward, step="limit")
        await message.reply_text("Введите максимальное количество активаций:")
        return True
    if step == "limit":
        if not text.strip().isdigit() or int(text.strip()) <= 0:
            await message.reply_text("❌ Лимит должен быть положительным целым числом:")
            return True
        state.update(usage_limit=int(text.strip()), step="expires")
        await message.reply_text(
            "Введите срок действия в часах или отправьте «нет», если срока нет:"
        )
        return True
    if step == "expires":
        value = text.strip().lower()
        expires_at = None
        if value not in {"нет", "нету", "-", "0"}:
            if not value.isdigit() or int(value) <= 0 or int(value) > 87600:
                await message.reply_text("❌ Укажи количество часов или слово «нет»:")
                return True
            expires_at = (utc_now() + timedelta(hours=int(value))).isoformat()
        state.update(expires_at=expires_at, step="confirm")
        expiry_text = "без срока" if not expires_at else f"до {expires_at[:19].replace('T', ' ')} UTC"
        await message.reply_text(
            "Проверь данные промокода:\n"
            f"Код: #{state['code']}\n"
            f"Награда: {fmt_gava(state['reward'])}\n"
            f"Лимит: {state['usage_limit']}\n"
            f"Срок: {expiry_text}",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton("✅ Создать", callback_data="admin:confirm"),
                        InlineKeyboardButton("❌ Отмена", callback_data="admin:cancel"),
                    ]
                ]
            ),
        )
        return True
    return False


async def start_mines(update: Update, amount: int) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    bombs = random.sample(range(36), MINES_BOMBS)
    data = {"bombs": bombs, "revealed": [], "multiplier": 1.0, "current_reward": 0}
    game_id = DB.create_game("mines", user.id, message.chat_id, amount, data)
    if not game_id:
        await message.reply_text("❌ Недостаточно GAVA для этой ставки.")
        return
    sent = await message.reply_text(
        mines_text(DB.game(game_id), data),
        reply_markup=mines_keyboard(game_id, []),
    )
    DB.update_game_message(game_id, sent.message_id)


async def start_coin(update: Update, amount: int) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    game_id = DB.create_game("coin", user.id, message.chat_id, amount, {})
    if not game_id:
        await message.reply_text("❌ Недостаточно GAVA для этой ставки.")
        return
    await message.reply_text(
        f"🪙 МОНЕТА\n💰 Ставка: {fmt_gava(amount)}\nВыбери сторону:",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("🟢 Орёл", callback_data=f"coin:{game_id}:heads"),
                InlineKeyboardButton("🔴 Решка", callback_data=f"coin:{game_id}:tails"),
            ]]
        ),
    )


async def start_instant_game(update: Update, context: ContextTypes.DEFAULT_TYPE, kind: str, amount: int) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user:
        return
    game_id = DB.create_game(kind, user.id, message.chat_id, amount, {})
    if not game_id:
        await message.reply_text("❌ Недостаточно GAVA для этой ставки.")
        return
    emoji = {"dice": "🎲", "dart": "🎯", "slot": "🎰"}[kind]
    try:
        animation = await context.bot.send_dice(chat_id=message.chat_id, emoji=emoji)
        value = animation.dice.value if animation.dice else random.randint(1, 6)
    except TelegramError:
        value = random.randint(1, 6)
    if kind == "dice":
        win = value >= 4
        multiplier = 1.8
        label = f"🎲 Выпало число: {value}"
    elif kind == "dart":
        win = value >= 4
        multiplier = 1.7
        label = f"🎯 Результат дартса: {value}"
    else:
        win = value in {1, 22, 43, 64}
        multiplier = 9.0 if value == 64 else 4.0 if value in {22, 43} else 2.0
        label = f"🎰 Результат слота: {value}"
    payout = int(amount * multiplier) if win else 0
    DB.finish_game(game_id, payout, f"Результат игры {kind}")
    await message.reply_text(
        f"{label}\n💰 Ставка: {fmt_gava(amount)}\n"
        + (f"✅ Победа! Выигрыш: {fmt_gava(payout)}" if win else "❌ Ставка проиграна.")
    )


async def start_duel(update: Update, amount: int) -> None:
    message = update.effective_message
    user = update.effective_user
    target = message.reply_to_message.from_user if message and message.reply_to_message else None
    if not message or not user or not target:
        if message:
            await message.reply_text("❌ Для вызова на дуэль нужно ответить на сообщение игрока.")
        return
    if target.is_bot or target.id == user.id:
        await message.reply_text("❌ Нельзя вызвать на дуэль бота или самого себя.")
        return
    DB.ensure_user(target)
    if not DB.user(user.id) or DB.user(user.id)["balance"] < amount:
        await message.reply_text("❌ У тебя недостаточно GAVA для этой ставки.")
        return
    duel_id = DB.create_duel(user.id, target.id, message.chat_id, amount, None)
    if not duel_id:
        await message.reply_text("❌ Не удалось создать приглашение.")
        return
    sent = await message.reply_text(
        "⚔️ ДУЭЛЬ\n"
        f"👤 {display_name(user)} бросает вызов\n"
        f"👤 {display_name(target)}\n"
        f"💰 Ставка: {fmt_gava(amount)}",
        reply_markup=InlineKeyboardMarkup(
            [[
                InlineKeyboardButton("✅ Принять", callback_data=f"duel:accept:{duel_id}"),
                InlineKeyboardButton("❌ Отказ", callback_data=f"duel:reject:{duel_id}"),
            ]]
        ),
    )
    with DB.lock, DB.connection:
        DB.connection.execute("UPDATE duels SET message_id=? WHERE id=?", (sent.message_id, duel_id))


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    if not message or not user or not message.text:
        return
    ensure_user(update)
    text = message.text.strip()
    if await handle_admin_input(update, text):
        return
    lowered = text.lower().strip()
    if lowered in {"б", "баланс"}:
        row = DB.user(user.id)
        await message.reply_text(f"💰 Твой баланс: {fmt_gava(row['balance'] if row else 0)}")
        return
    if lowered == "топ":
        await message.reply_text(top_text())
        return
    if re.fullmatch(r"#[a-zA-Z0-9]+", text):
        status, reward = DB.redeem_promo(text[1:].lower(), user.id)
        messages = {
            "ok": f"✅ Промокод активирован!\n🎁 Начислено: {fmt_gava(reward)}",
            "missing": "❌ Промокод не найден или выключен.",
            "limit": "❌ Этот промокод больше недоступен.",
            "expired": "❌ Срок действия промокода истёк.",
            "used": "❌ Ты уже использовал этот промокод.",
            "error": "❌ Не удалось активировать промокод.",
        }
        await message.reply_text(messages[status])
        return
    transfer_match = re.fullmatch(r"п\s+([0-9][0-9_, ]*)", lowered)
    if transfer_match:
        amount = parse_amount(transfer_match.group(1))
        target = message.reply_to_message.from_user if message.reply_to_message else None
        if not amount:
            await message.reply_text("❌ Укажи положительную сумму.")
        elif not target:
            await message.reply_text("❌ Для перевода нужно ответить на сообщение получателя.")
        elif target.is_bot or target.id == user.id:
            await message.reply_text("❌ Нельзя переводить GAVA боту или самому себе.")
        else:
            DB.ensure_user(target)
            if DB.transfer(user.id, target.id, amount):
                await message.reply_text(
                    "✅ Перевод выполнен!\n"
                    f"👤 Получатель: {display_name(target)}\n"
                    f"💰 Сумма: {fmt_gava(amount)}"
                )
            else:
                await message.reply_text("❌ Недостаточно GAVA или перевод не прошёл проверку.")
        return
    patterns = [
        (r"мина\s+([0-9][0-9_, ]*)", "mines"),
        (r"кубик\s+([0-9][0-9_, ]*)", "dice"),
        (r"монета\s+([0-9][0-9_, ]*)", "coin"),
        (r"дартс\s+([0-9][0-9_, ]*)", "dart"),
        (r"слот\s+([0-9][0-9_, ]*)", "slot"),
        (r"дуэль\s+([0-9][0-9_, ]*)", "duel"),
    ]
    for pattern, kind in patterns:
        match = re.fullmatch(pattern, lowered)
        if match:
            amount = parse_amount(match.group(1))
            if not amount:
                await message.reply_text("❌ Ставка должна быть положительным целым числом.")
                return
            if kind == "mines":
                await start_mines(update, amount)
            elif kind == "coin":
                await start_coin(update, amount)
            elif kind == "duel":
                await start_duel(update, amount)
            else:
                await start_instant_game(update, context, kind, amount)
            return


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    error = context.error
    if isinstance(error, RetryAfter):
        LOGGER.warning("Telegram попросил повторить запрос через %s секунд", error.retry_after)
    elif isinstance(error, (NetworkError, BadRequest)):
        LOGGER.warning("Ошибка Telegram API: %s", error)
    else:
        LOGGER.exception("Необработанная ошибка бота", exc_info=error)


async def post_init(application: Application) -> None:
    LOGGER.info("Бот GAVA запущен")


def build_application() -> Application:
    if not BOT_TOKEN:
        raise RuntimeError("Не найден BOT_TOKEN или TELEGRAM_BOT_TOKEN в секретах Replit.")
    if not ADMIN_ID:
        raise RuntimeError("Не найден корректный ADMIN_ID.")
    application = Application.builder().token(BOT_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", send_start))
    application.add_handler(CommandHandler("admin", show_admin))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler)
    )
    application.add_error_handler(error_handler)
    return application


if __name__ == "__main__":
    try:
        build_application().run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=False,
        )
    except KeyboardInterrupt:
        LOGGER.info("Бот остановлен")