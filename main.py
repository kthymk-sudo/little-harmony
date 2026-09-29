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
#
# 🌟 [DB 폴더 일원화] data/시청_YYYY_MM.db는 저장할 때 가공(직원 제외 등)이 끝난
# 완성본이다. 여기서는 시청기간에 걸치는 달 파일만 꺼내 그대로 쓴다.
# ============================================================
import streamlit as st

from config import init_session_state
from database.db_manager import (
    load_history_period, load_content, has_history_data, build_audience_profile, get_data_version,
)
from ui.sidebar import render_sidebar
from ui.chat_app import render_chat_app
from ui.analysis_chat import render_analysis_chat
from ui.report_chat import render_report_chat

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

db_audience = load_history_period(period_start, period_end)

# 🌟 db_audience 자체가 기간에 따라 달라지므로(전체 vs 특정 기간), build_audience_profile의
# 캐시 키에도 데이터 버전뿐 아니라 기간을 함께 넣어야 서로 다른 기간 결과가 뒤섞이지 않는다.
period_version = f"{data_version}_{period_start}_{period_end}"
profile_df = build_audience_profile(db_audience, version=period_version)

if st.session_state.active_feature == 'analysis':
    render_analysis_chat(profile_df, db_audience, load_content())
elif st.session_state.active_feature == 'report':
    render_report_chat(profile_df, db_audience, load_content())
else:
    render_chat_app(profile_df, db_audience)
