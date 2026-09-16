# main.py
# ============================================================
# 하모니펄스(harmony_pulse) 진입점.
# 🌟 [개편] 4개 탭 구조를 버리고, ChatGPT/Gemini 스타일의 단일 대화 화면 +
# 사이드바(새 대화/대화 목록/시청기간 설정/데이터 업로드) 구조로 변경.
# 🌟 [속도 최적화] db_manager의 캐시 함수들에 가벼운 version 값을 넘겨서,
# 화면 클릭 등 데이터와 무관한 rerun에서는 무거운 재계산 없이 캐시를 그대로 쓴다.
#
# 🌟 [속도 최적화 - 2차, 대용량 누적 대응] 시청내역은 매달 파일이 쌓이는 구조라, 사용
# 기간이 길어질수록 tb_history 전체 조회(load_from_db)가 계속 느려진다(실측: 72만 행
# 기준 약 10초). 시청기간 필터가 켜져 있을 때는 SQL 단계에서부터 그 기간만 조회하는
# load_history_period()를 대신 써서, 로딩 시간이 '누적된 전체 데이터양'이 아니라
# '선택한 기간의 크기'에 비례하도록 바꿨다(같은 조건에서 약 1초로 단축).
# ============================================================
import streamlit as st

from config import init_session_state
from database.db_manager import (
    load_from_db, load_history_period, load_employee_list, has_history_data,
    build_audience_db, build_audience_profile, filter_by_period, get_data_version,
)
from ui.sidebar import render_sidebar
from ui.chat_app import render_chat_app

st.set_page_config(page_title="레T-고객 타겟팅&카소 자동화", layout="wide")
init_session_state()

render_sidebar()

# 🌟 [속도 최적화] 데이터 존재 여부만 확인하는 가벼운 쿼리로 안내 화면을 먼저 분기한다
# (데이터가 아무리 많이 쌓여 있어도 이 확인 자체는 항상 즉시 끝난다).
if not has_history_data():
    st.title("레T-고객 타겟팅&카소 자동화")
    st.warning("아직 적재된 시청 데이터가 없습니다. 왼쪽 사이드바의 '📂 데이터 업로드'에서 파일을 올려주세요.")
    st.stop()

data_version = get_data_version()
period_start = st.session_state.get('period_start')
period_end = st.session_state.get('period_end')

if period_start or period_end:
    # 시청기간이 지정된 경우: SQL 단계에서부터 그 기간만 읽어와 누적 데이터양과 무관하게 빠르게 로딩.
    df_history = load_history_period(period_start, period_end)
    df_employee = load_employee_list()
else:
    df_history, df_employee = load_from_db()

# 🌟 df_history 자체가 이미 기간에 따라 달라지므로(전체 vs 특정 기간), build_audience_db와
# build_audience_profile의 캐시 키에도 데이터 버전뿐 아니라 기간을 함께 넣어야 서로 다른
# 기간 조회 결과가 캐시에서 뒤섞이지 않는다.
period_version = f"{data_version}_{period_start}_{period_end}"
db_audience = build_audience_db(df_history, df_employee, version=period_version)
# 이미 SQL 단계에서 기간이 걸러졌다면 여기서는 사실상 통과만 시키는 저비용 안전장치로 남는다
# (필터 미적용 상태의 df_history가 넘어오는 경우에도 항상 정확한 결과를 보장).
db_audience = filter_by_period(db_audience, period_start, period_end)
profile_df = build_audience_profile(db_audience, version=period_version)

render_chat_app(profile_df, db_audience)
