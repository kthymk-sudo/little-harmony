# database/conversation_store.py
# ============================================================
# 🌟 [모듈화] database/db_manager.py에서 "대화 저장/불러오기/이름변경/삭제"
# 관련 로직만 분리.
# ============================================================
import json
import sqlite3
import uuid
import datetime
import pandas as pd
from database.connection import _connect


def _ensure_conversation_table(cursor):
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS tb_conversation (
            id TEXT PRIMARY KEY,
            title TEXT,
            created_at TEXT,
            updated_at TEXT,
            phase TEXT,
            messages_json TEXT,
            conditions_json TEXT,
            stats_json TEXT,
            member_ids_json TEXT,
            reasoning TEXT,
            push_copy TEXT,
            title_custom INTEGER DEFAULT 0
        )
    """)
    cursor.execute("PRAGMA table_info(tb_conversation)")
    existing_cols = [info[1] for info in cursor.fetchall()]
    if 'title_custom' not in existing_cols:
        cursor.execute("ALTER TABLE tb_conversation ADD COLUMN title_custom INTEGER DEFAULT 0")


def new_conversation_id():
    return str(uuid.uuid4())


def make_conversation_title(first_user_text):
    if not first_user_text:
        return "새 타겟 대화"
    text = first_user_text.strip().replace("\n", " ")
    return text[:24] + ("…" if len(text) > 24 else "")


def save_conversation(conv_id, title, phase, messages, conditions, stats, member_ids, reasoning, push_copy):
    try:
        conn = _connect()
        cursor = conn.cursor()
        _ensure_conversation_table(cursor)

        cursor.execute("SELECT created_at, title, title_custom FROM tb_conversation WHERE id = ?", (conv_id,))
        row = cursor.fetchone()
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        created_at = row[0] if row else now
        if row and row[2]:
            final_title, title_custom = row[1], 1
        else:
            final_title, title_custom = title, 0

        cursor.execute(
            """INSERT OR REPLACE INTO tb_conversation
               (id, title, created_at, updated_at, phase, messages_json, conditions_json, stats_json, member_ids_json, reasoning, push_copy, title_custom)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                conv_id, final_title, created_at, now, phase,
                json.dumps(messages or [], ensure_ascii=False),
                json.dumps(conditions or {}, ensure_ascii=False),
                json.dumps(stats or {}, ensure_ascii=False),
                json.dumps(list(member_ids or []), ensure_ascii=False),
                reasoning or "", push_copy or "", title_custom,
            ),
        )
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def rename_conversation(conv_id, new_title):
    try:
        conn = _connect()
        cursor = conn.cursor()
        _ensure_conversation_table(cursor)
        cursor.execute("UPDATE tb_conversation SET title = ?, title_custom = 1 WHERE id = ?", (new_title, conv_id))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass


def list_conversations():
    try:
        conn = _connect()
        cursor = conn.cursor()
        _ensure_conversation_table(cursor)
        conn.commit()
        df = pd.read_sql("SELECT id, title, updated_at FROM tb_conversation ORDER BY updated_at DESC", conn)
        conn.close()
        return df
    except Exception:
        return pd.DataFrame(columns=['id', 'title', 'updated_at'])


def load_conversation(conv_id):
    try:
        conn = _connect()
        df = pd.read_sql("SELECT * FROM tb_conversation WHERE id = ?", conn, params=(conv_id,))
        conn.close()
        if df.empty:
            return None
        row = df.iloc[0]
        return {
            'id': row['id'],
            'title': row['title'],
            'phase': row['phase'] or 'targeting',
            'messages': json.loads(row['messages_json']) if row['messages_json'] else [],
            'conditions': json.loads(row['conditions_json']) if row['conditions_json'] else {},
            'stats': json.loads(row['stats_json']) if row['stats_json'] else {},
            'member_ids': json.loads(row['member_ids_json']) if row['member_ids_json'] else [],
            'reasoning': row['reasoning'] or "",
            'push_copy': row['push_copy'] or "",
        }
    except Exception:
        return None


def delete_conversation(conv_id):
    try:
        conn = _connect()
        cursor = conn.cursor()
        _ensure_conversation_table(cursor)
        cursor.execute("DELETE FROM tb_conversation WHERE id = ?", (conv_id,))
        conn.commit()
        conn.close()
    except sqlite3.Error:
        pass
