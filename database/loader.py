# database/loader.py
# ============================================================
# 🌟 [모듈화] database/db_manager.py에서 "캐시된 데이터 로딩" 관련 함수만 분리.
# ============================================================
import glob
import pandas as pd
import streamlit as st
from database.connection import _connect, get_data_version


@st.cache_data(show_spinner=False, persist="disk", max_entries=5)
def _load_from_db_cached(version):
    conn_main = _connect()
    try:
        df_employee = pd.read_sql("SELECT * FROM tb_employee", conn_main)
    except Exception:
        df_employee = pd.DataFrame(columns=['R고객번호'])
    finally:
        conn_main.close()

    history_files = glob.glob("harmony_history_*.db")
    df_list = []

    for db_file in history_files:
        conn_hist = _connect(db_file)
        try:
            df = pd.read_sql("SELECT * FROM tb_history", conn_hist)
            df_list.append(df)
        except Exception:
            pass
        finally:
            conn_hist.close()

    if df_list:
        df_history = pd.concat(df_list, ignore_index=True)
    else:
        df_history = pd.DataFrame(columns=['콘텐츠ID', 'R고객번호', '이웃고객명', '성별', '나이', '시청자SO',
                                           '채널명', '메뉴명', '장르', '영상명', '러닝타임', '시청시간',
                                           '시청일', '시청시간대', '시청 유지율'])

    return df_history, df_employee


def load_from_db():
    return _load_from_db_cached(get_data_version())


def optimize_db(conn=None):
    should_close = False
    if conn is None:
        conn = _connect()
        should_close = True

    try:
        cur = conn.cursor()
        cur.execute("PRAGMA page_count")
        page_count = cur.fetchone()[0]
        cur.execute("PRAGMA freelist_count")
        freelist_count = cur.fetchone()[0]
        if page_count > 0 and (freelist_count / page_count) > 0.1:
            conn.execute("VACUUM")
            conn.commit()
    except Exception:
        pass
    finally:
        if should_close:
            conn.close()


def has_history_data():
    history_files = glob.glob("harmony_history_*.db")
    if not history_files:
        return False

    for f in history_files:
        try:
            conn = _connect(f)
            cur = conn.cursor()
            cur.execute("SELECT EXISTS(SELECT 1 FROM tb_history LIMIT 1)")
            result = bool(cur.fetchone()[0])
            conn.close()
            if result:
                return True
        except Exception:
            pass
    return False


@st.cache_data(show_spinner=False, persist="disk", max_entries=5)
def _load_employee_cached(version):
    conn = _connect()
    try:
        df_employee = pd.read_sql("SELECT * FROM tb_employee", conn)
    except Exception:
        df_employee = pd.DataFrame(columns=['R고객번호'])
    conn.close()
    return df_employee


def load_employee_list():
    return _load_employee_cached(get_data_version())


_HISTORY_FALLBACK_COLS = ['콘텐츠ID', 'R고객번호', '이웃고객명', '성별', '나이', '시청자SO',
                          '채널명', '메뉴명', '장르', '영상명', '러닝타임', '시청시간',
                          '시청일', '시청시간대', '시청 유지율']


@st.cache_data(show_spinner=False, persist="disk", max_entries=30)
def _load_history_period_cached(version, start_date, end_date):
    history_files = glob.glob("harmony_history_*.db")
    df_list = []

    clauses, params = [], []
    if start_date is not None:
        clauses.append('"시청일" >= ?')
        params.append(str(start_date))
    if end_date is not None:
        clauses.append('"시청일" <= ?')
        params.append(str(end_date))

    query = "SELECT * FROM tb_history"
    if clauses:
        query += " WHERE " + " AND ".join(clauses)

    for db_file in history_files:
        conn = _connect(db_file)
        try:
            df = pd.read_sql(query, conn, params=params)
            df_list.append(df)
        except Exception:
            pass
        finally:
            conn.close()

    if df_list:
        return pd.concat(df_list, ignore_index=True)
    return pd.DataFrame(columns=_HISTORY_FALLBACK_COLS)


def load_history_period(start_date, end_date):
    return _load_history_period_cached(get_data_version(), start_date, end_date)
