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
