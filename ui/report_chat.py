# ui/report_chat.py
# ============================================================
# 📝 보고서 탭 화면. 📊 분석 탭과 같은 단순 챗 패턴(phase 없음)이다.
# 🌟 [활동 보고서 정리] SO별 활동 보고서를 하나씩 붙여넣으면 트리로 정리해 쌓고, 쌓인 보고서로 취합 방향을 대화로
# 의논해 취합안(키워드 위주 등)을 만든다. 알마인드 파일은 화면 아래 패널에서 최종본 하나만 내려받는다.
# 시청 데이터 집계를 요청했을 때만 차트/표가 추가로 붙는다.
# ============================================================
import re

import streamlit as st
from services.report_service import (
    process_report_turn, read_attachments, collect_so_reports, collect_materials, merged_title, region_status, latest_draft,
    draft_is_stale,
)
from utils.file_reader import FILE_TYPES
from ui.chart_render import render_chart_card
from ui.chat_styles import inject_chat_css, render_message
from ui.target_card import autosave_simple
from utils.emm_export import build_emm_from_nodes, merge_report_trees, parse_report_tree


def _safe_filename(text):
    return re.sub(r'[\\/:*?"<>|\s]+', '_', text).strip('_') or "보고서"


def _use_key(so):
    return f"merge_use_{st.session_state.get('report_current_conversation_id')}_{so}"


def _excluded_so(messages):
    """취합 체크박스에서 뺀 SO(AI가 취합안을 만들 때도 재료에서 뺀다)."""
    return [r['SO'] for r in collect_so_reports(messages) if st.session_state.get(_use_key(r['SO'])) is False]


def _material_key(index):
    return f"mat_use_{st.session_state.get('report_current_conversation_id')}_{index}"


def _excluded_materials(messages):
    """체크박스에서 뺀 분석 자료의 메시지 번호."""
    return [m['index'] for m in collect_materials(messages) if st.session_state.get(_material_key(m['index'])) is False]


def _render_merge_panel(messages):
    """정리된 SO 현황을 보여주고, 최종 알마인드 파일을 딱 하나 내려받게 한다.
    대화로 만든 취합안(draft)이 있으면 그것을, 없으면 SO별로 가지 하나씩 묶은 기본 취합을 쓴다(같은 SO를 다시 붙여넣었으면 나중 것)."""
    reports, materials = collect_so_reports(messages), collect_materials(messages)
    if not reports and not materials:
        return
    status, extra = region_status(reports)
    with st.container(border=True):
        st.markdown("**:material/inventory_2: SO별 취합 현황 · 최종 알마인드**")
        st.markdown("  ".join(f":green[:material/check_circle:] {region}" if so else f":gray[:material/radio_button_unchecked:] {region}"
                             for region, so in status)
                    + (f"  :blue[:material/add_circle:] {', '.join(extra)}" if extra else ""))
        st.caption(f"{len(reports)}개 SO 정리됨 (기본 권역 {sum(1 for _, so in status if so)}/{len(status)}). "
                   "빈 원은 아직 붙여넣지 않은 권역이에요. 다른 SO 보고서를 이어서 붙여넣으면 여기에 쌓여요.")
        def _label(r):
            note = f" · 원문 표기 '{r['원본이름']}'" if r['원본이름'] != r['SO'] else ""
            note += " · 다시 붙여넣어 최신본으로 교체됨" if r['교체'] else ""
            return f"{r['SO']} ({len(r['nodes'])}개 항목){note}"

        picked = [r for r in reports if st.checkbox(_label(r), value=True, key=_use_key(r['SO']))]
        if any(r['SO'].startswith("SO 미확인") for r in reports):
            st.caption(":material/warning: SO를 알 수 없는 보고서가 있어요. 그 보고서를 다시 붙여넣으며 'OO 보고서야'라고 SO를 알려주세요.")

        if materials:
            for m in materials:
                st.checkbox(f"{m.get('종류', '분석')} 자료 · {m['제목']}", value=True, key=_material_key(m['index']))
            st.caption("분석·첨부 자료는 대화로 취합안을 만들 때 반영돼요(\"이번 취합안에 이 자료도 넣어줘\"). SO별 기본 취합에는 들어가지 않아요.")

        draft = latest_draft(messages)
        use_draft = False
        if draft:
            choice = st.radio("최종 알마인드로 받을 안", ["대화로 만든 취합안 (가장 최근)", "SO별 기본 취합 (SO마다 가지 하나)"],
                              key=f"final_choice_{st.session_state.get('report_current_conversation_id')}")
            use_draft = choice.startswith("대화로")
            if use_draft and draft_is_stale(messages):
                st.warning("취합안을 만든 뒤 SO 보고서나 분석 자료가 새로 담기거나 교체됐어요. 그 내용은 이 취합안에 반영되지 않았으니, "
                           "대화로 '취합안 다시 만들어줘'라고 요청해 주세요.", icon=":material/warning:")
        if use_draft:
            nodes = parse_report_tree(draft['text'])
            data, title = build_emm_from_nodes(nodes, nodes[0][1]), nodes[0][1]
            st.caption("의논 끝에 만든 취합안이에요. 고치고 싶으면 대화로 요청하세요(예: '3번 키워드 빼줘').")
        elif len(picked) >= 2:
            key_suffix = "_".join(r['SO'] for r in picked)
            title = st.text_input("취합본 제목 (알마인드 중심 토픽)", value=merged_title(picked), key=f"merge_title_{key_suffix}").strip() or merged_title(picked)
            data = build_emm_from_nodes(merge_report_trees([r['nodes'] for r in picked]), title)
        else:
            st.caption("최종 파일은 2개 이상 SO를 선택하거나, 대화로 취합안을 만들면 받을 수 있어요.")
            return
        st.download_button("최종 알마인드 다운로드 (.emm)", icon=":material/download:", data=data, file_name=f"{_safe_filename(title)}.emm",
                           mime="application/octet-stream", key="emm_final")


def render_report_chat(profile_df, db_audience=None, db_content=None):
    inject_chat_css()

    st.title(":material/description: 보고서")
    st.caption("SO별 활동 보고서를 하나씩 붙여넣어 쌓고, 대화로 취합 방향을 정해 최종 알마인드 한 개로 받아요.")

    if not st.session_state.report_messages:
        greeting = (
            "안녕하세요! SO 활동 보고서를 하나씩 붙여넣어주세요. 원문의 내용(링크·수치·목록)은 그대로 두고 번호와 들여쓰기를 "
            "따라 트리로 정리해드릴게요. 예: '대전 SO 9월 4주차 활동 보고서야. 정리해줘' + 원문. "
            "SO 보고서가 쌓이면 '짬짬반장이랑 조회수 위주로 묶으면 어때?'처럼 취합 방향을 저와 의논할 수 있고, "
            "'이 키워드로 취합안 만들어줘'라고 하시면 예시를 보며 다듬은 뒤 최종 알마인드 파일 한 개로 받으실 수 있어요. "
            "글을 붙여넣는 대신 알마인드·엑셀·워드·한글·CSV·텍스트·사진·PDF 파일을 첨부해도 내용을 읽어서 같은 방식으로 정리해드려요."
        )
        render_message("assistant", greeting, animate=st.session_state.get('report_greet_stream_pending', False))
        st.session_state.report_greet_stream_pending = False

    last_idx = len(st.session_state.report_messages) - 1
    for i, turn in enumerate(st.session_state.report_messages):
        should_animate = (i == last_idx and turn["role"] == "assistant" and st.session_state.get('report_stream_next'))
        render_message(turn["role"], turn["text"], animate=should_animate)
        if should_animate:
            st.session_state.report_stream_next = False
        render_chart_card(turn.get("chart"), f"report_chart_{i}")

        if turn.get("missing"):  # 대조에서 다시 정리해도 남은 문제 - 숨기지 않고 알린다
            if turn.get("missing_kind") == "취합안":
                st.warning("다음 링크·숫자가 SO별 보고서에서 확인되지 않거나 이전 취합안에서 빠졌어요. 최종본에 넣기 전에 확인해 주세요: "
                           + ", ".join(turn["missing"][:12]), icon=":material/warning:")
            else:
                st.warning("원문에 있는 다음 링크·숫자·문장이 정리 결과에서 확인되지 않아요. 원문과 대조해 주세요: "
                           + ", ".join(turn["missing"][:12]), icon=":material/warning:")
        if turn.get("checks"):
            with st.expander("원문 대조 기록", icon=":material/verified_user:"):
                for c in turn["checks"]:
                    st.caption(f"· {c}")
        if turn.get("draft"):
            st.caption(":material/edit_note: 취합안 미리보기예요. 마음에 들면 아래 패널에서 최종 알마인드로 받으세요. 고칠 부분은 대화로 말씀해 주세요.")

    _render_merge_panel(st.session_state.report_messages)

    pending = st.session_state.pop('report_pending', None)
    if pending:
        earlier = st.session_state.report_messages[:-1]
        attachments, failed = [], []
        if pending['files']:
            with st.spinner("첨부 파일을 읽는 중... (사진·PDF는 글자를 읽느라 조금 걸려요)"):
                attachments, failed = read_attachments(pending['files'])
        notice = {"role": "assistant", "text": "읽지 못한 첨부 파일이 있어요:\n" + "\n".join(f"· {f}" for f in failed)} if failed else None
        if not pending['text'] and not attachments:  # 파일만 올렸는데 하나도 못 읽었으면 AI를 부를 것이 없다
            st.session_state.report_messages = st.session_state.report_messages + [notice]
        else:
            with st.spinner("보고서를 정리하는 중... (원문과 대조하는 중이라 조금 걸려요)"):
                new_messages, _ = process_report_turn(
                    earlier, pending['text'], profile_df, db_audience, db_content,
                    excluded_so=_excluded_so(earlier), excluded_materials=_excluded_materials(earlier),
                    attachments=attachments, display_text=pending['display'],
                )
            st.session_state.report_messages = new_messages + ([notice] if notice else [])
        st.session_state.report_stream_next = True
        autosave_simple('report')
        st.rerun()

    submitted = st.chat_input("SO 활동 보고서를 붙여넣거나 파일(알마인드·엑셀·워드·한글·사진 등)을 첨부해 주세요", accept_file="multiple", file_type=FILE_TYPES)
    if submitted:
        text = (submitted.text or "").strip()
        files = [(f.name, f.getvalue()) for f in submitted.files]
        if text or files:
            display = "\n".join(x for x in (text, "[첨부] " + " · ".join(n for n, _ in files) if files else "") if x)
            st.session_state.report_messages.append({"role": "user", "text": display})
            st.session_state.report_pending = {"text": text, "display": display, "files": files}
            st.rerun()
