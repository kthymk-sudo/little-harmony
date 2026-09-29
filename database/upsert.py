# database/upsert.py
# ============================================================
# 🌟 [모듈화] database/db_manager.py에서 "시청내역/직원리스트를 SQLite에
# 적재(upsert)"하는 로직만 분리.
#
# 🌟 [버그 수정] 기존에는 df_history를 그 자리에서 직접 수정(in-place)했고,
# 날짜를 인식하지 못한 행은 아무 안내 없이 조용히 버려졌다. 호출자가 넘긴
# 원본 DataFrame을 보존하도록 .copy()를 추가하고, 버려지는 행이 있으면
# 실무자가 알 수 있도록 화면에 경고를 띄우도록 수정.
#
# 🌟 [콘텐츠 통계 추가] 콘텐츠별 누적 통계(tb_content)도 콘텐츠ID 기준으로
# 적재한다 - 최신 파일이 오면 같은 콘텐츠의 값을 덮어쓴다.
#
# 🌟 [DB 폴더 일원화] data/ 폴더에 직원목록.db / 콘텐츠통계.db / 시청_YYYY_MM.db로
# 나눠 저장한다. 시청이력은 저장할 때 가공(직원 제외/시작시각/유지율 정리)까지 끝낸
# "분석용 완성본"으로 저장한다 - 앱은 필요한 달 파일을 꺼내 그대로 쓴다.
# ============================================================
import streamlit as st
import pandas as pd
from config import CONTENT_DB_PATH, EMPLOYEE_DB_PATH
from database.connection import _connect, history_db_path
from database.loader import optimize_db
from utils.data_cleaner import finalize_history


def _employee_ids():
    conn = _connect(EMPLOYEE_DB_PATH)
    try:
        return [r[0] for r in conn.execute('SELECT "R고객번호" FROM tb_employee')]
    except Exception:
        return []
    finally:
        conn.close()


def upsert_to_db(df_history=None, df_employee=None, df_content=None):
    if df_employee is not None and not df_employee.empty:
        conn = _connect(EMPLOYEE_DB_PATH)
        _upsert_table(conn, df_employee, 'tb_employee', ['R고객번호'])
        conn.close()

    if df_content is not None and not df_content.empty:
        conn = _connect(CONTENT_DB_PATH)
        _upsert_table(conn, df_content, 'tb_content', ['콘텐츠ID'])
        conn.close()

    if df_history is not None and not df_history.empty:
        # 직원 명단이 같이 올라왔다면 위에서 먼저 저장했으므로 최신 명단으로 가공된다
        employee_ids = _employee_ids()
        if not employee_ids:
            st.warning("⚠️ 저장된 당사직원 목록이 없어 직원 시청이 제외되지 않은 채로 저장됩니다. 직원 목록을 먼저 올려주세요.")
        df_history = finalize_history(df_history, employee_ids)
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
            db_name = history_db_path(month_str)
            conn_hist = _connect(db_name)

            save_df = month_df.drop(columns=['month_key']).copy()
            save_df['시청일'] = save_df['시청일'].dt.strftime('%Y-%m-%d')

            # 🌟 [버그 수정 - 중복 판정] 날짜+시간대가 아니라 초 단위 시청일시로 중복을 판정한다
            # (같은 시간대에 같은 영상을 여러 번 본 서로 다른 기록이 지워지던 문제).
            _upsert_table(conn_hist, save_df, 'tb_history', ['콘텐츠ID', 'R고객번호', '시청일시'])

            idx_cursor = conn_hist.cursor()
            # 같은 달을 다시 올리면 그 달 파일에 현재 직원 명단을 다시 적용한다
            # (예전 명단에서 빠져 있던 직원의 기존 기록까지 정리)
            if employee_ids:
                idx_cursor.executemany('DELETE FROM tb_history WHERE "R고객번호" = ?', [(i,) for i in employee_ids])
            idx_cursor.execute('CREATE INDEX IF NOT EXISTS idx_tb_history_date ON tb_history("시청일")')
            idx_cursor.execute('DROP INDEX IF EXISTS idx_tb_history_dedup')
            idx_cursor.execute('CREATE INDEX IF NOT EXISTS idx_tb_history_dedup2 ON tb_history("콘텐츠ID", "R고객번호", "시청일시")')
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
