# ui/target_review_panel.py
# ============================================================
# 기능 2: 확정된 타겟을 확인하고, AI가 선정 근거를 설명하는 화면.
# 여러 타겟을 이어서 작업할 수 있도록, 확정된 타겟을 이름을 붙여 저장하는
# 기능을 포함한다 (저장된 이력은 4단계 탭에서 다시 불러올 수 있음).
# ============================================================
import streamlit as st
from database.db_manager import (
    apply_target_conditions, format_target_summary,
    suggest_history_name, save_target_history,
)
from ai_engine.gemini_api import generate_target_reasoning


def compute_target_and_reasoning(profile_df, conditions):
    """조건 적용 -> 집계 -> 근거 설명 생성까지의 순수 처리 로직 (단위 테스트 가능)."""
    target_df, stats = apply_target_conditions(profile_df, conditions)
    summary_str = format_target_summary(conditions, stats)
    reasoning = generate_target_reasoning(str(conditions), str(stats))
    return target_df, stats, summary_str, reasoning


def render_target_review_panel(profile_df):
    st.subheader("2. 타겟 확인 및 근거")

    if not st.session_state.target_conditions:
        st.info("먼저 1단계 대화에서 타겟 조건을 정해주세요.")
        return

    st.write("현재 조건:", st.session_state.target_conditions)

    if st.button("이 조건으로 타겟 확정 및 근거 생성", type="primary"):
        with st.spinner("타겟을 계산하고 근거를 생성하는 중..."):
            target_df, stats, summary_str, reasoning = compute_target_and_reasoning(
                profile_df, st.session_state.target_conditions
            )
        st.session_state.target_result_df = target_df
        st.session_state.target_result_stats = stats
        st.session_state.target_summary_str = summary_str
        st.session_state.target_reasoning = reasoning
        # 조건을 새로 확정했으니, 이전에 불러온 저장 이력과의 연결은 끊는다
        # (저장하려면 아래에서 다시 '저장' 눌러야 새 이력으로 기록됨)
        st.session_state.active_history_id = None

    if st.session_state.target_result_df is not None:
        st.metric("대상자 수", f"{st.session_state.target_result_stats.get('대상자수', 0):,}명")
        st.write(st.session_state.target_reasoning)
        show_cols = [c for c in ['R고객번호', '성별', '나이', '시청자SO', '선호장르', '선호시청시간대']
                     if c in st.session_state.target_result_df.columns]
        st.dataframe(st.session_state.target_result_df[show_cols], width='stretch')

        st.divider()
        if st.session_state.active_history_id:
            st.caption(f"💾 저장된 이력 #{st.session_state.active_history_id}에 연결되어 있습니다. "
                       f"(3단계에서 카피를 생성하면 이 이력에 자동으로 갱신됩니다)")
        else:
            default_name = suggest_history_name(st.session_state.target_conditions)
            save_name = st.text_input("저장할 타겟 이름", value=default_name, key="save_name_input")
            if st.button("💾 이 타겟 저장"):
                member_ids = st.session_state.target_result_df['R고객번호'].astype(str).tolist()
                history_id = save_target_history(
                    name=save_name,
                    conditions=st.session_state.target_conditions,
                    stats=st.session_state.target_result_stats,
                    member_ids=member_ids,
                    reasoning=st.session_state.target_reasoning,
                    push_copy=st.session_state.push_copy_result,
                )
                st.session_state.active_history_id = history_id
                st.success(f"'{save_name}' 이름으로 저장했습니다. (4단계 탭에서 다시 불러올 수 있어요)")
                st.rerun()
