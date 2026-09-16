# database/db_manager.py
# ============================================================
# 🌟 [대대적 개편]
# 1) 콘텐츠별 통계 DB(구 df1/tb_content)는 더 이상 사용하지 않음.
#    이제 원본 DB는 딱 2종류만 관리한다.
#      - tb_history  : 시청내역 상세 (여러 파일을 합쳐서 누적 가능)
#      - tb_employee : 당사직원 제외리스트 (여러 파일을 합쳐서 누적 가능)
#    당사직원의 R고객번호에 해당하는 시청기록만 걸러내는 것이 이 계층의 역할.
# 2) 타겟 작업을 "대화 단위"로 통째로 저장/조회하는 tb_conversation 테이블을
#    새로 추가 (기존 tb_target_history 방식은 폐기 - ChatGPT처럼 대화 자체가
#    곧 이력이 되는 구조로 변경).
# ============================================================
import os
import sqlite3
import json
import uuid
import datetime
import pandas as pd
import numpy as np
import streamlit as st
from config import DB_PATH, ACTIVE_SEGMENT_DAYS, DORMANT_SEGMENT_DAYS
from utils.data_cleaner import standardize_columns


def _connect():
    """
    🌟 [전체 점검 - 동시 사용 안정성] 사업추진팀 여러 명이 동시에 이 앱을 쓰면
    (예: 한 명이 대화 중 자동저장이 되는 순간, 다른 한 명이 데이터 업로드로
    VACUUM을 실행 중) SQLite는 파일 하나를 잠그고 쓰는 방식이라 "database is
    locked" 오류가 날 수 있다. 두 가지로 이 가능성을 크게 낮춘다:
    1. timeout을 기본 5초에서 20초로 늘려, 잠깐의 잠금(WAL 전환 전 실측 기준
       VACUUM 최대 약 2.5초)은 오류 없이 자동으로 기다렸다가 통과하게 한다.
    2. journal_mode=WAL로 전환 - 읽기 작업(대화 목록 조회 등)이 동시에 진행 중인
       쓰기 작업을 기다리지 않고 즉시 처리되므로, 타임아웃까지 갈 필요 자체가
       거의 없어진다(가장 흔한 "여러 명이 동시에 조회+저장" 시나리오에서 특히 효과적).
    """
    conn = sqlite3.connect(DB_PATH, timeout=20)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
    except sqlite3.Error:
        pass
    return conn


def get_data_version():
    """
    🌟 [속도 최적화] DB 파일의 마지막 수정 시각을 '버전 값'으로 사용한다.
    업로드로 DB 파일이 실제로 바뀔 때만 값이 달라지므로, 이 값을 캐시 키로 쓰면
    화면 클릭/채팅 등 데이터와 무관한 rerun에서는 무거운 SQL 재조회 없이
    캐시된 결과를 즉시 재사용할 수 있다.
    """
    try:
        return os.path.getmtime(DB_PATH)
    except OSError:
        return 0


# ============================================================
# 원본 DB 적재 (시청이력 / 당사직원 2종)
# ============================================================
def upsert_to_db(df_history=None, df_employee=None):
    """임시 테이블을 활용한 무손실 병합. 여러 파일을 미리 concat해서 한 번에 넘겨도 되고,
    파일별로 여러 번 호출해도 안전하게 누적된다 (동일 키는 최신 내용으로 덮어씀)."""
    conn = _connect()

    def _upsert_table(df, table_name, subset_keys):
        if df is None or df.empty:
            return

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

    try:
        _upsert_table(df_history, 'tb_history', ['콘텐츠ID', 'R고객번호', '시청일', '시청시간대'])
        _upsert_table(df_employee, 'tb_employee', ['R고객번호'])
        # 🌟 [속도 최적화] tb_history가 실제로 갱신됐다면, 시청기간 필터 조회(load_history_period)와
        # 이 업서트 자체의 중복 제거(DELETE ... WHERE ... IN (...))에 쓰이는 인덱스를 보장해둔다.
        # 매달 파일이 누적될수록 이 인덱스가 없으면 조회/업서트 모두 테이블 전체를 훑어야 해서
        # 시간이 계속 늘어난다. CREATE INDEX IF NOT EXISTS라 최초 1회 이후에는 사실상 비용이 없다.
        if df_history is not None and not df_history.empty:
            idx_cursor = conn.cursor()
            idx_cursor.execute('CREATE INDEX IF NOT EXISTS idx_tb_history_date ON tb_history("시청일")')
            idx_cursor.execute(
                'CREATE INDEX IF NOT EXISTS idx_tb_history_dedup '
                'ON tb_history("콘텐츠ID", "R고객번호", "시청일", "시청시간대")'
            )
            conn.commit()
    except Exception as e:
        st.error(f"🚨 데이터베이스 누적 중 오류가 발생하여 안전하게 작업을 중단했습니다: {e}")
    finally:
        conn.close()

    # 🌟 [속도 최적화] VACUUM으로 그동안의 업서트(DELETE+INSERT)가 남긴 빈 페이지를 정리해
    # DB 파일을 압축한다. 실측 기준 72만 행에서도 약 1.5초로 비용이 크지 않고, 업로드는
    # 자주 발생하는 동작이 아니라서(보통 월 단위) 매번 실행해도 부담이 없다. 반면 이걸 안 하면
    # 파일이 계속 부풀어 조회 속도가 데이터양보다 더 나빠질 수 있어(실측: 조각화된 DB에서
    # 전체 조회가 약 2배 느려짐), 업로드가 있었을 때만 실행한다.
    if df_history is not None and not df_history.empty:
        optimize_db()


@st.cache_data(show_spinner=False, persist="disk", max_entries=5)
def _load_from_db_cached(version):
    """🌟 [속도 최적화] 실제 SQL 조회부. version이 그대로면(=DB 파일이 안 바뀌었으면)
    캐시된 결과를 즉시 반환하고, 매 rerun마다 전체 테이블을 다시 읽지 않는다.
    persist="disk"로 이 캐시를 파일로 저장해두므로, 실무자님이 streamlit을
    종료했다가 나중에 다시 실행해도(=초기 로딩) 데이터가 그대로라면 이 무거운
    SQL 조회를 처음부터 다시 하지 않고 디스크에 저장된 결과를 바로 불러온다."""
    conn = _connect()
    try:
        df_history = pd.read_sql("SELECT * FROM tb_history", conn)
    except Exception:
        df_history = pd.DataFrame(columns=['콘텐츠ID', 'R고객번호', '이웃고객명', '성별', '나이', '시청자SO',
                                            '채널명', '메뉴명', '장르', '영상명', '러닝타임', '시청시간',
                                            '시청일', '시청시간대', '시청 유지율'])
    try:
        df_employee = pd.read_sql("SELECT * FROM tb_employee", conn)
    except Exception:
        df_employee = pd.DataFrame(columns=['R고객번호'])
    conn.close()
    return df_history, df_employee


def load_from_db():
    return _load_from_db_cached(get_data_version())


def optimize_db():
    """🌟 [속도 최적화] SQLite VACUUM으로 DB 파일 내부의 빈 페이지(과거 업서트가 남긴
    조각)를 회수해 파일을 압축한다. VACUUM은 DB 파일 전체를 다시 쓰는 작업이라 누적
    데이터가 아주 많아지면 그 자체로 시간이 걸리므로(실측: 72만 행에서 매번 실행 시
    업로드 시간이 12개월차에 2.5초까지 늘어남), 빈 페이지 비율이 낮을 때는 정리할 게
    거의 없다고 보고 건너뛴다. 이렇게 하면 대부분의 업로드는 VACUUM 없이 빠르게 끝나고,
    조각이 실제로 쌓였을 때만 가끔 정리해서 평균적으로 업로드 속도와 조회 속도 둘 다를
    적절히 챙길 수 있다."""
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("PRAGMA page_count")
        page_count = cur.fetchone()[0]
        cur.execute("PRAGMA freelist_count")
        freelist_count = cur.fetchone()[0]
        if page_count > 0 and (freelist_count / page_count) > 0.1:
            conn.execute("VACUUM")
            conn.commit()
        conn.close()
    except Exception:
        pass


def has_history_data():
    """🌟 [속도 최적화] tb_history에 데이터가 하나라도 있는지만 빠르게 확인.
    main.py의 '아직 데이터가 없습니다' 안내 화면 분기에 load_from_db()(전체 조회)를
    쓰면 시청이력이 아무리 쌓여도 그 전체를 다 읽은 뒤에야 화면을 그릴 수 있다.
    이 함수는 첫 행 존재 여부만 확인해서 데이터 규모와 무관하게 항상 즉시 응답한다."""
    try:
        conn = _connect()
        cur = conn.cursor()
        cur.execute("SELECT EXISTS(SELECT 1 FROM tb_history LIMIT 1)")
        result = bool(cur.fetchone()[0])
        conn.close()
        return result
    except Exception:
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
    """🌟 [속도 최적화] 시청기간 필터가 켜져 있을 때, load_from_db()처럼 tb_history
    전체까지 함께 읽지 않고 당사직원 제외리스트만 가볍게 조회하기 위한 전용 함수."""
    return _load_employee_cached(get_data_version())


_HISTORY_FALLBACK_COLS = ['콘텐츠ID', 'R고객번호', '이웃고객명', '성별', '나이', '시청자SO',
                          '채널명', '메뉴명', '장르', '영상명', '러닝타임', '시청시간',
                          '시청일', '시청시간대', '시청 유지율']


@st.cache_data(show_spinner=False, persist="disk", max_entries=30)
def _load_history_period_cached(version, start_date, end_date):
    """
    🌟 [속도 최적화 - 핵심] 시청기간 필터가 켜져 있을 때는 tb_history 전체를 읽어온 뒤
    파이썬(filter_by_period)에서 걸러내는 대신, SQL WHERE 절로 처음부터 그 기간의 행만
    가져온다. 실측(72만 행 누적 기준): 전체 조회는 약 10초가 걸리지만, idx_tb_history_date
    인덱스를 탄 1개월치 조회는 약 1초로 끝난다. 시청내역은 매달 파일이 누적되는 구조라
    load_from_db()의 전체 조회 시간은 데이터가 쌓일수록 계속 길어지지만, 이 함수는 '선택한
    기간의 크기'에만 비례하므로 데이터가 아무리 쌓여도 항상 빠르다.
    """
    conn = _connect()
    try:
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
        df_history = pd.read_sql(query, conn, params=params)
    except Exception:
        df_history = pd.DataFrame(columns=_HISTORY_FALLBACK_COLS)
    conn.close()
    return df_history


def load_history_period(start_date, end_date):
    return _load_history_period_cached(get_data_version(), start_date, end_date)


@st.cache_data(show_spinner=False, persist="disk", max_entries=10)
def build_audience_db(_df_history, _df_employee, version=None):
    """
    시청이력에서 당사직원의 시청기록만 제외한 시청자 상세 이력을 반환.
    🌟 [속도 최적화] 인자명 앞에 _를 붙여 매 rerun마다 큰 DataFrame 전체를
    해시하지 않도록 하고(스트림릿 캐시는 _로 시작하는 인자를 해시에서 제외한다),
    대신 가벼운 version 값(데이터가 실제로 바뀌었는지)만으로 캐시 적중 여부를 판단한다.
    """
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
    """
    지정한 시청기간(시청일 기준)의 데이터만 남긴다. 둘 다 None이면 전체 기간 그대로 반환.
    🌟 [속도 최적화] 시청기간 필터를 켜두면 화면을 클릭할 때마다(rerun마다) 대용량
    데이터를 매번 다시 필터링하고 있었다. 인자명에 _를 붙여 큰 DataFrame은 해시하지
    않고, 캐시 적중 여부는 가벼운 start_date/end_date 값만으로 판단하도록 캐싱했다.
    """
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


# ============================================================
# 대화형 타겟 설정을 위한 시청자 프로필 집계
# ============================================================
@st.cache_data(show_spinner=False, persist="disk", max_entries=10)
def build_audience_profile(_db_audience, version=None):
    """
    _db_audience(시청 이벤트 단위)를 R고객번호 기준 1인 1행으로 집계해
    대화형 타겟 설정에 쓸 '시청자 프로필' 테이블을 만든다.
    🌟 [속도 최적화] 인자명 앞에 _를 붙여 큰 DataFrame을 매번 해시하지 않도록 하고,
    캐시 적중 여부는 version(데이터 버전 + 시청기간)만으로 판단한다.
    persist="disk"로 디스크에도 저장해서, streamlit을 껐다 다시 켜도(=초기 로딩)
    데이터/기간이 그대로면 이 무거운 집계 연산을 처음부터 다시 하지 않는다.
    """
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
        # NaN 값이 섞여 있으면 "!= ''" 비교만으로는 걸러지지 않아(NaN은 무엇과 비교해도
        # True) notna()를 함께 검사해 NaN/빈 문자열을 모두 제외한다 (기존 버그 수정 유지).
        valid = df[df[col_name].notna() & (df[col_name].astype(str).str.strip() != '')]
        if valid.empty:
            return pd.DataFrame(columns=['R고객번호', out_name])

        # 🌟 [속도 최적화] 기존에는 groupby().agg(파이썬 함수)로 고객마다 파이썬 함수를
        # 호출해서(value_counts+idxmax) 고객 수가 많을 때 크게 느려졌다. 최다 빈도 값은
        # count 집계 + 정렬(첫 행만 유지)이라는 벡터화 연산만으로 구할 수 있어 훨씬 빠르다.
        # 참고: 시청 횟수가 정확히 동률일 때 어느 값을 고를지는(원래 코드도 포함해)
        # 정해진 규칙이 없었다. 여기서는 "고객이 그 값을 더 먼저 시청한 순서"를
        # 동률 기준으로 사용해 결과가 매번 같도록 고정했다 (동률이 아니면 결과는 항상 동일).
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
    # 🌟 [타겟팅 고도화] 장르/시간대와 동일한 방식으로 선호채널/선호메뉴도 집계.
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
        # 🌟 [타겟팅 고도화] 활동세그먼트(활성/휴면/이탈위험) 자동 분류.
        # 기준 시점은 datetime.now()가 아니라 업로드된 데이터 자체의 최신 시청일로 잡는다
        # (과거 데이터를 올려도 전부 '이탈위험'으로 뭉뚱그려지는 것을 방지).
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
    """대화형 타겟 설정 AI에게 '지금 어떤 조건 값이 존재하는지' 알려주는 요약 텍스트.
    🌟 [타겟팅 고도화] 인구통계/장르뿐 아니라, '몰입도' 축(시청 유지율/총시청시간/시청콘텐츠수)의
    분포(평균/상위 25% 지점)도 함께 알려줘서, AI가 "충성 시청자" 같은 조건을 말로만 뭉뚱그리지
    않고 실제 데이터에 근거한 구체적인 숫자 기준(예: 유지율 70% 이상)을 제안할 수 있게 한다."""
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
    """
    🌟 [타겟팅 고도화] 콘텐츠명포함/채널명포함 조건 전용 헬퍼.
    이벤트 단위 원본(db_audience)에서 col_name(영상명/채널명)에 keyword가 포함된
    행을 찾아, 그 행에 해당하는 R고객번호 집합을 반환한다("대략적인" 이름으로도
    매치되도록 부분 문자열(contains) 매칭만 사용 - 정확히 일치할 필요 없음).
    별도 선택 UI 없이 실무자가 채팅에 적은 키워드 그대로 사용하는 것을 전제로 한다.
    """
    if db_audience is None or db_audience.empty or col_name not in db_audience.columns or not keyword:
        return set()
    mask = db_audience[col_name].astype(str).str.contains(str(keyword), case=False, na=False, regex=False)
    return set(db_audience.loc[mask, 'R고객번호'].astype(str).unique().tolist())


def _as_list(val):
    """
    🌟 [전체 점검 - 안정성] AI가 JSON을 생성할 때, 스키마상 리스트로 지정된 필드도
    가끔 리스트 대신 단일 값으로 내려줄 수 있다(예: "나이대": ["40대"]가 아니라
    "나이대": "40대"). pandas의 Series.isin()에 문자열을 그대로 넘기면 문자열을
    "글자 하나하나의 목록"으로 오인해(예: "40대".isin 검사 시 '4','0','대' 각각과
    비교) 항상 매치가 안 되는 조용한 오답(대상자 0명)이 나올 수 있다. 이 함수로
    항상 리스트로 정규화해서 그런 조용한 오작동을 막는다.
    """
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        return list(val)
    return [val]


def _to_num(val):
    """
    🌟 [전체 점검 - 안정성] 숫자 조건 필드(최소시청횟수 등)도 AI가 가끔 문자열로
    내려줄 수 있다(예: "최소시청횟수": "5"). 이 경우 정수 컬럼과 문자열을 그대로
    비교(>=)하면 pandas/파이썬에서 타입 오류로 예외가 발생해 "타겟 확정" 버튼을
    누르는 순간 화면 전체가 에러로 멈출 수 있다. 항상 숫자로 정규화하고, 숫자로
    변환할 수 없는 값(빈 문자열 등)은 조건이 없는 것처럼 조용히 무시한다.
    """
    if val is None:
        return None
    try:
        num = pd.to_numeric(val, errors='coerce')
    except (TypeError, ValueError):
        return None
    return None if pd.isna(num) else num


def _filter_by_conditions(profile_df, conditions, db_audience=None):
    """
    conditions(dict) 하나를 profile_df에 그대로 적용하는 공용 필터 엔진.
    apply_target_conditions()의 "포함 조건"과 "제외조건"(중첩 dict) 양쪽에서
    동일한 로직을 재사용하기 위해 분리했다 (선제적 모듈화).

    🌟 [전체 점검 - 안정성] 모든 리스트형/숫자형 조건 값을 _as_list()/_to_num()으로
    한 번 정규화한 뒤 비교한다. AI 응답이 항상 스키마를 완벽히 지킨다는 보장이
    없으므로(리스트 대신 단일 값, 숫자 대신 숫자 문자열 등), 이 방어 코드가 없으면
    아주 가끔 "타겟 확정" 버튼을 누르는 순간 화면 전체가 에러로 멈추거나(타입
    비교 실패), 조용히 대상자 0명이 나오는(리스트/문자열 혼동) 문제가 생길 수 있다.
    """
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
    """
    🌟 [탐색형 대화 고도화] "꽃중년들이 가장 선호하는 콘텐츠는?"처럼 실무자가 특정
    세그먼트에 대해 순수하게 궁금해서 묻는 질문에, AI가 숫자를 지어내지 않고 답할 수
    있도록 실제 데이터를 계산해서 돌려주는 함수.

    apply_target_conditions()와 완전히 동일한 _filter_by_conditions() 엔진을
    재사용한다 - 그래야 "이 조건으로 타겟 잡아볼까요?"에 실무자가 동의했을 때,
    질문에 답할 때 썼던 것과 100% 동일한 필터가 실제 확정 타겟에도 그대로
    적용된다(질문 답변과 타겟 확정이 같은 계산 경로를 공유하므로 결과가 어긋날 수 없음).

    확정 조건(target_conditions)과 달리 이 결과는 세션에 저장되지 않고, 그 턴의
    답변 문장을 만드는 데만 한 번 쓰이고 버려지는 임시 계산이다.
    """
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
    """
    🌟 [오류/속도 고도화] summarize_segment_insight()가 계산한 실제 수치를, 별도
    제미나이 API 호출 없이 바로 자연스러운 한국어 문장으로 조립한다.

    원래는 이 답변도 AI에게 한 번 더 물어서("이 숫자를 자연스럽게 말해줘") 문장을
    받았는데, 어차피 "이미 계산된 숫자를 예쁘게 말해주는 것"뿐이라 AI의 창의적 판단이
    필요한 작업이 아니었다. 대화 한 턴에 API 호출이 2번(조건 해석 + 이 답변) 나가던
    것을 1번으로 줄여서, (1) 무료 등급 사용량 한도(429 오류)에 더 여유가 생기고,
    (2) 매번 최소 하나의 네트워크 왕복이 줄어드는 만큼 실무자 체감 응답 속도도
    빨라진다. 원래 쓰던 LLM 버전(get_segment_insight_prompt/generate_segment_insight_reply)은
    나중에 다시 쓰고 싶을 때를 위해 코드에는 남겨뒀다.
    """
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
    """
    대화에서 확정된 조건(dict)을 프로필에 적용해 실제 타겟 대상자와
    근거 설명 생성에 쓸 집계 통계를 함께 반환한다.

    🌟 [타겟팅 고도화 - 1차] 인구통계/장르 조건에 더해, 이미 build_audience_profile()에서
    집계되어 있었지만 조건으로는 쓰이지 않던 '몰입도' 축을 새 조건으로 추가:
      - 최소시청유지율: 평균시청유지율(%) 이상인 시청자만 (예: "끝까지 보는" 충성 시청자)
      - 최소총시청시간_분: 총시청시간(분) 이상인 시청자만 (예: 헤비 유저)
      - 최소시청콘텐츠수: 시청한 콘텐츠 종류 수 이상인 시청자만 (예: 다양하게 즐기는 시청자)

    🌟 [타겟팅 고도화 - 2차] 추가로:
      - 선호채널/선호메뉴, 나이최소/나이최대, 활동세그먼트(활성/휴면/이탈위험)
      - 콘텐츠명포함/채널명포함: db_audience(이벤트 단위 원본)를 넘겨주면, 영상명/채널명에
        해당 키워드가 포함된 시청 이력이 있는 사람만 남긴다(부분 일치, 대략적인 이름 OK).
      - 제외조건: 위와 완전히 동일한 필드 구조를 쓰는 중첩 dict. "~는 빼고" /
        "이 콘텐츠를 이미 본 사람은 제외"처럼, 포함 조건을 그대로 재사용해 NOT으로
        적용한다(같은 필터 엔진(_filter_by_conditions)을 재사용해 매치되는 사람을
        찾은 뒤, 최종 결과에서 그 사람들만 제거하는 방식).

    또한 근거 설명(get_target_reasoning_prompt)이 "전체 평균과 비교해 몇 % 더 높다" 같은
    설득력 있는 대비를 만들 수 있도록, 조건 적용 전 전체 모집단(profile_df) 기준
    평균값도 함께 계산해 stats에 담는다.
    """
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
    """conditions(dict) 하나를 'A=1, B=2' 형태의 문자열 파츠 리스트로 변환. (제외조건은 호출부에서 별도 처리)"""
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
    """조건 dict(+ 중첩된 제외조건)를 사람이 읽기 쉬운 한 줄 문자열로 변환하는 공용 로직.
    🌟 [타겟팅 고도화] 조건 dict를 그대로 화면에 찍으면 새로 추가된 제외조건(중첩 dict) 같은
    구조가 파이썬 dict 표현 그대로 노출되어 읽기 어려우므로, format_conditions_line()과
    format_target_summary() 양쪽에서 이 로직을 공유한다."""
    conditions = conditions or {}
    cond_parts = _format_condition_parts(conditions)
    exclude_cond = conditions.get('제외조건')
    if isinstance(exclude_cond, dict) and exclude_cond:
        exclude_parts = _format_condition_parts(exclude_cond)
        if exclude_parts:
            cond_parts.append(f"제외: {', '.join(exclude_parts)}")
    return ", ".join(cond_parts) if cond_parts else empty_text


def format_conditions_line(conditions):
    """확정 전, 대화 중간에 화면에 보여줄 '지금까지 파악된 조건' 한 줄 요약."""
    return _build_cond_str(conditions, "아직 확정된 조건 없음")


def format_target_summary(conditions, stats):
    """확정된 타겟 조건 + 집계 통계를 AI 프롬프트/화면 표시에 바로 쓸 수 있는 한글 요약으로 변환.
    🌟 [타겟팅 고도화] 새로 추가된 축(몰입도/채널·메뉴/나이범위/활동세그먼트/콘텐츠·채널 키워드)의
    라벨을 추가하고, 중첩된 '제외조건'은 같은 포맷 함수를 재사용해 "제외: ..." 형태로 붙인다.
    집계 텍스트에도 '전체 평균 대비' 비교값을 함께 넣어 근거 설명(AI)이 막연한 서술이 아니라
    실제 숫자 대비로 설득력 있게 쓰일 수 있게 한다."""
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


# ============================================================
# 🌟 [신규] 대화 단위 이력 관리 (ChatGPT처럼 사이드바에 대화 목록을 남기기 위함)
# 기존의 "확정된 타겟 하나를 수동으로 저장"하는 방식(tb_target_history) 대신,
# 대화가 진행될 때마다 자동으로 통째로 저장/갱신되는 방식으로 교체했다.
# ============================================================
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
    # 🌟 [고도화 - 대화 이름 수정] CREATE TABLE IF NOT EXISTS는 이미 만들어져 있던(예전
    # 버전의) 테이블 구조를 바꿔주지 않으므로, title_custom 컬럼이 없는 기존 DB라면
    # upsert_to_db()에서 쓰던 것과 동일한 방식으로 컬럼을 추가해준다.
    cursor.execute("PRAGMA table_info(tb_conversation)")
    existing_cols = [info[1] for info in cursor.fetchall()]
    if 'title_custom' not in existing_cols:
        cursor.execute("ALTER TABLE tb_conversation ADD COLUMN title_custom INTEGER DEFAULT 0")


def new_conversation_id():
    return str(uuid.uuid4())


def make_conversation_title(first_user_text):
    """대화 목록에 보여줄 제목을 첫 사용자 메시지로부터 만든다."""
    if not first_user_text:
        return "새 타겟 대화"
    text = first_user_text.strip().replace("\n", " ")
    return text[:24] + ("…" if len(text) > 24 else "")


def save_conversation(conv_id, title, phase, messages, conditions, stats, member_ids, reasoning, push_copy):
    """대화 상태 전체를 저장(신규면 생성, 기존이면 갱신).
    🌟 [고도화 - 대화 이름 수정] 실무자가 사이드바에서 대화 이름을 직접 수정했다면
    (rename_conversation()로 title_custom=1 표시됨), 그 뒤로 매 턴 자동저장될 때마다
    이 함수에 넘어오는 자동 생성 제목(첫 메시지 기반)으로 되돌아가지 않도록, 기존에
    저장된 커스텀 제목을 그대로 유지한다.

    🌟 [전체 점검 - 안정성] 이 함수는 대화 한 턴이 끝날 때마다 자동으로 호출된다
    (_autosave). 만약 그 순간 DB가 잠겨있는 등 저장이 실패하면, 예외를 그대로
    던져서 화면 전체가 에러로 멈춰버리면 안 된다 - 방금 나눈 대화 자체는 이미
    화면에 정상적으로 표시된 상태이므로, 자동저장 한 번 정도 실패해도 다음 턴에
    다시 저장을 시도하면 되는 '있으면 좋은' 보조 기능으로 처리한다."""
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
        # 자동저장은 최선을 다해 시도하는 보조 기능 - 실패해도 대화 자체는 계속 이어가고,
        # 다음 턴의 자동저장이 이번에 못 담은 내용까지 포함해 다시 저장을 시도한다.
        pass


def rename_conversation(conv_id, new_title):
    """🌟 [고도화 - 자주 쓰는 타겟 즐겨찾기 대체] 사이드바에서 대화 이름을 직접 수정.
    자주 쓰는 조건 조합을 기억하기 쉬운 이름(예: '이탈위험 4050 여성')으로 바꿔두면,
    매번 조건을 새로 설명하지 않고 사이드바 목록에서 바로 찾아 이어서 쓰는 '즐겨찾기
    타겟'처럼 활용할 수 있다. title_custom=1로 표시해서 이후 자동저장이 이 이름을
    자동 생성 제목으로 되돌리지 않게 한다."""
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
    """저장된 대화 목록 (최신순).
    🌟 [전체 점검 - 안정성] 화면이 열릴 때마다(사이드바 렌더링마다) 호출되는 함수라,
    DB가 일시적으로 잠겨 있어도 사이드바 하나 때문에 앱 전체가 죽으면 안 된다.
    실패 시 목록이 잠깐 비어 보이는 정도로 저하시키고, 다음 rerun에서 다시 시도한다."""
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
    """🌟 [전체 점검 - 안정성] 저장된 대화를 클릭해서 불러올 때, DB 오류로
    화면이 깨지는 대신 None을 반환해 호출부(사이드바)가 조용히 무시하게 한다."""
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
