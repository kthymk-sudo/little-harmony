# database/upsert.py
# ============================================================
# 🌟 [모듈화] database/db_manager.py에서 "시청내역/직원리스트를 SQLite에
# 적재(upsert)"하는 로직만 분리.
#
# 🌟 [버그 수정] 기존에는 df_history를 그 자리에서 직접 수정(in-place)했고,
# 날짜를 인식하지 못한 행은 아무 안내 없이 조용히 버려졌다. 호출자가 넘긴
# 원본 DataFrame을 보존하도록 .copy()를 추가하고, 버려지는 행이 있으면
# 실무자가 알 수 있도록 화면에 경고를 띄우도록 수정.
# ============================================================
import streamlit as st
import pandas as pd
from database.connection import _connect
from database.loader import optimize_db


def upsert_to_db(df_history=None, df_employee=None):
    if df_employee is not None and not df_employee.empty:
        conn = _connect()
        _upsert_table(conn, df_employee, 'tb_employee', ['R고객번호'])
        conn.close()

    if df_history is not None and not df_history.empty:
        df_history = df_history.copy()
        df_history['시청일'] = pd.to_datetime(df_history['시청일'], errors='coerce')
        invalid_count = int(df_history['시청일'].isna().sum())
        if invalid_count > 0:
            st.warning(
                f"⚠️ 시청일을 인식하지 못해 저장에서 제외된 데이터가 {invalid_count}건 있습니다. "
                "원본 파일의 날짜 형식을 확인해주세요."
            )
        valid_history = df_history.dropna(subset=['시청일']).copy()
        valid_history['month_key'] = valid_history['시청일'].dt.strftime('%Y_%m')

        for month_str, month_df in valid_history.groupby('month_key'):
            db_name = f"harmony_history_{month_str}.db"
            conn_hist = _connect(db_name)

            save_df = month_df.drop(columns=['month_key']).copy()
            save_df['시청일'] = save_df['시청일'].dt.strftime('%Y-%m-%d')

            _upsert_table(conn_hist, save_df, 'tb_history', ['콘텐츠ID', 'R고객번호', '시청일', '시청시간대'])

            idx_cursor = conn_hist.cursor()
            idx_cursor.execute('CREATE INDEX IF NOT EXISTS idx_tb_history_date ON tb_history("시청일")')
            idx_cursor.execute('CREATE INDEX IF NOT EXISTS idx_tb_history_dedup ON tb_history("콘텐츠ID", "R고객번호", "시청일", "시청시간대")')
            conn_hist.commit()

            optimize_db(conn_hist)
            conn_hist.close()


def _upsert_table(conn, df, table_name, subset_keys):
    valid_keys = [k for k in subset_keys if k in df.columns]
    for k in valid_keys:
        df[k] = df[k].astype(str).str.strip()
        df[k] = df[k].replace({'nan': 'UNKNOWN', 'None': 'UNKNOWN', '': 'UNKNOWN'})
        df[k] = df[k].fillna('UNKNOWN')
    if valid_keys:
        df = df.drop_duplicates(subset=valid_keys, keep='last')

    cursor = conn.cursor()
    cursor.execute("SELECT count(name) FROM sqlite_master WHERE type='table' AND name=?", (table_name,))

    if cursor.fetchone()[0] == 0:
        df.to_sql(table_name, conn, if_exists='replace', index=False)
    else:
        cursor.execute(f"PRAGMA table_info('{table_name}')")
        existing_cols = [info[1] for info in cursor.fetchall()]
        for col in df.columns:
            if col not in existing_cols:
                cursor.execute(f'ALTER TABLE "{table_name}" ADD COLUMN "{col}" TEXT')

        temp_table = f"temp_{table_name}"
        df.to_sql(temp_table, conn, if_exists='replace', index=False)
        cols_str = ", ".join([f'"{c}"' for c in df.columns])

        if valid_keys:
            if len(valid_keys) == 1:
                where_clause = f'"{valid_keys[0]}" IN (SELECT "{valid_keys[0]}" FROM "{temp_table}")'
            else:
                keys_str = ", ".join([f'"{k}"' for k in valid_keys])
                where_clause = f'({keys_str}) IN (SELECT {keys_str} FROM "{temp_table}")'
            cursor.execute(f'DELETE FROM "{table_name}" WHERE {where_clause}')

        cursor.execute(f'INSERT INTO "{table_name}" ({cols_str}) SELECT {cols_str} FROM "{temp_table}"')
        cursor.execute(f'DROP TABLE "{temp_table}"')
    conn.commit()
