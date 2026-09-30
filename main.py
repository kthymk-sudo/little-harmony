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
from ui.sidebar import render_sidebar, SYSTEM_NAME
from ui.chat_app import render_chat_app
from ui.analysis_chat import render_analysis_chat
from ui.report_chat import render_report_chat
from ui.chat_styles import inject_chat_css

st.set_page_config(page_title=SYSTEM_NAME, layout="wide")
init_session_state()

inject_chat_css()  # 사이드바 스타일은 데이터가 없는 안내 화면에서도 적용돼야 해서 여기서 한 번
render_sidebar()

# 대화 저장이 실패했으면(ui/*의 autosave가 표시) 한 번 알려준다 - 저장 직후 rerun되므로 여기서 띄운다
if st.session_state.pop('save_failed', False):
    st.toast("대화 저장에 실패했어요. 중요한 내용은 따로 복사해 두세요.", icon=":material/warning:")

# 🌟 [속도 최적화] 데이터 존재 여부만 확인하는 가벼운 쿼리로 안내 화면을 먼저 분기한다
# (데이터가 아무리 많이 쌓여 있어도 이 확인 자체는 항상 즉시 끝난다).
if not has_history_data():
    st.title(SYSTEM_NAME)
    st.warning("아직 적재된 시청 데이터가 없습니다. 왼쪽 사이드바의 '데이터 업로드'에서 파일을 올려주세요.")
    st.stop()

data_version = get_data_version()
# 🌟 [대화로 기간 설정] 사이드바 시청기간 설정을 없앴다 - 항상 적재된 전체 기간을 불러오고,
# 분석/보고서는 대화에서 정한 기간(분석 스펙의 "기간")을 그 집계에만 적용한다.
db_audience = load_history_period()
profile_df = build_audience_profile(db_audience, version=data_version)

if st.session_state.active_feature == 'analysis':
    render_analysis_chat(profile_df, db_audience, load_content())
elif st.session_state.active_feature == 'report':
    render_report_chat(profile_df, db_audience, load_content())
else:
    render_chat_app(profile_df, db_audience, load_content())
