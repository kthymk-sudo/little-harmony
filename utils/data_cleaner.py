# utils/data_cleaner.py
# ============================================================
# 하모니 프로젝트의 data_cleaner.py 로직을 참고해 작성.
#
# 🌟 [변경] 콘텐츠별 통계 DB(구 df1)는 더 이상 사용하지 않기로 함에 따라
# clean_df1()을 제거하고, 시청이력/직원리스트 정제 함수만 남김 (이름도
# clean_history/clean_employee로 바꿔 역할을 명확히 함).
# 'SO' -> '시청자SO' 매핑도 추가 (콘텐츠 통계 DB가 없어져 '업로더SO'와
# 헷갈릴 일이 없어졌지만, 시청자가 속한 SO라는 의미를 명확히 하기 위함).
# ============================================================
import pandas as pd
import numpy as np
import datetime
import re


def fill_zero_id(series):
    s = series.astype(str).str.strip()
    s = s.str.replace(r'\.0$', '', regex=True)
    mask = s.str.lower().isin(['nan', 'none', '<na>', '']) | series.isna()
    s = s.str.zfill(8)
    s[mask] = np.nan
    return s


def clean_id(series):
    s = series.astype(str).str.strip()
    s = s.str.replace(r'\.0$', '', regex=True)
    mask = s.str.lower().isin(['nan', 'none', '<na>', '']) | series.isna()
    s[mask] = np.nan
    return s


def clean_percent(series):
    has_percent = series.astype(str).str.contains('%', na=False)
    s = series.astype(str).str.replace('%', '', regex=False).str.replace(',', '', regex=False).str.strip()
    s = s.replace(['nan', 'none', '<na>', ''], np.nan)
    s = pd.to_numeric(s, errors='coerce').fillna(0.0)

    mask_needs_mult = (s > 0) & (s <= 1.0) & (~has_percent)
    s = np.where(mask_needs_mult, s * 100, s)
    return pd.Series(s, index=series.index)


def parse_time_to_seconds(val):
    if pd.isna(val):
        return 0.0
    if isinstance(val, (int, float)):
        return float(val)
    if isinstance(val, datetime.time):
        return float(val.hour * 3600 + val.minute * 60 + val.second)

    val_str = str(val).strip()
    if not val_str:
        return 0.0
    val_str = val_str.replace('초', '').replace('분', ':').replace('시', ':').replace(' ', '')
    parts = val_str.split(':')
    try:
        if len(parts) == 3:
            return float(parts[0]) * 3600 + float(parts[1]) * 60 + float(parts[2])
        elif len(parts) == 2:
            return float(parts[0]) * 60 + float(parts[1])
        else:
            return float(parts[0])
    except (ValueError, IndexError):
        return 0.0


def clean_date(series):
    return pd.to_datetime(series, errors='coerce').dt.strftime('%Y-%m-%d')


def standardize_columns(df):
    """원본 엑셀마다 다른 컬럼명을 하나로 표준화."""
    df.columns = df.columns.str.strip()
    rename_dict = {
        '콘텐츠 아이디': '콘텐츠ID', '콘텐츠 ID': '콘텐츠ID',
        'R 고객번호': 'R고객번호', 'R-고객번호': 'R고객번호',
        '고객번호': 'R고객번호',  # 직원 제외리스트의 '고객번호'도 R고객번호로 통일
        '시청유지율': '시청 유지율',
        '영상제목': '영상명', '프로그램명': '영상명',
        '메뉴': '메뉴명',
        '장르명': '장르',
        'SO': '시청자SO',  # 🌟 [harmony_pulse 추가] 시청자가 속한 SO임을 명확히 표기
        '노출 수': '노출수', '클릭 수': '클릭수',
        '노출 클릭율': '노출클릭율', '노출 클릭률': '노출클릭율',
    }
    for old_col, new_col in rename_dict.items():
        if old_col in df.columns and old_col != new_col:
            if new_col in df.columns:
                df[new_col] = df[new_col].fillna(df[old_col])
                df = df.drop(columns=[old_col])
            else:
                df = df.rename(columns={old_col: new_col})
    return df


def strip_strings(df):
    for c in df.columns:
        if df[c].dtype == 'object':
            df[c] = df[c].apply(lambda x: str(x).strip() if isinstance(x, str) else x)
    return df


# ==========================================
# DB 정제 함수 (시청이력 / 당사직원 제외리스트 - 2종만 사용)
# ==========================================
def clean_history(df):
    """시청내역 상세 원본을 정제."""
    df = standardize_columns(df)
    df = strip_strings(df)
    df = df.dropna(subset=['콘텐츠ID', 'R고객번호']).copy()
    df['콘텐츠ID'] = clean_id(df['콘텐츠ID'])
    df['R고객번호'] = fill_zero_id(df['R고객번호'])
    if '나이' in df.columns:
        # 원본은 "나이 모름"을 -1로 적는다 - 그대로 두면 "-10대"로 집계되고 "50세 이하" 같은
        # 조건에도 걸리므로 빈 값(미상)으로 바꾼다
        age = pd.to_numeric(df['나이'], errors='coerce')
        df['나이'] = age.where(age >= 0).astype('Int64')
    if '시청일' in df.columns:
        # 🌟 [버그 수정 - 중복 판정] 시청일을 날짜로만 남기면 같은 사람이 같은 영상을
        # 같은 시간대에 여러 번 본 기록이 중복으로 판정돼 지워졌다(26.08 기준 4,717건).
        # 초 단위 시각은 '시청일시'로 따로 보존해 중복 판정 키로 쓰고, 기존 코드가
        # 쓰는 '시청일'(YYYY-MM-DD)은 그대로 둔다.
        df['시청일시'] = pd.to_datetime(df['시청일'], errors='coerce').dt.strftime('%Y-%m-%d %H:%M:%S')
        df['시청일'] = clean_date(df['시청일'])
    if '시청 유지율' in df.columns:
        df['시청 유지율'] = clean_percent(df['시청 유지율'])
    for c in ['조회수']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c].astype(str).str.replace(',', '').str.strip(), errors='coerce').fillna(0)
    for c in ['러닝타임', '시청시간']:
        if c in df.columns:
            df[c] = df[c].apply(parse_time_to_seconds)
    return df


def finalize_history(df, employee_ids):
    """🌟 [DB 폴더 일원화] 정제된 시청이력을 "분석용 가공 완료본"으로 만든다 - 저장할 때
    한 번만 적용하고 월별 파일에 그대로 고정한다(나중에 바뀌지 않는 가공만 여기서 한다.
    업로더/제작자/삭제 여부처럼 바뀔 수 있는 콘텐츠 정보는 분석할 때 최신 통계에서 붙인다)."""
    # 당사 직원 제외 - 이 달을 가공하는 시점의 직원 명단 기준
    df = df[~df['R고객번호'].astype(str).str.strip().isin(set(employee_ids))].copy()

    # 🌟 [시청 시작 시각] 원본 시청시간대는 "시작시각 ~ 종료시각"이라 같은 14시 시작도
    # "14시 ~ 14시"/"14시 ~ 15시"로 쪼개져 120여 가지가 된다. 시작 시각만 뽑아
    # "00시"~"23시" 24개로 통일한다(두 자리로 맞춰야 문자열 정렬이 곧 시간순).
    if '시청시간대' in df.columns:
        start_hour = df['시청시간대'].astype(str).str.extract(r'^\s*(\d{1,2})\s*시', expand=False)
        df['시청시작시'] = (start_hour.str.zfill(2) + '시').where(start_hour.notna())

    # 🌟 [유지율 계산 불가 처리] 원본에서 러닝타임이 00:00:00인 콘텐츠는 몇 시간을 봤어도
    # 유지율이 0.00%로 찍혀 나온다(26.04~09 기준 755건, 콘텐츠 35개). 실제 0%가 아니라
    # "계산 불가"이므로 빈 값으로 바꿔 평균 유지율/완료율 계산에서 빠지게 한다.
    if '러닝타임' in df.columns and '시청 유지율' in df.columns:
        no_runtime = pd.to_numeric(df['러닝타임'], errors='coerce').fillna(0) <= 0
        df['시청 유지율'] = pd.to_numeric(df['시청 유지율'], errors='coerce').mask(no_runtime)
    return df


def clean_content(df):
    """콘텐츠별 통계(업로더별 시청통계목록) 원본을 정제.
    조회수/노출수 등은 추출 시점까지의 누적값 스냅샷이라, 새 파일이 오면 콘텐츠ID 기준으로 덮어쓴다."""
    # 이 파일의 SO는 '업로드한 SO'라서 standardize_columns의 SO -> 시청자SO 매핑보다 먼저 바꾼다
    df = df.rename(columns=lambda c: '업로더SO' if str(c).strip() == 'SO' else c)
    df = standardize_columns(df)
    df = strip_strings(df)
    df = df.drop(columns=[c for c in ['No', '게시자', '업로드 콘텐츠 용량'] if c in df.columns])
    df = df.dropna(subset=['콘텐츠ID']).copy()
    df['콘텐츠ID'] = clean_id(df['콘텐츠ID'])
    if '등록일' in df.columns:
        df['등록일'] = clean_date(df['등록일'])
    if '노출클릭율' in df.columns:
        df['노출클릭율'] = clean_percent(df['노출클릭율'])
    for c in ['조회수', '이용자수', '찜하기 수', '댓글 수', '노출수', '클릭수']:
        if c in df.columns:
            df[c] = pd.to_numeric(df[c].astype(str).str.replace(',', '').str.strip(), errors='coerce').fillna(0)
    for c in ['러닝타임', '평균 시청 지속시간', '누적 시청시간']:
        if c in df.columns:
            df[c] = df[c].apply(parse_time_to_seconds)
    return df


def clean_employee(df):
    """당사직원(제외 대상) 리스트 원본을 정제."""
    df = standardize_columns(df)
    df = strip_strings(df)
    if 'R고객번호' in df.columns:
        df['R고객번호'] = fill_zero_id(df['R고객번호'])
        df = df.dropna(subset=['R고객번호']).copy()
        # 제외 처리에는 고객번호만 필요 - 이름/연락처/주소 같은 개인정보는 DB에 남기지 않는다
        df = df[['R고객번호']].drop_duplicates()
    return df
