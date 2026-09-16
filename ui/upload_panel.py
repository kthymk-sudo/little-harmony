# ui/upload_panel.py
# ============================================================
# 🌟 [변경] 콘텐츠별 통계 파일 업로드는 제거 (더 이상 사용하지 않음).
# 시청내역 상세 / 당사직원 제외리스트, 이 2종류만 다루며, 각각 여러 파일을
# 한 번에 선택해 업로드할 수 있도록 accept_multiple_files=True 적용.
# 이 함수는 사이드바의 expander 안에서 호출되는 것을 전제로 st.* 를 그대로 사용한다.
# ============================================================
import pandas as pd
import streamlit as st
from utils.data_cleaner import clean_history, clean_employee
from database.db_manager import upsert_to_db


def render_upload_panel():
    st.caption("여러 파일을 한 번에 선택할 수 있어요 (예: 월별 시청이력 여러 개).")
    history_files = st.file_uploader(
        "시청내역 상세 (xlsx)", type="xlsx", accept_multiple_files=True, key="up_history"
    )
    employee_files = st.file_uploader(
        "당사직원 제외리스트 (xlsx)", type="xlsx", accept_multiple_files=True, key="up_employee"
    )

    if st.button("DB에 반영", key="btn_upload_commit"):
        if not history_files and not employee_files:
            st.warning("업로드된 파일이 없습니다.")
            return

        with st.spinner("데이터 정제 및 DB 반영 중..."):
            history_df = None
            if history_files:
                parts = [clean_history(pd.read_excel(f)) for f in history_files]
                history_df = pd.concat(parts, ignore_index=True)

            employee_df = None
            if employee_files:
                parts = [clean_employee(pd.read_excel(f)) for f in employee_files]
                employee_df = pd.concat(parts, ignore_index=True)

            upsert_to_db(history_df, employee_df)

        st.cache_data.clear()
        st.success("DB에 반영되었습니다.")
        st.rerun()
