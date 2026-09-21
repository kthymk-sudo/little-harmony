# database/audience.py
# ============================================================
# 🌟 [모듈화] database/db_manager.py에서 "오디언스(시청자) 집계와 타겟 조건
# 필터링" 관련 로직만 분리.
# ============================================================
import re
import numpy as np
import pandas as pd
import streamlit as st
from config import ACTIVE_SEGMENT_DAYS, DORMANT_SEGMENT_DAYS
from utils.data_cleaner import standardize_columns

# 🌟 [취향 다각도 분석] 선호장르/선호채널/선호메뉴/선호시청시간대를 "1위 하나"가
# 아니라 "1위와 충분히 근접한 항목 전부"로 잡기 위한 기준값. RATIO는 1위 시청
# 횟수 대비 비율(0.6 = 1위의 60% 이상이면 인정), MIN_COUNT는 그 항목을 최소
# 몇 번은 봐야 "선호"로 인정할지(1~2번 우연히 본 건 제외)의 절대 하한이다.
_PREFERENCE_RATIO_THRESHOLD = 0.6
_PREFERENCE_MIN_COUNT = 2


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

    def _pref_list_by_customer(col_name, out_name):
        """
        🌟 [취향 다각도 분석] 예전에는 이 함수가 고객별 최빈값(시청 횟수 1위) 딱
        하나만 뽑았다("선호장르"가 문자열 1개). 그런데 드라마 100번, 다큐 99번을
        본 사람도 그 방식으로는 "선호장르=드라마"로만 기록되고 다큐 취향은
        통째로 사라진다 - 실무자가 지적한 대로, 1위와 거의 차이가 안 나는 항목도
        "이 사람이 실제로 좋아하는 것"으로 보는 게 자연스럽다.
        그래서 순위를 top2/top3처럼 고정 개수로 자르는 대신, "그 고객의 1위
        시청 횟수 대비 일정 비율(_PREFERENCE_RATIO_THRESHOLD) 이상 본 항목"을
        전부 선호 목록에 담는다 - 드라마 100/다큐 99처럼 박빙이면 둘 다, 드라마
        100/다큐 1처럼 격차가 크면 다큐는 자동으로 빠진다. 다만 아주 적게(1~2번)
        본 항목까지 우연히 "선호"로 잡히는 걸 막기 위해 절대 횟수 하한
        (_PREFERENCE_MIN_COUNT)도 같이 둔다 - 단, 그 고객의 1위 항목만큼은
        하한 미만이어도(그 사람이 딱 1번만 시청한 경우 등) 항상 포함시켜서,
        데이터가 적은 고객이 선호 목록 자체가 통째로 비어버리는 일은 없게 한다.
        결과 컬럼(out_name)은 예전과 달리 "리스트"를 담는다 - 이 값을 쓰는
        쪽(필터링/화면표시/집계)도 리스트를 다루도록 같이 손봐야 한다.
        """
        if col_name not in df.columns:
            return pd.DataFrame(columns=['R고객번호', out_name])
        valid = df[df[col_name].notna() & (df[col_name].astype(str).str.strip() != '')]
        if valid.empty:
            return pd.DataFrame(columns=['R고객번호', out_name])

        counts = valid.groupby(['R고객번호', col_name]).size().reset_index(name='__cnt')
        max_per_customer = counts.groupby('R고객번호')['__cnt'].transform('max')
        is_top = counts['__cnt'] >= max_per_customer  # 동률 1위는 전부 top으로 인정
        clears_bar = (counts['__cnt'] >= max_per_customer * _PREFERENCE_RATIO_THRESHOLD) & \
            (counts['__cnt'] >= _PREFERENCE_MIN_COUNT)
        qualified = counts[is_top | clears_bar].sort_values(
            ['R고객번호', '__cnt'], ascending=[True, False], kind='mergesort'
        )
        result = (
            qualified.groupby('R고객번호')[col_name]
            .apply(list)
            .reset_index()
            .rename(columns={col_name: out_name})
        )
        return result

    pref_genre = _pref_list_by_customer('장르', '선호장르')
    pref_time = _pref_list_by_customer('시청시간대', '선호시청시간대')
    pref_channel = _pref_list_by_customer('채널명', '선호채널')
    pref_menu = _pref_list_by_customer('메뉴명', '선호메뉴')

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

    profile = profile.fillna({'활동세그먼트': '미분류'})
    # 🌟 [취향 다각도 분석] 선호장르 등 4개 컬럼은 이제 "리스트"를 담으므로
    # (위 _pref_list_by_customer 참고), 일반 fillna(스칼라 값)로는 빈 값을
    # 못 채운다 - 왼쪽 조인에서 매칭이 안 돼 NaN으로 남은 고객은 빈 리스트로
    # 명시적으로 바꿔줘야 이후 필터링/집계 코드가 "리스트"라고 안전하게 가정할
    # 수 있다.
    for col in ['선호장르', '선호시청시간대', '선호채널', '선호메뉴']:
        if col in profile.columns:
            profile[col] = profile[col].apply(lambda v: v if isinstance(v, list) else [])

    return profile


def summarize_profile_context(profile_df):
    if profile_df is None or profile_df.empty:
        return "현재 집계된 시청자 프로필 데이터가 없습니다."

    gender_counts = profile_df['성별'].value_counts().to_dict() if '성별' in profile_df.columns else {}
    so_list = sorted(profile_df['시청자SO'].dropna().unique().tolist()) if '시청자SO' in profile_df.columns else []

    # 🌟 [취향 다각도 분석] 선호장르 등은 이제 고객별로 "리스트"를 담는 컬럼이라
    # (한 고객이 여러 값을 가질 수 있음) 그냥 .unique()를 부르면 "unhashable
    # type: list" 에러가 난다. 먼저 explode()로 펼쳐서 개별 값 단위로 만든 뒤
    # 유일값을 뽑아야 한다.
    def _flatten_unique(col):
        if col not in profile_df.columns:
            return []
        vals = profile_df[col].dropna()
        if vals.empty:
            return []
        exploded = vals.explode().dropna()
        return sorted([v for v in exploded.unique().tolist() if v])

    genre_list = _flatten_unique('선호장르')
    time_list = _flatten_unique('선호시청시간대')
    channel_list = _flatten_unique('선호채널')
    menu_list = _flatten_unique('선호메뉴')
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
    # 🌟 [타겟 매칭 방어] AI가 여러 값을 배열이 아니라 "캠핑, 낚시"나 "캠핑 또는 낚시"처럼
    # 사람이 읽는 문장 하나로 합쳐서 돌려주는 경우가 있다. 이 값을 그대로 [val]로 감싸면
    # isin() 비교 시 데이터의 실제 장르 값과 그 문자열 전체가 완전히 똑같아야만 매칭되는데
    # 그런 값은 실제 데이터에 없어서 전부 매칭 실패(0명에 가까움)로 이어진다. 그 결과
    # 조건을 넓혔는데도 오히려 대상자가 줄어드는 것처럼 보이는 문제가 생길 수 있어서,
    # 쉼표/슬래시/"또는"/"혹은"으로 구분된 문자열이면 여러 값으로 나눠서 인식하도록 방어한다.
    if isinstance(val, str):
        # 🌟 콘텐츠명포함/채널명포함 필드에서 AI가 "캠핑|낚시"처럼 정규식 OR처럼
        # 보이는 파이프로 여러 값을 표현하는 경우가 있어 구분자에 포함시킨다.
        parts = [p.strip() for p in re.split(r'[,/|]|또는|혹은', val) if p.strip()]
        return parts if parts else [val]
    return [val]


def _series_intersects(series, target_values):
    """
    🌟 [취향 다각도 분석] 선호장르/선호채널/선호메뉴/선호시청시간대는 이제 고객별로
    "리스트"를 담는다(_pref_list_by_customer 참고). 예전처럼 .isin()을 그대로
    쓰면 "그 컬럼의 값이 target_values 중 하나와 정확히 같은가"만 보는데, 컬럼
    값 자체가 리스트라 어차피 target_values의 원소들과 절대 같아질 수 없어
    전부 매칭 실패한다. 대신 "그 고객의 선호 리스트와 실무자가 요청한 값
    목록이 하나라도 겹치는가"로 바꿔야 한다 - 드라마/다큐를 둘 다 선호하는
    고객은 "드라마 좋아하는 사람" 조건에도, "다큐 좋아하는 사람" 조건에도
    똑같이 매칭되는 게 맞다.
    """
    target_set = {str(v) for v in target_values}

    def _has_overlap(prefs):
        if prefs is None:
            return False
        if isinstance(prefs, (list, tuple, set)):
            return bool({str(p) for p in prefs} & target_set)
        # 혹시 과거 데이터/다른 경로로 아직 스칼라 값이 들어와도 방어적으로 처리
        return str(prefs) in target_set

    return series.apply(_has_overlap)


def _parse_recent_date_condition(val, db_audience=None):
    """
    🌟 [버그 수정 - 날짜 조건 형식 불일치] '최근시청일이후'는 AI가 자유 텍스트로
    채우는 필드인데, 프롬프트에 형식 지침이 없던 시절에는 아래처럼 단순 문자열
    비교만 하고 있었다:
        df['최근시청일'] >= str(conditions['최근시청일이후'])
    데이터의 '최근시청일'은 항상 'YYYY-MM-DD' 문자열인데, 실무자가 "최근 30일
    이내" "3개월 전부터"처럼 말하면 AI가 그 표현을 그대로("30일 이내", "3개월 전")
    돌려주거나 "2026.07.01"/"2026/07/01"처럼 다른 구분자로 줄 수 있다. 이 경우
    문자열 비교가 조용히 전원 탈락(대상자 0명)으로 이어진다 - 콘텐츠명포함의
    파이프 문제와 같은 계열의 버그. 아래처럼 절대 날짜/상대 표현을 모두 안전하게
    해석하고, 무엇도 해석이 안 되면 차라리 조건을 무시한다(에러 없이 전원 탈락
    시키는 것보다, 조건 하나를 못 알아들었다고 무시하는 편이 안전하다는 원칙).
    """
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return None  # 숫자 하나만 덩그러니 온 경우는 의미가 모호해 무시

    text = str(val).strip()
    if not text:
        return None

    # 1) 절대 날짜로 바로 해석 시도 (구분자가 -, ., / 등이어도 pandas가 대부분 흡수)
    parsed = pd.to_datetime(text, errors='coerce')
    if pd.notna(parsed):
        return parsed.strftime('%Y-%m-%d')

    # 2) "숫자 + 단위(일/주/개월/달/년)" 형태의 상대적 표현 시도
    #    ("이내", "전", "이후" 등 접미사는 무시하고 숫자·단위만 사용)
    m = re.search(r'(\d+)\s*(일|주|개월|달|년)', text)
    if m:
        n = int(m.group(1))
        days_per_unit = {'일': 1, '주': 7, '개월': 30, '달': 30, '년': 365}
        offset_days = n * days_per_unit[m.group(2)]

        # 기준점은 실제 '오늘'이 아니라 데이터상 가장 최근 시청일로 삼는다
        # (activity 세그먼트 계산에 쓰는 ref_date와 같은 기준 - 업로드된 데이터가
        # 과거 시점이어도 "최근"의 의미가 어긋나지 않게 하기 위함).
        ref_date = None
        if db_audience is not None and not db_audience.empty and '시청일' in db_audience.columns:
            ref_date = pd.to_datetime(db_audience['시청일'], errors='coerce').max()
        if ref_date is None or pd.isna(ref_date):
            ref_date = pd.Timestamp.today()

        cutoff = ref_date - pd.Timedelta(days=offset_days)
        return cutoff.strftime('%Y-%m-%d')

    # 3) 둘 다 실패하면 조건을 무시 (전원 탈락시키는 것보다 안전)
    return None


# 🌟 [버그 수정 - 조건 삭제 시 필드명 불일치] "나이 조건 빼줘"를 처리할 때 AI가
# 필드명을 스키마와 정확히 똑같이("나이대") 써야만 삭제가 되는데, 대화 맥락상
# "나이", "연령대"처럼 살짝 다르게 쓰면 dict.pop()이 조용히 아무 일도 하지 않고
# 넘어간다 - 실무자 입장에서는 "빼달라고 했는데 안 빠졌다"로 보이는, 역시 같은
# 계열의 버그. 실제로 나올 법한 표현들을 정식 필드명으로 매핑해 흡수한다.
_CANONICAL_CONDITION_FIELDS = {
    '성별', '나이대', '나이최소', '나이최대', 'SO', '선호장르', '선호시청시간대',
    '선호채널', '선호메뉴', '최소시청횟수', '최근시청일이후', '최소시청유지율',
    '최소총시청시간_분', '최소시청콘텐츠수', '활동세그먼트', '콘텐츠명포함',
    '채널명포함', '제외조건', '상위N명',
}

_CONDITION_FIELD_ALIASES = {
    '나이': '나이대', '연령': '나이대', '연령대': '나이대',
    '지역': 'SO', '거주지역': 'SO', 'so지역': 'SO',
    '장르': '선호장르',
    '시간대': '선호시청시간대', '시청시간대': '선호시청시간대',
    '채널': '선호채널',
    '메뉴': '선호메뉴',
    '시청횟수': '최소시청횟수',
    '최근시청일': '최근시청일이후', '최근시청': '최근시청일이후',
    '시청유지율': '최소시청유지율', '유지율': '최소시청유지율',
    '총시청시간': '최소총시청시간_분', '시청시간': '최소총시청시간_분',
    '시청콘텐츠수': '최소시청콘텐츠수', '콘텐츠수': '최소시청콘텐츠수',
    '세그먼트': '활동세그먼트', '활동': '활동세그먼트',
    '콘텐츠명': '콘텐츠명포함', '콘텐츠': '콘텐츠명포함',
    '채널명': '채널명포함',
    '제외': '제외조건',
    '인원수': '상위N명', '인원수제한': '상위N명', '정원': '상위N명', '상위n명': '상위N명',
}

# 🌟 [상위 N명 타겟팅] "충성도 높은 상위 100명"처럼 실무자가 조건값(유지율 X%
# 이상) 대신 정확한 인원수로 타겟을 지정할 때, 어떤 지표 하나로 순위를 매길지
# 결정하는 매핑. 왼쪽은 AI가 "기준"에 쓸 수 있는 흔한 표현, 오른쪽은 실제
# profile_df 컬럼(과 "분" 단위로 바꾸려면 나눌 값)이다. 여기 없는 표현이거나
# "기준"이 비어있으면(예: 그냥 "충성도 높은") _engagement_score()로 계산한
# 복합 점수를 쓴다 - 실무자가 특정 지표를 콕 집어 말하지 않았는데 임의로 지표
# 하나만 골라버리면 "왜 하필 이 지표냐"는 오해를 살 수 있기 때문이다.
_RANKABLE_METRIC_COLUMNS = {
    '총시청시간': ('총시청시간', 60),
    '시청유지율': ('평균시청유지율', 1),
    '시청콘텐츠수': ('시청콘텐츠수', 1),
    '시청횟수': ('시청횟수', 1),
}

_RANK_METRIC_ALIASES = {
    '시청시간': '총시청시간', '시청 시간': '총시청시간',
    '유지율': '시청유지율',
    '콘텐츠수': '시청콘텐츠수', '다양성': '시청콘텐츠수',
    '횟수': '시청횟수',
}


def _normalize_rank_metric(name):
    """'상위N명'의 '기준' 값을 _RANKABLE_METRIC_COLUMNS의 정식 키로 정규화한다.
    인식하지 못하는 표현(예: '충성도', '몰입도'처럼 포괄적인 말, 또는 애초에
    비어있음)은 None을 반환해 호출부가 복합 점수로 자연스럽게 폴백하게 한다."""
    if not name:
        return None
    text = str(name).strip()
    if text in _RANKABLE_METRIC_COLUMNS:
        return text
    return _RANK_METRIC_ALIASES.get(text)


def _normalize_field_name(name):
    """'삭제할조건'에 담긴 필드명을 정식 스키마 필드명으로 정규화한다.
    정식 명칭이면 그대로, 흔히 쓸 법한 다른 표현이면 별칭 매핑으로, 어느 쪽에도
    없으면 None을 반환해 호출부가 "모르는 필드는 그냥 무시"하도록 한다."""
    if not name:
        return None
    text = str(name).strip()
    if text in _CANONICAL_CONDITION_FIELDS:
        return text
    if text.lower() == 'so':
        return 'SO'
    return _CONDITION_FIELD_ALIASES.get(text)


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
    # 🌟 [취향 다각도 분석] 선호장르/선호시청시간대/선호채널/선호메뉴는 이제 고객별로
    # 여러 값을 담는 리스트 컬럼이라(_pref_list_by_customer), .isin() 대신
    # "겹치는 게 있는지"를 보는 _series_intersects()로 필터링한다.
    if conditions.get('선호장르'):
        df = df[_series_intersects(df['선호장르'], _as_list(conditions['선호장르']))]
    if conditions.get('선호시청시간대'):
        df = df[_series_intersects(df['선호시청시간대'], _as_list(conditions['선호시청시간대']))]
    if conditions.get('선호채널') and '선호채널' in df.columns:
        df = df[_series_intersects(df['선호채널'], _as_list(conditions['선호채널']))]
    if conditions.get('선호메뉴') and '선호메뉴' in df.columns:
        df = df[_series_intersects(df['선호메뉴'], _as_list(conditions['선호메뉴']))]
    if conditions.get('활동세그먼트') and '활동세그먼트' in df.columns:
        df = df[df['활동세그먼트'].isin(_as_list(conditions['활동세그먼트']))]
    min_watch_cnt = _to_num(conditions.get('최소시청횟수'))
    if min_watch_cnt is not None:
        df = df[pd.to_numeric(df['시청횟수'], errors='coerce') >= min_watch_cnt]
    if conditions.get('최근시청일이후'):
        cutoff = _parse_recent_date_condition(conditions['최근시청일이후'], db_audience)
        if cutoff is not None:
            df = df[df['최근시청일'] >= cutoff]
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
        # 🌟 [취향 다각도 분석] 선호장르/선호채널/선호메뉴는 이제 고객별로 여러 값을
        # 담는 리스트 컬럼이다(_pref_list_by_customer). value_counts()는 리스트를
        # 셀 수 없으므로(unhashable) 먼저 explode()로 펼친 뒤 세야 한다. 한 사람이
        # 드라마/다큐를 둘 다 선호하면 두 항목 집계에 모두 1명씩 잡히는 게 맞다
        # (다각도 취향을 그대로 반영 - 인원 합계가 대상자수보다 커질 수 있음).
        if col not in df.columns:
            return []
        s = df[col].dropna()
        if s.empty:
            return []
        if len(s) > 0 and isinstance(s.iloc[0], list):
            s = s.explode().dropna()
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


def summarize_content_ranking(db_audience, profile_df=None, conditions=None, target='콘텐츠', order='인기', top_n=10):
    """
    🌟 [시청기록 자유 분석 - 콘텐츠/채널 순위] "요즘 가장 인기있는 콘텐츠 TOP10은?",
    "가장 안 보는 채널이 뭐야?"처럼 순위를 묻는 질문에 답하기 위한 집계.
    target='콘텐츠'면 '영상명', target='채널'이면 '채널명' 컬럼을 기준으로,
    "몇 명이 봤는지"(시청자수=고유 고객 수) 기준으로 순위를 매긴다 - 단순 시청
    "건수"가 아니라 "얼마나 많은 사람에게 닿았는지"가 인기를 더 정확히 나타낸다고
    판단했다(재시청 많은 소수보다 널리 본 콘텐츠를 "인기"로 보는 게 자연스러움).
    conditions가 있으면(예: "40대 여성이 가장 좋아하는") 먼저 그 조건에 맞는 고객
    ID로 원본 시청이력을 좁힌 뒤 집계한다 - 이때도 profile_df 필터링과 완전히
    동일한 _filter_by_conditions()를 그대로 재사용하므로, 조건 해석 방식이 타겟
    설정과 달라질 일이 없다.
    order='비인기'면 오름차순(적게 본 순)으로 뒤집는다 - 다만 이 데이터에 아예
    한 번도 시청되지 않은 콘텐츠/채널은 원본 시청이력 자체에 기록이 없어서
    순위에 잡힐 수 없다는 한계가 있다(그런 콘텐츠가 있는지는 이 함수로는 알 수 없음).
    """
    col_name = '영상명' if target == '콘텐츠' else '채널명'
    if db_audience is None or db_audience.empty or col_name not in db_audience.columns:
        return None

    df = db_audience
    matched_total = None
    if conditions and profile_df is not None and not profile_df.empty:
        matched = _filter_by_conditions(profile_df.copy(), conditions, db_audience)
        matched_ids = set(matched['R고객번호'].astype(str).unique().tolist())
        matched_total = len(matched_ids)
        df = df[df['R고객번호'].astype(str).isin(matched_ids)]

    if df.empty:
        return {'대상': target, '기준': order, '기준인원수': matched_total, '항목': []}

    grouped = df.groupby(col_name)['R고객번호'].nunique().reset_index(name='시청자수')
    grouped = grouped.sort_values('시청자수', ascending=(order == '비인기'), kind='mergesort')
    top = grouped.head(top_n)
    return {
        '대상': target,
        '기준': order,
        '기준인원수': matched_total,  # 조건으로 좁힌 경우 그 조건에 해당하는 고객 수 (참고용, 조건 없으면 None)
        '항목': [{'이름': str(row[col_name]), '시청자수': int(row['시청자수'])} for _, row in top.iterrows()],
    }


def format_content_ranking_reply(ranking):
    """summarize_content_ranking()의 계산 결과를 AI 호출 없이도 안전하게 문장으로
    바꾸는 확정적 폴백 - AI 호출이 실패했을 때 에러 문구 대신 이걸 대화에 남긴다."""
    if ranking is None:
        return "죄송해요, 그 조건에 맞는 시청 데이터를 찾지 못했어요. 조건을 조금 다르게 다시 말씀해주시겠어요?"
    items = ranking.get('항목') or []
    if not items:
        return "말씀하신 조건에 맞는 시청 기록을 찾지 못했어요. 조건을 조금 다르게 말씀해주시겠어요?"
    label = ranking.get('대상') or '콘텐츠'
    order_label = "비인기" if ranking.get('기준') == '비인기' else "인기"
    joined = ', '.join(f"{it['이름']}({it['시청자수']}명)" for it in items)
    return f"{order_label} {label} 순위는 {joined} 순이에요."


# 🌟 [시청기록 자유 분석 - 그룹 현황/비교] "SO별로 시청자 수 비교해줘", "나이대별
# 분포가 어때?"는 사실 같은 계산이다 - 어떤 기준으로 사람을 나눠서 그룹별 인원/비중을
# 보는 것. 그룹핑 가능한 필드를 여기서 명시적으로 정해두고(임의 컬럼명을 그대로
# 받으면 존재하지 않는 컬럼 요청 등으로 깨지기 쉬움), '나이대'처럼 실제 컬럼이 아닌
# 계산이 필요한 필드는 별도 처리한다.
_GROUPABLE_FIELD_COLUMNS = {
    '성별': '성별', 'SO': '시청자SO', '활동세그먼트': '활동세그먼트',
    '선호장르': '선호장르', '선호채널': '선호채널', '선호메뉴': '선호메뉴',
    '선호시청시간대': '선호시청시간대',
}


def _groupable_series(df, group_field):
    """group_field 이름으로 그룹핑에 쓸 Series를 만든다. '나이대'는 profile_df에
    실제 컬럼이 없고 '나이'에서 계산해야 하므로, _filter_by_conditions()의 나이대
    계산식과 완전히 동일한 방식을 재사용한다(다른 곳과 구간 정의가 어긋나면 실무자가
    혼란스러움). 나이 값이 없는 행은 "<NA>대" 같은 이상한 문자열이 되지 않도록
    명시적으로 NaN으로 남겨서, 호출부의 notna() 필터에 자연스럽게 걸러지게 한다."""
    if group_field == '나이대':
        if '나이' not in df.columns:
            return None
        age_num = pd.to_numeric(df['나이'], errors='coerce')
        band = (age_num // 10 * 10).astype('Int64').astype(str) + '대'
        return band.where(age_num.notna())
    col = _GROUPABLE_FIELD_COLUMNS.get(group_field)
    if col is None or col not in df.columns:
        return None
    return df[col]


def summarize_group_breakdown(profile_df, group_field, conditions=None, db_audience=None, top_n=10):
    """
    🌟 [시청기록 자유 분석 - 그룹 현황/비교] group_field 기준으로 고객을 나눠
    그룹별 인원수/비중을 계산한다. conditions가 있으면(예: "40대 중에서 성별
    비율은?") 먼저 그 조건으로 좁힌 뒤 그룹핑한다.
    선호장르 등은 고객별로 여러 값을 담는 리스트 컬럼이라(취향 다각도 분석)
    explode해서 집계하므로, 한 사람이 여러 그룹에 동시에 잡힐 수 있다 - 이 경우
    그룹별 인원 합계가 전체인원수보다 커질 수 있음을 감안해야 한다(취향 다각도
    분석과 동일한 원칙: 그 사람이 실제로 그 그룹들 모두에 해당하는 게 맞기 때문).
    """
    if profile_df is None or profile_df.empty or not group_field:
        return None

    df = profile_df
    if conditions:
        df = _filter_by_conditions(df.copy(), conditions, db_audience)
    if df.empty:
        return {'기준필드': group_field, '전체인원수': 0, '그룹': []}

    keys = _groupable_series(df, group_field)
    if keys is None:
        return None

    work = df.assign(__group=keys.values)
    is_list_valued = len(work) > 0 and isinstance(work['__group'].iloc[0], list)
    if is_list_valued:
        work = work.explode('__group')
    work = work[work['__group'].notna() & (work['__group'].astype(str).str.strip() != '')]
    total = len(df)  # explode로 행이 늘어날 수 있으니 분모는 원래(중복 제거 전) 대상자 수로 고정
    if work.empty:
        return {'기준필드': group_field, '전체인원수': total, '그룹': []}

    counts = work.groupby('__group')['R고객번호'].nunique().sort_values(ascending=False)
    groups = [
        {'그룹값': str(name), '인원수': int(cnt), '비중': round(cnt / total * 100, 1) if total else 0}
        for name, cnt in counts.head(top_n).items()
    ]
    return {'기준필드': group_field, '전체인원수': total, '그룹': groups}


def format_group_breakdown_reply(breakdown):
    """summarize_group_breakdown()의 계산 결과를 AI 호출 없이도 안전하게 문장으로
    바꾸는 확정적 폴백."""
    if breakdown is None:
        return "죄송해요, 그 기준으로는 데이터를 나눠보기 어려웠어요. 다른 기준으로 다시 말씀해주시겠어요?"
    groups = breakdown.get('그룹') or []
    if not groups:
        return "말씀하신 조건/기준에 맞는 데이터를 찾지 못했어요. 조건을 조금 다르게 말씀해주시겠어요?"
    parts = ', '.join(f"{g['그룹값']} {g['인원수']}명({g['비중']}%)" for g in groups)
    total = breakdown.get('전체인원수', 0)
    field_label = breakdown.get('기준필드', '')
    return f"전체 {total:,}명 기준으로 {field_label}별로는 {parts} 순이에요."


def _engagement_score(df):
    """
    🌟 [상위 N명 타겟팅] 여러 몰입도 지표(평균시청유지율/총시청시간/시청콘텐츠수/
    시청횟수)를 각각 0~1로 min-max 정규화한 뒤 동일 가중치로 평균 내어 "충성도/
    몰입도 복합 점수"를 만든다. 실무자가 특정 지표를 콕 집어 말하지 않고 그냥
    "충성도 높은 상위 100명"처럼 포괄적으로 말했을 때 쓰는 기본 순위 기준이다.
    데이터가 없는 지표는 0으로 채운다 - 중간값으로 보정하면 시청 기록이 적은
    사람의 몰입도를 실제보다 부풀리는 셈이 되기 때문이다. 정규화는 현재 순위를
    매기는 대상 집합(다른 조건으로 이미 좁혀진 df) 안에서 상대적으로 계산되므로,
    "20대 여성 중 상위 100명"처럼 다른 조건과 결합됐을 때도 그 집단 안에서
    합리적인 순위가 나온다.
    """
    cols = ['평균시청유지율', '총시청시간', '시청콘텐츠수', '시청횟수']
    scores = []
    for col in cols:
        if col not in df.columns:
            continue
        vals = pd.to_numeric(df[col], errors='coerce').fillna(0)
        span = vals.max() - vals.min()
        norm = (vals - vals.min()) / span if span > 0 else pd.Series(0.0, index=vals.index)
        scores.append(norm)
    if not scores:
        return pd.Series(0.0, index=df.index)
    return sum(scores) / len(scores)


def select_top_n_by_engagement(df, n, metric=None):
    """
    🌟 [상위 N명 타겟팅] "충성도 높은 상위 100명"처럼 조건값(유지율 X% 이상)이
    아니라 정확한 인원수로 타겟을 지정하고 싶을 때 쓴다. 예전에는 AI가 임계값을
    추측하며 여러 턴에 걸쳐 조정해도(예: 유지율 70%→60%→50%) 정확히 N명을
    맞추기 어려웠는데, 이 함수는 순위를 매겨 상위 N명을 정확히 한 번에 잘라낸다.
    metric이 _RANKABLE_METRIC_COLUMNS에 있는 지표명이면 그 지표 하나만으로,
    아니면(None 포함) _engagement_score()로 계산한 복합 점수로 순위를 매긴다.
    정렬은 안정 정렬(mergesort)이라 동점자 사이에서는 원래 순서가 유지되므로,
    "정확히 N명"을 보장하면서도 매번 같은 입력에는 같은 결과가 재현된다.
    """
    empty_result = df.iloc[0:0] if df is not None else df
    if df is None or df.empty or not n or n <= 0:
        return empty_result, None

    resolved = _RANKABLE_METRIC_COLUMNS.get(metric) if metric else None
    if resolved:
        col, divide = resolved
        if col not in df.columns:
            return empty_result, None
        score = pd.to_numeric(df[col], errors='coerce').fillna(0) / divide
    else:
        metric = None  # 인식 못한 값이 넘어왔을 수도 있으니 복합 점수임을 명확히
        score = _engagement_score(df)

    work = df.assign(__score=score.values).sort_values('__score', ascending=False, kind='mergesort')
    top = work.head(int(n))
    info = {
        '기준': metric,  # None이면 복합 점수
        '요청인원수': int(n),
        '실제인원수': len(top),
        '컷오프점수': round(float(top['__score'].iloc[-1]), 3) if len(top) else None,
    }
    return top.drop(columns='__score'), info


def apply_target_conditions(profile_df, conditions, db_audience=None):
    empty_stats = {
        '대상자수': 0, '전체시청자수': len(profile_df) if profile_df is not None else 0,
        '평균시청횟수': 0, '평균총시청시간(분)': 0, '평균시청유지율': 0, '평균시청콘텐츠수': 0,
        '전체평균시청횟수': 0, '전체평균총시청시간(분)': 0, '전체평균시청유지율': 0,
    }
    if profile_df is None or profile_df.empty or not conditions:
        return profile_df, empty_stats

    exclude_cond = conditions.get('제외조건')
    top_n_cond = conditions.get('상위N명')
    include_cond = {k: v for k, v in conditions.items() if k not in ('제외조건', '상위N명')}

    df = _filter_by_conditions(profile_df.copy(), include_cond, db_audience)

    if isinstance(exclude_cond, dict) and exclude_cond:
        excluded_df = _filter_by_conditions(profile_df.copy(), exclude_cond, db_audience)
        excluded_ids = set(excluded_df['R고객번호'].astype(str).unique().tolist())
        if excluded_ids:
            df = df[~df['R고객번호'].astype(str).isin(excluded_ids)]

    # 🌟 [상위 N명 타겟팅] 다른 조건(성별/나이대/제외조건 등)으로 이미 좁혀진
    # 뒤에 마지막 단계로 적용한다 - "20대 여성 중 상위 100명"처럼 다른 조건과
    # 결합됐을 때, 그 조건에 맞는 사람들 안에서 상위 N명을 뽑아야 자연스럽다.
    if isinstance(top_n_cond, dict) and _to_num(top_n_cond.get('인원수')):
        n = int(_to_num(top_n_cond.get('인원수')))
        metric = _normalize_rank_metric(top_n_cond.get('기준'))
        df, _ = select_top_n_by_engagement(df, n, metric)

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
