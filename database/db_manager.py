import os
import sqlite3
import json
import uuid
import datetime
import glob
import pandas as pd
import numpy as np
import streamlit as st
from config import DB_PATH, ACTIVE_SEGMENT_DAYS, DORMANT_SEGMENT_DAYS
from utils.data_cleaner import standardize_columns


def _connect(db_file=DB_PATH):
    conn = sqlite3.connect(db_file, timeout=20)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def get_data_version():
    try:
        files = [DB_PATH] + glob.glob("harmony_history_*.db")
        return max(os.path.getmtime(f) for f in files if os.path.exists(f))
    except OSError:
        return 0


def upsert_to_db(df_history=None, df_employee=None):
    if df_employee is not None and not df_employee.empty:
        conn = _connect(DB_PATH)
        _upsert_table(conn, df_employee, 'tb_employee', ['R고객번호'])
        conn.close()

    if df_history is not None and not df_history.empty:
        df_history['시청일'] = pd.to_datetime(df_history['시청일'], errors='coerce')
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


@st.cache_data(show_spinner=False, persist="disk", max_entries=5)
def _load_from_db_cached(version):
    conn_main = _connect(DB_PATH)
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
        conn = _connect(DB_PATH)
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


@st.cache_data(show_spinner=False, persist="disk", max_entries=10)
def build_audience_db(_df_history, _df_employee, version=None):
    if _df_history is None or _df_history.empty:
        return _df_history

    df = standardize_columns(_df_history.copy())

    if _df_employee is not None and not _df_employee.empty and 'R고객번호' in _df_employee.columns:
        df_employee = standardize_columns(_df_employee.copy())
        emp_ids = df_employee['R고객번호'].dropna().astype(str).str.strip().tolist()
        df = df[~df['R고객번호'].astype(str).str.strip().isin(emp_ids)]

    return df


@st.cache_data(show_spinner=False, persist="disk", max_entries=30)
def filter_by_period(_db_audience, start_date=None, end_date=None):
    if _db_audience is None or _db_audience.empty or '시청일' not in _db_audience.columns:
        return _db_audience
    if start_date is None and end_date is None:
        return _db_audience

    df = _db_audience[_db_audience['시청일'].notna()].copy()
    if start_date is not None:
        df = df[df['시청일'] >= str(start_date)]
    if end_date is not None:
        df = df[df['시청일'] <= str(end_date)]
    return df


@st.cache_data(show_spinner=False, persist="disk", max_entries=10)
def build_audience_profile(_db_audience, version=None):
    empty_cols = ['R고객번호', '이웃고객명', '성별', '나이', '시청자SO', '선호장르',
                  '선호시청시간대', '선호채널', '선호메뉴', '총시청시간', '시청콘텐츠수', '시청횟수',
                  '최근시청일', '평균시청유지율', '활동세그먼트']
    if _db_audience is None or _db_audience.empty:
        return pd.DataFrame(columns=empty_cols)

    df = _db_audience.copy()

    base_cols = [c for c in ['R고객번호', '이웃고객명', '성별', '나이', '시청자SO'] if c in df.columns]
    base_info = df[base_cols].drop_duplicates(subset=['R고객번호'], keep='first')

    def _mode_by_customer(col_name, out_name):
        if col_name not in df.columns:
            return pd.DataFrame(columns=['R고객번호', out_name])
        valid = df[df[col_name].notna() & (df[col_name].astype(str).str.strip() != '')]
        if valid.empty:
            return pd.DataFrame(columns=['R고객번호', out_name])

        valid = valid.reset_index(drop=True)
        counts = (
            valid.assign(__order=valid.index)
            .groupby(['R고객번호', col_name])
            .agg(__cnt=('__order', 'size'), __first=('__order', 'min'))
            .reset_index()
        )
        counts = counts.sort_values(['__cnt', '__first'], ascending=[False, True], kind='mergesort')
        result = (
            counts.drop_duplicates(subset='R고객번호', keep='first')[['R고객번호', col_name]]
            .rename(columns={col_name: out_name})
        )
        return result

    pref_genre = _mode_by_customer('장르', '선호장르')
    pref_time = _mode_by_customer('시청시간대', '선호시청시간대')
    pref_channel = _mode_by_customer('채널명', '선호채널')
    pref_menu = _mode_by_customer('메뉴명', '선호메뉴')

    agg_dict = {'시청콘텐츠수': ('콘텐츠ID', 'nunique'), '시청횟수': ('콘텐츠ID', 'count')}
    if '시청시간' in df.columns:
        agg_dict['총시청시간'] = ('시청시간', 'sum')
    if '시청 유지율' in df.columns:
        agg_dict['평균시청유지율'] = ('시청 유지율', 'mean')
    behavior = df.groupby('R고객번호').agg(**agg_dict).reset_index()

    if '시청일' in df.columns:
        recent = df.groupby('R고객번호')['시청일'].max().reset_index().rename(columns={'시청일': '최근시청일'})
        ref_date = pd.to_datetime(df['시청일'], errors='coerce').max()
        if pd.notna(ref_date):
            days_since = (ref_date - pd.to_datetime(recent['최근시청일'], errors='coerce')).dt.days
            recent['활동세그먼트'] = pd.cut(
                days_since,
                bins=[-1, ACTIVE_SEGMENT_DAYS, DORMANT_SEGMENT_DAYS, float('inf')],
                labels=['활성', '휴면', '이탈위험'],
            ).astype(str)
    else:
        recent = pd.DataFrame(columns=['R고객번호', '최근시청일', '활동세그먼트'])

    profile = base_info
    for part in [pref_genre, pref_time, pref_channel, pref_menu, behavior, recent]:
        if not part.empty:
            profile = pd.merge(profile, part, on='R고객번호', how='left')

    for c in empty_cols:
        if c not in profile.columns:
            profile[c] = np.nan

    return profile.fillna({
        '선호장르': '', '선호시청시간대': '', '선호채널': '', '선호메뉴': '', '활동세그먼트': '미분류',
    })


def summarize_profile_context(profile_df):
    if profile_df is None or profile_df.empty:
        return "현재 집계된 시청자 프로필 데이터가 없습니다."

    gender_counts = profile_df['성별'].value_counts().to_dict() if '성별' in profile_df.columns else {}
    so_list = sorted(profile_df['시청자SO'].dropna().unique().tolist()) if '시청자SO' in profile_df.columns else []
    genre_list = sorted([g for g in profile_df['선호장르'].dropna().unique().tolist() if g]) if '선호장르' in profile_df.columns else []
    time_list = sorted([t for t in profile_df['선호시청시간대'].dropna().unique().tolist() if t]) if '선호시청시간대' in profile_df.columns else []
    channel_list = sorted([c for c in profile_df['선호채널'].dropna().unique().tolist() if c]) if '선호채널' in profile_df.columns else []
    menu_list = sorted([m for m in profile_df['선호메뉴'].dropna().unique().tolist() if m]) if '선호메뉴' in profile_df.columns else []
    segment_counts = (
        profile_df[profile_df['활동세그먼트'] != '미분류']['활동세그먼트'].value_counts().to_dict()
        if '활동세그먼트' in profile_df.columns else {}
    )

    def _dist_line(col, unit="", digits=1):
        if col not in profile_df.columns or profile_df[col].dropna().empty:
            return None
        s = pd.to_numeric(profile_df[col], errors='coerce').dropna()
        if s.empty:
            return None
        return f"평균 {round(s.mean(), digits)}{unit} / 상위 25%는 {round(s.quantile(0.75), digits)}{unit} 이상"

    retention_line = _dist_line('평균시청유지율', unit="%")
    watchtime_line = None
    if '총시청시간' in profile_df.columns and not profile_df['총시청시간'].dropna().empty:
        minutes = pd.to_numeric(profile_df['총시청시간'], errors='coerce').dropna() / 60
        if not minutes.empty:
            watchtime_line = f"평균 {round(minutes.mean(), 1)}분 / 상위 25%는 {round(minutes.quantile(0.75), 1)}분 이상"
    content_line = _dist_line('시청콘텐츠수', unit="개", digits=1)

    lines = [
        f"- 전체 시청자 수: {len(profile_df)}명",
        f"- 성별 분포: {gender_counts}",
        f"- SO(지역) 목록: {so_list}",
        f"- 선호 장르 목록: {genre_list}",
        f"- 선호 시청시간대 목록: {time_list}",
        f"- 선호 채널 목록: {channel_list}",
        f"- 선호 메뉴 목록: {menu_list}",
    ]
    if segment_counts:
        lines.append(f"- 활동세그먼트 분포(활성/휴면/이탈위험): {segment_counts}")
    if retention_line:
        lines.append(f"- 시청 유지율 분포(끝까지 보는 정도): {retention_line}")
    if watchtime_line:
        lines.append(f"- 총시청시간(분) 분포: {watchtime_line}")
    if content_line:
        lines.append(f"- 시청콘텐츠수 분포(다양하게 보는 정도): {content_line}")

    return "\n".join(lines)


def _matching_ids_by_keyword(db_audience, col_name, keyword):
    if db_audience is None or db_audience.empty or col_name not in db_audience.columns or not keyword:
        return set()
    mask = db_audience[col_name].astype(str).str.contains(str(keyword), case=False, na=False, regex=False)
    return set(db_audience.loc[mask, 'R고객번호'].astype(str).unique().tolist())


def _as_list(val):
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        return list(val)
    return [val]


def _to_num(val):
    if val is None:
        return None
    try:
        num = pd.to_numeric(val, errors='coerce')
    except (TypeError, ValueError):
        return None
    return None if pd.isna(num) else num


def _filter_by_conditions(profile_df, conditions, db_audience=None):
    df = profile_df

    if conditions.get('성별'):
        df = df[df['성별'].isin(_as_list(conditions['성별']))]
    if conditions.get('나이대'):
        age_band = (pd.to_numeric(df['나이'], errors='coerce') // 10 * 10).astype('Int64').astype(str) + '대'
        df = df[age_band.isin(_as_list(conditions['나이대']))]
    min_age = _to_num(conditions.get('나이최소'))
    if min_age is not None:
        df = df[pd.to_numeric(df['나이'], errors='coerce') >= min_age]
    max_age = _to_num(conditions.get('나이최대'))
    if max_age is not None:
        df = df[pd.to_numeric(df['나이'], errors='coerce') <= max_age]
    if conditions.get('SO'):
        df = df[df['시청자SO'].isin(_as_list(conditions['SO']))]
    if conditions.get('선호장르'):
        df = df[df['선호장르'].isin(_as_list(conditions['선호장르']))]
    if conditions.get('선호시청시간대'):
        df = df[df['선호시청시간대'].isin(_as_list(conditions['선호시청시간대']))]
    if conditions.get('선호채널') and '선호채널' in df.columns:
        df = df[df['선호채널'].isin(_as_list(conditions['선호채널']))]
    if conditions.get('선호메뉴') and '선호메뉴' in df.columns:
        df = df[df['선호메뉴'].isin(_as_list(conditions['선호메뉴']))]
    if conditions.get('활동세그먼트') and '활동세그먼트' in df.columns:
        df = df[df['활동세그먼트'].isin(_as_list(conditions['활동세그먼트']))]
    min_watch_cnt = _to_num(conditions.get('최소시청횟수'))
    if min_watch_cnt is not None:
        df = df[pd.to_numeric(df['시청횟수'], errors='coerce') >= min_watch_cnt]
    if conditions.get('최근시청일이후'):
        df = df[df['최근시청일'] >= str(conditions['최근시청일이후'])]
    min_retention = _to_num(conditions.get('최소시청유지율'))
    if min_retention is not None and '평균시청유지율' in df.columns:
        df = df[pd.to_numeric(df['평균시청유지율'], errors='coerce') >= min_retention]
    min_watch_minutes = _to_num(conditions.get('최소총시청시간_분'))
    if min_watch_minutes is not None and '총시청시간' in df.columns:
        df = df[(pd.to_numeric(df['총시청시간'], errors='coerce') / 60) >= min_watch_minutes]
    min_content_cnt = _to_num(conditions.get('최소시청콘텐츠수'))
    if min_content_cnt is not None and '시청콘텐츠수' in df.columns:
        df = df[pd.to_numeric(df['시청콘텐츠수'], errors='coerce') >= min_content_cnt]
    if conditions.get('콘텐츠명포함'):
        ids = _matching_ids_by_keyword(db_audience, '영상명', conditions['콘텐츠명포함'])
        df = df[df['R고객번호'].astype(str).isin(ids)]
    if conditions.get('채널명포함'):
        ids = _matching_ids_by_keyword(db_audience, '채널명', conditions['채널명포함'])
        df = df[df['R고객번호'].astype(str).isin(ids)]

    return df


def summarize_segment_insight(profile_df, conditions, db_audience=None, top_n=3):
    if profile_df is None or profile_df.empty or not conditions:
        return None

    df = _filter_by_conditions(profile_df.copy(), conditions, db_audience)
    if df.empty:
        return {'대상자수': 0, '전체시청자수': len(profile_df)}

    def _top(col):
        if col not in df.columns:
            return []
        s = df[col].dropna()
        s = s[s != '']
        if s.empty:
            return []
        return [f"{name}({int(cnt)}명)" for name, cnt in s.value_counts().head(top_n).items()]

    def _mean(col, divide=1):
        if col not in df.columns or df.empty:
            return 0
        val = pd.to_numeric(df[col], errors='coerce').mean()
        return round(val / divide, 1) if pd.notna(val) else 0

    return {
        '대상자수': len(df),
        '전체시청자수': len(profile_df),
        '선호장르_상위': _top('선호장르'),
        '선호채널_상위': _top('선호채널'),
        '선호메뉴_상위': _top('선호메뉴'),
        '평균시청유지율': _mean('평균시청유지율'),
        '평균총시청시간(분)': _mean('총시청시간', divide=60),
        '평균시청콘텐츠수': _mean('시청콘텐츠수'),
    }


def format_segment_insight_reply(insight):
    if insight is None:
        return "죄송해요, 그 조건에 맞는 데이터를 찾지 못했어요. 조건을 조금 다르게 다시 말씀해주시겠어요?"

    n = insight.get('대상자수', 0)
    total = insight.get('전체시청자수', 0)
    if n == 0:
        return "말씀하신 조건에 정확히 맞는 시청자는 없었어요. 나이대나 성별 등을 조금 다르게 말씀해주시겠어요?"

    share = f"{n / total * 100:.1f}%" if total else "-"
    lines = [f"이 조건에 해당하는 시청자는 {n:,}명이에요(전체 시청자의 {share})."]

    def _join(top_list, label):
        if not top_list:
            return None
        return f"{label}는 {', '.join(top_list)} 순으로 많이 봤어요"

    detail_parts = [
        p for p in (
            _join(insight.get('선호장르_상위'), "선호 장르"),
            _join(insight.get('선호채널_상위'), "선호 채널"),
            _join(insight.get('선호메뉴_상위'), "선호 메뉴"),
        ) if p
    ]
    if detail_parts:
        lines.append(". ".join(detail_parts) + ".")
    else:
        lines.append("다만 선호 장르/채널/메뉴 데이터가 충분하지 않아 구체적인 순위는 확인하기 어려웠어요.")

    lines.append("이 조건으로 타겟을 잡아볼까요?")
    return " ".join(lines)


def apply_target_conditions(profile_df, conditions, db_audience=None):
    empty_stats = {
        '대상자수': 0, '전체시청자수': len(profile_df) if profile_df is not None else 0,
        '평균시청횟수': 0, '평균총시청시간(분)': 0, '평균시청유지율': 0, '평균시청콘텐츠수': 0,
        '전체평균시청횟수': 0, '전체평균총시청시간(분)': 0, '전체평균시청유지율': 0,
    }
    if profile_df is None or profile_df.empty or not conditions:
        return profile_df, empty_stats

    exclude_cond = conditions.get('제외조건')
    include_cond = {k: v for k, v in conditions.items() if k != '제외조건'}

    df = _filter_by_conditions(profile_df.copy(), include_cond, db_audience)

    if isinstance(exclude_cond, dict) and exclude_cond:
        excluded_df = _filter_by_conditions(profile_df.copy(), exclude_cond, db_audience)
        excluded_ids = set(excluded_df['R고객번호'].astype(str).unique().tolist())
        if excluded_ids:
            df = df[~df['R고객번호'].astype(str).isin(excluded_ids)]

    def _mean(frame, col, divide=1):
        if col not in frame.columns or frame.empty:
            return 0
        val = pd.to_numeric(frame[col], errors='coerce').mean()
        return round(val / divide, 1) if pd.notna(val) else 0

    stats = {
        '대상자수': len(df),
        '전체시청자수': len(profile_df),
        '평균시청횟수': _mean(df, '시청횟수'),
        '평균총시청시간(분)': _mean(df, '총시청시간', divide=60),
        '평균시청유지율': _mean(df, '평균시청유지율'),
        '평균시청콘텐츠수': _mean(df, '시청콘텐츠수'),
        '전체평균시청횟수': _mean(profile_df, '시청횟수'),
        '전체평균총시청시간(분)': _mean(profile_df, '총시청시간', divide=60),
        '전체평균시청유지율': _mean(profile_df, '평균시청유지율'),
    }
    return df, stats


_CONDITION_LABEL_MAP = {
    '성별': '성별', '나이대': '나이대', '나이최소': '나이(최소)', '나이최대': '나이(최대)',
    'SO': 'SO(지역)',
    '선호장르': '선호장르', '선호시청시간대': '선호시청시간대',
    '선호채널': '선호채널', '선호메뉴': '선호메뉴',
    '최소시청횟수': '최소시청횟수', '최근시청일이후': '최근시청일이후',
    '최소시청유지율': '최소 시청유지율(%)',
    '최소총시청시간_분': '최소 총시청시간(분)',
    '최소시청콘텐츠수': '최소 시청콘텐츠수',
    '활동세그먼트': '활동세그먼트',
    '콘텐츠명포함': '콘텐츠명 포함',
    '채널명포함': '채널명 포함',
}


def _format_condition_parts(conditions):
    parts = []
    for key, label in _CONDITION_LABEL_MAP.items():
        val = (conditions or {}).get(key)
        if val:
            if isinstance(val, list):
                parts.append(f"{label}={','.join(map(str, val))}")
            else:
                parts.append(f"{label}={val}")
    return parts


def _build_cond_str(conditions, empty_text):
    conditions = conditions or {}
    cond_parts = _format_condition_parts(conditions)
    exclude_cond = conditions.get('제외조건')
    if isinstance(exclude_cond, dict) and exclude_cond:
        exclude_parts = _format_condition_parts(exclude_cond)
        if exclude_parts:
            cond_parts.append(f"제외: {', '.join(exclude_parts)}")
    return ", ".join(cond_parts) if cond_parts else empty_text


def format_conditions_line(conditions):
    return _build_cond_str(conditions, "아직 확정된 조건 없음")


def format_target_summary(conditions, stats):
    cond_str = _build_cond_str(conditions, "조건 없음(전체 시청자)")

    stats = stats or {}
    stats_str = (
        f"대상자수 {stats.get('대상자수', 0)}명 (전체 시청자 {stats.get('전체시청자수', 0)}명 중), "
        f"평균 시청횟수 {stats.get('평균시청횟수', 0)}회(전체 평균 {stats.get('전체평균시청횟수', 0)}회), "
        f"평균 총 시청시간 {stats.get('평균총시청시간(분)', 0)}분(전체 평균 {stats.get('전체평균총시청시간(분)', 0)}분), "
        f"평균 시청유지율 {stats.get('평균시청유지율', 0)}%(전체 평균 {stats.get('전체평균시청유지율', 0)}%), "
        f"평균 시청콘텐츠수 {stats.get('평균시청콘텐츠수', 0)}개"
    )
    return f"[적용 조건] {cond_str}\n[집계] {stats_str}"


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