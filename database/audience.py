# database/audience.py
import re
import numpy as np
import pandas as pd
import streamlit as st
from config import ACTIVE_SEGMENT_DAYS, DORMANT_SEGMENT_DAYS
from utils.data_cleaner import standardize_columns


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
    """
    🌟 [버그 수정 - 조건 넓히기 시 대상자 감소] 콘텐츠명포함/채널명포함은 문자열 하나만
    받는 필드라, 실무자가 "캠핑 또는 낚시도 넣어줘"처럼 넓혀달라고 하면 AI가 그 의도를
    "캠핑|낚시"처럼 파이프로 합쳐서 표현하는 경우가 있었다. 그런데 str.contains()를
    regex=False로 쓰고 있어서 그 파이프가 OR 연산자가 아니라 글자 그대로 취급됐고,
    결과적으로 "캠핑|낚시"라는 문자열이 통째로 들어있는 영상명만 찾는 꼴이 되어
    거의 아무도 안 걸렸다(조건을 넓혔는데 오히려 대상자가 줄어드는 원인이었다).
    이제 keyword를 _as_list()로 먼저 여러 값으로 쪼갠 뒤, 각 값을 여전히 안전한
    리터럴(regex=False) 부분일치로 검사하고 결과를 OR로 합친다 - 정규식을 직접 쓰지
    않으므로 AI가 특수문자가 섞인 값을 보내도 예상치 못한 패턴으로 깨질 위험이 없다.
    """
    if db_audience is None or db_audience.empty or col_name not in db_audience.columns or not keyword:
        return set()
    terms = _as_list(keyword)
    if not terms:
        return set()
    mask = pd.Series(False, index=db_audience.index)
    for term in terms:
        mask = mask | db_audience[col_name].astype(str).str.contains(str(term), case=False, na=False, regex=False)
    return set(db_audience.loc[mask, 'R고객번호'].astype(str).unique().tolist())


def _as_list(val):
    if val is None:
        return []
    if isinstance(val, (list, tuple, set)):
        return list(val)
    if isinstance(val, str):
        parts = [p.strip() for p in re.split(r'[,/|]|또는|혹은', val) if p.strip()]
        return parts if parts else [val]
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
