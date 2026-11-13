from datetime import UTC, datetime
from hashlib import sha256
import json
from pathlib import Path
import sqlite3
from typing import Callable
from uuid import uuid4

from pydantic import ValidationError

from .domain import Message, current_follow_up, normalize_follow_up
from .window import (
    CUSTOMER_PILOT_SCOPE, MAX_VISIBLE_MESSAGES, WINDOW_CONTACT, WINDOW_SCOPE,
    Bubble, WindowError, WindowSnapshot, new_bubbles, normalize_scope,
)


class InputError(Exception):
    pass


def fingerprint(message: Message) -> str:
    normalized = message.model_copy(update={"time": message.time.astimezone(UTC)})
    return sha256(normalized.model_dump_json().encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS leads (
                contact_id TEXT PRIMARY KEY,
                lead_id TEXT NOT NULL UNIQUE,
                first_seen TEXT NOT NULL,
                revision INTEGER NOT NULL DEFAULT 0,
                classified_revision INTEGER NOT NULL DEFAULT 0,
                snapshot TEXT,
                extraction TEXT,
                model TEXT,
                prompt_version INTEGER,
                rules_version INTEGER,
                error TEXT
            );
            CREATE TABLE IF NOT EXISTS messages (
                message_id TEXT PRIMARY KEY,
                contact_id TEXT NOT NULL REFERENCES leads(contact_id),
                occurred_at TEXT NOT NULL,
                recalled_message_id TEXT,
                path TEXT NOT NULL,
                offset INTEGER NOT NULL,
                fingerprint TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS conversation ON messages(contact_id, occurred_at, message_id);
            CREATE TABLE IF NOT EXISTS window_session (
                slot INTEGER PRIMARY KEY CHECK (slot = 1),
                header TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS window_customer_pilot (
                slot INTEGER PRIMARY KEY REFERENCES window_session(slot)
            );
            CREATE TABLE IF NOT EXISTS window_messages (
                sequence INTEGER PRIMARY KEY,
                message_id TEXT NOT NULL UNIQUE REFERENCES messages(message_id),
                payload TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS feishu_sync (
                target TEXT NOT NULL,
                lead_id TEXT NOT NULL REFERENCES leads(lead_id),
                record_id TEXT,
                confirmed_version INTEGER NOT NULL DEFAULT 0,
                pending_fields TEXT,
                client_token TEXT,
                synced_study_profile_hash TEXT,
                confirmed_follow_up TEXT,
                PRIMARY KEY (target, lead_id)
            );
        """)
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(feishu_sync)")}
        sync_columns = ("synced_study_profile_hash", "confirmed_follow_up")
        if any(name not in columns for name in sync_columns):
            with self.db:
                self.db.execute("BEGIN IMMEDIATE")
                columns = {row["name"] for row in self.db.execute("PRAGMA table_info(feishu_sync)")}
                for name in sync_columns:
                    if name not in columns:
                        self.db.execute(f"ALTER TABLE feishu_sync ADD COLUMN {name} TEXT")

    def close(self):
        self.db.close()

    def import_file(self, path: Path) -> dict:
        path = path.resolve()
        inserted = duplicates = 0
        changed_contacts = set()
        first_seen = datetime.now(UTC).isoformat()
        with path.open("rb") as source, self.db:
            self.db.execute("BEGIN IMMEDIATE")
            if self.window_scope():
                raise InputError("请使用另一份数据库导入合成样本")
            line_number = 0
            while True:
                offset = source.tell()
                line = source.readline()
                if not line:
                    break
                line_number += 1
                if not line.strip():
                    continue
                try:
                    message = Message.model_validate_json(line)
                except ValidationError:
                    raise InputError(f"第 {line_number} 行不是有效的消息") from None
                if message.contact_id == WINDOW_CONTACT:
                    raise InputError(f"第 {line_number} 行使用了企微窗口专用联系人 ID")
                digest = fingerprint(message)
                existing = self.db.execute(
                    "SELECT fingerprint FROM messages WHERE message_id = ?", (message.message_id,)
                ).fetchone()
                if existing:
                    if existing["fingerprint"] != digest:
                        raise InputError(f"第 {line_number} 行的消息 ID 与已导入消息冲突")
                    self.db.execute("UPDATE messages SET path = ?, offset = ? WHERE message_id = ?",
                                    (str(path), offset, message.message_id))
                    duplicates += 1
                    continue

                self.db.execute("INSERT OR IGNORE INTO leads (contact_id, lead_id, first_seen) VALUES (?, ?, ?)",
                                (message.contact_id, str(uuid4()), first_seen))
                self.db.execute("""
                    INSERT INTO messages VALUES (?, ?, ?, ?, ?, ?, ?)
                """, (message.message_id, message.contact_id, message.time.astimezone(UTC).isoformat(),
                      message.target_message_id, str(path), offset, digest))
                changed_contacts.add(message.contact_id)
                inserted += 1
            self.db.executemany("UPDATE leads SET revision = revision + 1, error = NULL WHERE contact_id = ?",
                                ((contact_id,) for contact_id in changed_contacts))
        return {"导入消息": inserted, "重复消息": duplicates, "待重算联系人": len(changed_contacts)}

    def window_scope(self) -> str | None:
        row = self.db.execute("""
            SELECT window_customer_pilot.slot AS customer_pilot
            FROM window_session LEFT JOIN window_customer_pilot USING (slot)
            WHERE window_session.slot = 1
        """).fetchone()
        if row is None:
            return None
        return CUSTOMER_PILOT_SCOPE if row["customer_pilot"] else WINDOW_SCOPE

    def capture_window(self, read_snapshot: Callable[[], WindowSnapshot], *, customer_pilot=False) -> dict:
        with self.db:
            self.db.execute("BEGIN IMMEDIATE")
            requested_scope = CUSTOMER_PILOT_SCOPE if customer_pilot else WINDOW_SCOPE
            existing_scope = self.window_scope()
            if existing_scope and existing_scope != requested_scope:
                raise WindowError("采集模式与这份数据库的企微会话范围不符")
            session = self.db.execute("SELECT header FROM window_session WHERE slot = 1").fetchone()
            if session is None and self.db.execute("SELECT 1 FROM leads LIMIT 1").fetchone():
                raise WindowError("请为指定企微会话使用单独的数据库")
            snapshot = read_snapshot()
            if session and json.loads(session["header"]) != snapshot.header:
                raise WindowError("当前企微会话与这份数据库绑定的会话不符")

            recent = self.db.execute("""
                SELECT sequence, payload FROM window_messages ORDER BY sequence DESC LIMIT ?
            """, (MAX_VISIBLE_MESSAGES,)).fetchall()
            previous = []
            for row in reversed(recent):
                message = Message.model_validate_json(row["payload"])
                previous.append(Bubble(speaker=message.speaker, text=message.text))
            added = new_bubbles(previous, snapshot.messages)
            if added:
                observed_at = datetime.now(UTC)
                if recent:
                    last = Message.model_validate_json(recent[0]["payload"])
                    observed_at = max(observed_at, last.time)
                else:
                    self.db.execute("INSERT INTO window_session VALUES (1, ?)", (json.dumps(snapshot.header),))
                    if customer_pilot:
                        self.db.execute("INSERT INTO window_customer_pilot VALUES (1)")
                    self.db.execute("INSERT INTO leads (contact_id, lead_id, first_seen) VALUES (?, ?, ?)",
                                    (WINDOW_CONTACT, str(uuid4()), observed_at.isoformat()))
                start = recent[0]["sequence"] + 1 if recent else 1
                for sequence, bubble in enumerate(added, start):
                    message = Message(message_id=f"window-{sequence:020d}", contact_id=WINDOW_CONTACT,
                                      time=observed_at, speaker=bubble.speaker, text=bubble.text)
                    self.db.execute("""
                        INSERT INTO messages VALUES (?, ?, ?, NULL, '', 0, ?)
                    """, (message.message_id, WINDOW_CONTACT, observed_at.isoformat(), fingerprint(message)))
                    self.db.execute("INSERT INTO window_messages VALUES (?, ?, ?)",
                                    (sequence, message.message_id, message.model_dump_json()))
                self.db.execute("UPDATE leads SET revision = revision + 1, error = NULL WHERE contact_id = ?",
                                (WINDOW_CONTACT,))
        return {"可见文字消息": len(snapshot.messages), "新增消息": len(added), "待重算联系人": int(bool(added))}

    def pending(self, *, reprocess=False):
        return self.db.execute("""
            SELECT contact_id, lead_id, first_seen, revision, classified_revision FROM leads
            WHERE classified_revision < revision OR ?
            ORDER BY contact_id
        """, (reprocess,)).fetchall()

    def conversation(self, contact_id: str) -> list[Message]:
        rows = self.db.execute("""
            SELECT messages.*, window_messages.payload
            FROM messages LEFT JOIN window_messages USING (message_id)
            WHERE contact_id = ? ORDER BY occurred_at, message_id
        """, (contact_id,)).fetchall()
        recalled = {row["recalled_message_id"] for row in rows if row["recalled_message_id"]}
        messages = []
        for row in rows:
            if row["recalled_message_id"] or row["message_id"] in recalled:
                continue
            try:
                if row["payload"] is not None:
                    message = Message.model_validate_json(row["payload"])
                else:
                    with open(row["path"], "rb") as source:
                        source.seek(row["offset"])
                        message = Message.model_validate_json(source.readline())
            except (OSError, ValidationError):
                raise InputError("消息源文件不可重放，请恢复或重新导入原文件") from None
            if fingerprint(message) != row["fingerprint"]:
                raise InputError("消息源文件已变更，请恢复或重新导入原文件")
            messages.append(message)
        return messages

    def save(self, lead, snapshot, extraction, model, prompt_version, rules_version, *, reprocess=False) -> bool:
        version = lead["revision"] + int(reprocess and lead["classified_revision"] == lead["revision"])
        snapshot = {**snapshot, "同步版本": version}
        with self.db:
            cursor = self.db.execute("""
                UPDATE leads SET snapshot = ?, extraction = ?, model = ?, prompt_version = ?, rules_version = ?,
                                 revision = ?, classified_revision = ?, error = NULL
                WHERE contact_id = ? AND revision = ? AND classified_revision = ?
                  AND (classified_revision < revision OR ?)
            """, (json.dumps(snapshot, ensure_ascii=False), extraction.model_dump_json(), model,
                  prompt_version, rules_version, version, version, lead["contact_id"], lead["revision"],
                  lead["classified_revision"], reprocess))
        return cursor.rowcount == 1

    def record_failure(self, lead, error: str):
        with self.db:
            self.db.execute("""
                UPDATE leads SET error = ? WHERE contact_id = ? AND revision = ? AND classified_revision < revision
            """, (error, lead["contact_id"], lead["revision"]))

    def preview(self):
        rows = self.db.execute("""
            SELECT snapshot FROM leads WHERE classified_revision = revision AND snapshot IS NOT NULL ORDER BY contact_id
        """)
        result = []
        now = datetime.now(UTC)
        for row in rows:
            fields = normalize_follow_up(normalize_scope(json.loads(row["snapshot"])))
            timestamp = fields.pop("_last_customer_at", None)
            current_follow_up(fields, datetime.fromisoformat(timestamp) if timestamp else None, now)
            result.append(fields)
        return result

    def sync_state(self, target, lead_id):
        return self.db.execute("SELECT * FROM feishu_sync WHERE target = ? AND lead_id = ?", (target, lead_id)).fetchone()

    def begin_sync(self, target, fields, record_id=None):
        lead_id = fields["线索 ID"]
        token = None if record_id else str(uuid4())
        payload = fields if record_id else {name: value for name, value in fields.items() if value is not None}
        with self.db:
            self.db.execute("""
                INSERT INTO feishu_sync (target, lead_id, record_id, pending_fields, client_token) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT (target, lead_id) DO UPDATE SET record_id = excluded.record_id,
                    pending_fields = excluded.pending_fields, client_token = excluded.client_token
            """, (target, lead_id, record_id, json.dumps(payload, ensure_ascii=False), token))
        return self.sync_state(target, lead_id)

    def confirm_sync(self, target, lead_id, record_id, version, *, study_profile_hash=None, follow_up=None):
        with self.db:
            self.db.execute("""
                INSERT INTO feishu_sync (target, lead_id, record_id, confirmed_version,
                                         synced_study_profile_hash, confirmed_follow_up)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (target, lead_id) DO UPDATE SET record_id = excluded.record_id,
                    confirmed_version = excluded.confirmed_version, pending_fields = NULL, client_token = NULL,
                    synced_study_profile_hash = COALESCE(excluded.synced_study_profile_hash, synced_study_profile_hash),
                    confirmed_follow_up = excluded.confirmed_follow_up
            """, (target, lead_id, record_id, version, study_profile_hash, follow_up))

    def status(self):
        row = self.db.execute("""
            SELECT COUNT(*) AS total,
                   COUNT(CASE WHEN classified_revision = revision THEN 1 END) AS classified,
                   COUNT(CASE WHEN error IS NOT NULL THEN 1 END) AS failed
            FROM leads
        """).fetchone()
        return {"联系人": row["total"], "可预览": row["classified"],
                "待分层": row["total"] - row["classified"], "分层失败": row["failed"]}
