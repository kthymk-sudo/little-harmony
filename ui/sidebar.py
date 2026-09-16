# ui/sidebar.py
# ============================================================
# 🌟 [신규] ChatGPT/Gemini 스타일 사이드바.
# 상단에 '+ 새 대화' 버튼, 그 아래 저장된 대화(=타겟 작업) 목록,
# 시청기간 설정, 맨 아래 접힌 상태의 데이터 업로드 섹션으로 구성.
# 대화 목록의 각 항목은 하나의 '타겟 설정 스레드'이며 클릭하면 그
# 대화 전체(메시지, 조건, 확정 결과, 카피)가 복원된다.
# ============================================================
import datetime
import streamlit as st
from config import start_new_conversation, load_conversation_into_session
from database.db_manager import list_conversations, load_conversation, delete_conversation, rename_conversation
from ai_engine.gemini_api import get_current_model_label
from ui.upload_panel import render_upload_panel


def render_sidebar():
    with st.sidebar:
        # 1. 컬럼 비율 유지
        col1, col2 = st.columns([1.2, 4])
        
        with col1:
            # 🌟 [추가됨] 로고 위쪽에 20px 만큼의 투명한 빈 공간을 만들어 로고를 아래로 밀어냅니다.
            st.markdown("<div style='margin-top: 20px;'></div>", unsafe_allow_html=True)
            st.image("logo.png", width=150) 
            
        with col2:
            # 🌟 [수정됨] 글씨의 위쪽 여백(margin-top)을 로고 높이와 비슷하게 맞춥니다.
            st.markdown(
                "<h3 style='margin-top: 5px; margin-bottom: 0px; font-size: 1.3rem; letter-spacing: -0.02em;'>"
                "고객 타겟팅&카소 자동화</h3>", 
                unsafe_allow_html=True
            )
            
        st.write("") # 타이틀과 아래 '+ 새 대화' 버튼 사이에 약간의 여유 공간

        if st.button("＋ 새 대화", width='stretch', type="primary"):
            start_new_conversation()
            st.rerun()

        st.divider()
        st.caption("대화 기록")

        conv_list = list_conversations()
        if conv_list.empty:
            st.caption("아직 저장된 대화가 없습니다.")
        else:
            for _, row in conv_list.iterrows():
                is_active = (row['id'] == st.session_state.current_conversation_id)

                # 🌟 [고도화 - 자주 쓰는 타겟 즐겨찾기 대체] 이름 수정 모드일 때는 버튼 대신
                # 입력창을 보여준다. "이탈위험 4050 여성"처럼 기억하기 쉬운 이름으로 바꿔두면,
                # 매번 조건을 다시 설명하지 않고 이 목록에서 바로 찾아 이어서 쓸 수 있다.
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
                        final_title = new_title.strip() or row['title'] or "새 타겟 대화"
                        rename_conversation(row['id'], final_title)
                        st.session_state.pop('renaming_conv_id', None)
                        st.rerun()
                    elif cancel_clicked:
                        st.session_state.pop('renaming_conv_id', None)
                        st.rerun()
                    continue

                col_title, col_rename, col_delete = st.columns([4, 1, 1])
                with col_title:
                    label = ("🟢 " if is_active else "") + (row['title'] or "새 타겟 대화")
                    if st.button(label, key=f"conv_{row['id']}", width='stretch'):
                        if not is_active:
                            detail = load_conversation(row['id'])
                            if detail:
                                load_conversation_into_session(detail)
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
                            start_new_conversation()
                        st.rerun()

        st.divider()
        with st.expander("📅 시청기간 설정"):
            use_period = st.checkbox("특정 기간만 집계", value=st.session_state.get('use_period_filter', False))
            st.session_state.use_period_filter = use_period
            if use_period:
                col1, col2 = st.columns(2)
                with col1:
                    start = st.date_input("시작일", value=st.session_state.get('period_start') or datetime.date.today().replace(day=1))
                with col2:
                    end = st.date_input("종료일", value=st.session_state.get('period_end') or datetime.date.today())
                st.session_state.period_start = start
                st.session_state.period_end = end
            else:
                st.session_state.period_start = None
                st.session_state.period_end = None

        st.divider()
        with st.expander("📂 데이터 업로드"):
            render_upload_panel()

        # 🌟 [429 진단 편의] 429(요청 한도 초과) 오류가 자주 뜰 때, "지금 실제로 어떤
        # 모델이 쓰이고 있는지"를 실무자가 직접 확인할 수 있게 한다. 자동 선택 로직이
        # 하필 한도가 낮은 실험/미리보기 모델을 골랐는지, 아니면 정말 사용량이 많아서인지
        # Google AI Studio 사용량 페이지에서 이 이름으로 바로 대조해볼 수 있다.
        with st.expander("⚙️ 시스템 정보"):
            try:
                model_label = get_current_model_label()
            except Exception:
                model_label = "확인 불가"
            st.caption(f"현재 사용 중인 AI 모델: `{model_label}`")
            st.caption(
                "429(요청 한도 초과) 오류가 자주 뜬다면, 위 모델 이름으로 "
                "[Google AI Studio 사용량 페이지](https://aistudio.google.com/rate-limit)에서 "
                "정확한 한도와 현재 사용량을 확인해보세요."
            )
