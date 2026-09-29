# ui/sidebar.py
import streamlit as st
from config import (
    start_new_conversation, load_conversation_into_session,
    start_new_analysis_conversation, load_analysis_conversation_into_session,
    start_new_report_conversation, load_report_conversation_into_session,
)
from database.db_manager import list_conversations, load_conversation, delete_conversation, rename_conversation
from database.db_manager import get_loaded_periods
from ui.upload_panel import render_upload_panel

_FEATURES = [
    ("targeting", "🎯 타겟팅&카피"),
    ("analysis", "📊 분석"),
    ("report", "📝 보고서"),
]

# 🌟 [모듈화] 기능이 2개일 때는 if/else 하나로 충분했지만, 3개가 되면서
# 매 분기마다 if/elif가 늘어지는 대신 기능별 동작(새 대화 시작/불러오기/
# 세션의 현재 대화 id 키/기본 제목)을 표로 묶어 한 곳에서 관리한다.
_FEATURE_HANDLERS = {
    "targeting": {
        "start_new": start_new_conversation,
        "load": load_conversation_into_session,
        "conv_id_key": "current_conversation_id",
        "default_title": "새 타겟 대화",
    },
    "analysis": {
        "start_new": start_new_analysis_conversation,
        "load": load_analysis_conversation_into_session,
        "conv_id_key": "analysis_current_conversation_id",
        "default_title": "새 분석 대화",
    },
    "report": {
        "start_new": start_new_report_conversation,
        "load": load_report_conversation_into_session,
        "conv_id_key": "report_current_conversation_id",
        "default_title": "새 보고서 대화",
    },
}


def _render_feature_switcher():
    """세로 기능 메뉴. 선택된 기능에 따라 메인 화면과 아래 대화 목록이 통째로 바뀐다."""
    for feature_key, label in _FEATURES:
        is_active = (st.session_state.active_feature == feature_key)
        if st.button(label, key=f"feature_{feature_key}", width='stretch', type="primary" if is_active else "secondary"):
            if not is_active:
                st.session_state.active_feature = feature_key
                st.rerun()


def render_sidebar():
    with st.sidebar:
        # 🌟 로고를 사이드바 전체 폭에 꽉 차게 맨 위에 배치합니다.
        st.image("logo.png", use_container_width=True)

        # 🌟 [UI 개선] 'text-align: center;'를 추가하여 타이틀을 로고의 중앙에 예쁘게 정렬합니다.
        st.markdown(
            "<h3 style='text-align: center; margin-top: 10px; margin-bottom: 15px; font-size: 1.5rem; letter-spacing: -0.02em; font-weight: bold;'>"
            "고객 타겟팅&카소 자동화</h3>",
            unsafe_allow_html=True
        )

        _render_feature_switcher()
        handler = _FEATURE_HANDLERS[st.session_state.active_feature]

        st.divider()

        if st.button("＋ 새 대화", width='stretch', type="primary"):
            handler["start_new"]()
            st.rerun()

        st.divider()
        st.caption("대화 기록")

        conv_list = list_conversations(feature=st.session_state.active_feature)
        active_conv_id = st.session_state[handler["conv_id_key"]]
        if conv_list.empty:
            st.caption("아직 저장된 대화가 없습니다.")
        else:
            for _, row in conv_list.iterrows():
                is_active = (row['id'] == active_conv_id)

                if st.session_state.get('renaming_conv_id') == row['id']:
                    with st.form(key=f"rename_form_{row['id']}", border=False):
                        new_title = st.text_input(
                            "대화 이름 변경", value=row['title'] or "",
                            label_visibility="collapsed", key=f"rename_input_{row['id']}",
                        )
                        col_save, col_cancel = st.columns(2)
                        with col_save:
                            save_clicked = st.form_submit_button("저장", width='stretch', type="primary")
                        with col_cancel:
                            cancel_clicked = st.form_submit_button("취소", width='stretch')
                    if save_clicked:
                        final_title = new_title.strip() or row['title'] or handler["default_title"]
                        rename_conversation(row['id'], final_title)
                        st.session_state.pop('renaming_conv_id', None)
                        st.rerun()
                    elif cancel_clicked:
                        st.session_state.pop('renaming_conv_id', None)
                        st.rerun()
                    continue

                col_title, col_rename, col_delete = st.columns([4, 1, 1])
                with col_title:
                    label = ("🟢 " if is_active else "") + (row['title'] or handler["default_title"])
                    if st.button(label, key=f"conv_{row['id']}", width='stretch'):
                        if not is_active:
                            detail = load_conversation(row['id'])
                            if detail:
                                handler["load"](detail)
                                st.rerun()
                with col_rename:
                    if st.button("✏️", key=f"rename_{row['id']}"):
                        st.session_state['renaming_conv_id'] = row['id']
                        st.rerun()
                with col_delete:
                    if st.button("🗑", key=f"del_{row['id']}"):
                        was_active = is_active
                        delete_conversation(row['id'])
                        if was_active:
                            handler["start_new"]()
                        st.rerun()

        # 🌟 [대화로 기간 설정] 사이드바 시청기간 설정은 없앴다 - 분석/보고서는 대화에서 말한
        # 기간이 분석 스펙("기간")에 담겨 그 집계에만 적용된다(7월 대비 8월처럼 기간을 넘나드는
        # 비교가 가능). 타겟팅은 적재된 전체 기간을 쓰고, 최근성은 대화 조건(최근시청일이후)으로 좁힌다.
        st.divider()
        with st.expander("📂 데이터 업로드"):
            render_upload_panel()

        with st.expander("⚙️ 시스템 정보"):
            try:
                periods = get_loaded_periods()
            except Exception:
                periods = []

            if not periods:
                st.caption("적재된 시청내역 DB가 없습니다.")
            else:
                first_month = periods[0]['month']
                last_month = periods[-1]['month']
                st.caption(f"적재된 시청내역 기간: **{first_month} ~ {last_month}** ({len(periods)}개월)")
                for p in periods:
                    st.caption(f"　·  {p['month']}  —  {p['rows']:,}건")
