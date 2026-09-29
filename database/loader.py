# database/loader.py
# ============================================================
# 🌟 [모듈화] database/db_manager.py에서 "캐시된 데이터 로딩" 관련 함수만 분리.
#
# 🌟 [DB 폴더 일원화] data/시청_YYYY_MM.db는 이미 가공이 끝난 분석용 완성본이라,
# 여기서는 필요한 달 파일만 골라 꺼내 쓰기만 한다(전체 로딩용 load_from_db와 직원
# 목록 로딩은 더 이상 필요 없어 제거 - 직원 제외는 저장할 때 끝난다).
# ============================================================
import pandas as pd
import streamlit as st
from config import CONTENT_DB_PATH
from database.connection import _connect, get_data_version, history_db_files, history_db_month


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
    history_files = history_db_files()
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
def _load_content_cached(version):
    conn = _connect(CONTENT_DB_PATH)
    try:
        return pd.read_sql("SELECT * FROM tb_content", conn)
    except Exception:
        return pd.DataFrame(columns=['콘텐츠ID'])
    finally:
        conn.close()


def load_content():
    """콘텐츠별 누적 통계(tb_content). 기간 필터 대상이 아니다 - 파일 추출 시점까지의 누적값."""
    return _load_content_cached(get_data_version())


_HISTORY_FALLBACK_COLS = ['콘텐츠ID', 'R고객번호', '이웃고객명', '성별', '나이', '시청자SO',
                          '채널명', '메뉴명', '장르', '영상명', '러닝타임', '시청시간',
                          '시청일', '시청시간대', '시청 유지율', '시청일시', '시청시작시']


def _months_in_period(start_date, end_date):
    """기간에 걸치는 달만 고른다(파일명 'YYYY-MM'과 문자열 비교)."""
    lo = str(start_date)[:7] if start_date is not None else None
    hi = str(end_date)[:7] if end_date is not None else None
    return [
        f for f in history_db_files()
        if (lo is None or history_db_month(f) >= lo) and (hi is None or history_db_month(f) <= hi)
    ]


@st.cache_data(show_spinner=False, persist="disk", max_entries=30)
def _load_history_period_cached(version, start_date, end_date):
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

    for db_file in _months_in_period(start_date, end_date):
        conn = _connect(db_file)
        try:
            df = pd.read_sql(query, conn, params=params)
            df_list.append(df)
        except Exception:
            pass
        finally:
            conn.close()

    if not df_list:
        return pd.DataFrame(columns=_HISTORY_FALLBACK_COLS)
    df = pd.concat(df_list, ignore_index=True)
    if '나이' in df.columns:  # 미상(NULL)이 섞이면 실수로 읽혀 45.0처럼 보이므로 정수형으로 되돌린다
        df['나이'] = pd.to_numeric(df['나이'], errors='coerce').astype('Int64')
    return df


def load_history_period(start_date=None, end_date=None):
    """시청기간에 걸치는 달 파일만 열어서 가공 완료된 시청이력을 돌려준다(기간 없으면 전체)."""
    return _load_history_period_cached(get_data_version(), start_date, end_date)
