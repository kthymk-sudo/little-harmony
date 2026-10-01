# services/report_agent.py
# ============================================================
# 📝 보고서 탭 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능).
#
# 🌟 [대화형 AI] 정해진 명령어·작업 이름·문구 정규식으로 길을 나누지 않는다. AI가 대화 속에서 뜻을 읽고 스스로 도구를 골라 일한다:
#   save_so_report(SO 보고서 정리 저장) / save_merged_mindmap(취합안 저장) / show_example_mindmap(구성 예시) / check_data(간단한 수치 확인)
#   글로 하는 일(질문·피드백·메일 초안 등)은 도구 없이 답 글로 한다. 뜻이 불분명하면 되묻고, 불법·부당한 요청은 같은 대화 안에서 거절한다(prompts/report_agent_prompt.py).
# 🌟 [사실 검증] 도구가 저장하기 전에 시스템이 원문과 대조한다(링크·숫자·문장 누락, 지어낸 값, 이전 안에서 빠진 값). 문제가 있으면 저장하지 않고 그 목록을
#   AI에게 돌려줘서 AI가 스스로 고쳐 다시 부르게 한다. 두 번 고쳐도 남으면 저장하되 화면에 경고로 남긴다.
# ============================================================
import re

from services.agent_loop import run_agent, confused, contents_from
from prompts.report_agent_prompt import get_report_agent_prompt
from services.code_analyst import lazy_tables, quick_check
from services.report_service import (
    find_missing_items, build_material, collect_materials, so_reports_context, collect_so_reports, latest_draft,
)
from database.db_manager import data_period_line
from utils.emm_export import parse_report_tree, strip_tree_lines

_LONG_PASTE_CHARS = 200          # 이 길이 이상 붙여넣은 글은 "정리할 보고서 원문"으로 본다
_MAX_FIXES = 2                   # 같은 도구의 저장 시도가 대조에서 걸려 되돌려 보내는 최대 횟수(그 뒤에는 경고를 남기고 저장)
_MAX_MISSING_SHOWN = 12
_MAX_HISTORY_PASTE = 15000       # 정리되지 않은 긴 원문은 대화에 그대로 남기되 이 길이까지만
_MARKERS = "▸·-•▪◦‣∙"
_CONFUSED = confused("'SO 보고서 정리해줘', '올린 자료 요약해서 알마인드로 만들어줘'")
_DECLARATIONS = [
    {"name": "save_so_report",
     "description": "실무자가 이번에 올린(붙여넣은·첨부한) SO 활동 보고서 하나를 알마인드 트리로 정리해 저장한다. 원문 내용은 하나도 빼거나 바꾸지 않는다.",
     "parameters": {"type": "OBJECT", "properties": {
         "so": {"type": "STRING", "description": "SO 이름(예: 대전, 광주). 모르면 빈 문자열"},
         "period": {"type": "STRING", "description": "보고 기간(예: 9월 4주차). 모르면 빈 문자열"},
         "tree": {"type": "STRING", "description": "정리한 트리 전체(📌 중심 한 줄 + ▸ · - 기호의 들여쓰기 항목)"}},
         "required": ["so", "tree"]}},
    {"name": "save_merged_mindmap",
     "description": "저장된 자료를 취합한 최종 알마인드(취합안)를 만들거나 고쳐서 저장한다. 구성·순서·묶음 변경, 요약, 자료 추가, 피드백 가지 추가가 모두 이 도구다. 항상 고친 전체 트리를 넘긴다.",
     "parameters": {"type": "OBJECT", "properties": {
         "tree": {"type": "STRING", "description": "취합안 트리 전체(📌 중심 한 줄 + ▸ · - 기호의 들여쓰기 항목)"},
         "removed_on_purpose": {"type": "BOOLEAN", "description": "요약하거나 빼 달라는 요청으로 이전 취합안의 내용을 일부러 줄였으면 true"}},
         "required": ["tree"]}},
    {"name": "show_example_mindmap",
     "description": "구성을 의논하는 중에 어떤 모양이 될지 보여 주는 짧은 예시 그림. 저장되지 않는다.",
     "parameters": {"type": "OBJECT", "properties": {"tree": {"type": "STRING", "description": "예시 트리(3~4개 SO만, 10줄 안팎)"}}, "required": ["tree"]}},
    {"name": "check_data",
     "description": "시청 데이터로 간단한 수치를 계산해 확인한다(MAU, SO별 시청자수, 재방문율, 순위 등). 그래프·깊은 분석은 하지 않는다. 결과는 보고서 자료로 담긴다.",
     "parameters": {"type": "OBJECT", "properties": {
         "question": {"type": "STRING", "description": "확인할 내용. 집단·기간·기준을 빠짐없이 적는다"},
         "save_as_material": {"type": "BOOLEAN", "description": "보고서에 넣을 수치를 알아보는 것이면 true(기본). 방금 담은 수치가 맞는지 확인만 하는 것처럼 새 자료가 필요 없으면 false"}},
         "required": ["question"]}},
]


def tree_block(text):
    """답변 글에서 트리 부분만(📌 줄부터 이어지는 항목 줄들). 트리 뒤에 붙은 AI의 설명 글은 뺀다."""
    out, started = [], False
    for raw in text.splitlines():
        s = raw.strip().lstrip("﻿")
        if not started:
            started = s.startswith("📌")
        if not started:
            continue
        if not s:
            continue
        if s.startswith("📌") or s[0] in _MARKERS:
            out.append(raw.rstrip())
        else:
            break
    return "\n".join(out)


def _clean_tree(tree):
    """AI가 넘긴 트리를 다듬고 형식을 확인한다. 반환: (트리 글, None) 또는 (None, AI에게 알려줄 문제)."""
    lines = [l.rstrip() for l in re.sub(r"^```\w*\s*|```\s*$", "", (tree or "").strip()).splitlines() if l.strip()]
    if not lines or not lines[0].strip().startswith("📌"):
        return None, "첫 줄이 '📌 중심 제목'이어야 해."
    if sum(l.strip().startswith("📌") for l in lines) != 1:
        return None, "📌 중심 줄은 정확히 하나여야 해."
    for n, l in enumerate(lines[1:], 2):
        s = l.strip()
        if s.startswith(("💡", "📊")):
            return None, f"{n}번째 줄이 💡/📊로 시작해. 피드백 가지는 '▸ 💡 피드백'처럼 ▸ 가지로 써."
        if s[0] not in _MARKERS and not s.startswith("📌"):
            return None, f"{n}번째 줄('{s[:20]}')이 가지 기호(▸ · -)로 시작하지 않아. 모든 줄이 기호로 시작해야 해."
    if len(parse_report_tree("\n".join(lines))) < 3:
        return None, "항목이 너무 적어(3개 이상이어야 해)."
    return "\n".join(lines), None


def _plain_reply(text):
    """트리 뒤에 붙일 답 글: 마크다운 강조를 지우고, 트리 기호로 시작하는 줄은 트리로 오인되지 않게 '– '로 바꾼다."""
    out = []
    for line in text.replace("**", "").splitlines():
        s = line.strip()
        if s.startswith("📌"):
            s = s[1:].strip()
        elif s and s[0] in _MARKERS:
            s = "– " + s[1:].strip()
        out.append(s if s != line.strip() else line.rstrip())
    return "\n".join(out).strip()


class _Turn:
    """한 턴 동안 도구가 모은 것: 만든 트리 메시지, 담을 자료, 이번 턴 원문, 대조 실패 횟수."""

    def __init__(self, ai_text, source, reports, materials, draft_tree, tables):
        self.ai_text, self.source, self.reports, self.materials, self.draft_tree, self.tables = ai_text, source, reports, materials, draft_tree, tables
        self.trees, self.new_materials, self.fails, self.checks = [], [], {}, {}

    @property
    def worked(self):
        return bool(self.trees or self.new_materials)


def _verify(turn, name, problems):
    """문제 목록이 있으면 (고쳐 다시 부르라는 응답, None), 이미 두 번 고쳤으면 (None, 남은 문제)로 저장을 허락한다."""
    if not problems:
        return None, []
    items = ", ".join(problems[:_MAX_MISSING_SHOWN])
    if turn.fails.get(name, 0) < _MAX_FIXES:
        turn.fails[name] = turn.fails.get(name, 0) + 1
        turn.checks.setdefault(name, []).append(f"대조 {len(problems)}건 → 다시 작성: {items}")
        return {"저장됨": False, "문제": problems[:_MAX_MISSING_SHOWN * 2],
                "안내": "이 문제를 고쳐서 전체 트리를 처음부터 다시 불러. 링크·숫자·문장은 자료에 있는 그대로, 다른 내용은 바꾸지 마."}, None
    return None, problems


def _tool_save_so_report(turn, args):
    tree, err = _clean_tree(args.get("tree"))
    if err:
        return {"저장됨": False, "문제": [err]}
    if len(turn.source) < _LONG_PASTE_CHARS:
        return {"저장됨": False, "문제": ["이번에 올린 보고서 원문이 없어. 원문을 먼저 올려 달라고 안내해."]}
    reply, left = _verify(turn, "so", find_missing_items(turn.source, tree, lines=True))
    if reply:
        return reply
    msg = {"role": "assistant", "text": tree, "report_meta": {"SO": str(args.get("so") or "").strip(), "기간": str(args.get("period") or "").strip()}}
    _attach_checks(turn, msg, "so", left, kind=None)
    turn.trees.append(msg)
    return {"저장됨": True, "항목수": len(parse_report_tree(tree)),
            **({"안내": "일부 항목이 확인되지 않은 채 저장됐어. 실무자에게 원문과 대조해 달라고 알려줘: " + ", ".join(left[:_MAX_MISSING_SHOWN])} if left else {})}


def _source_text(turn):
    return "\n".join([r['text'] for r in turn.reports] + [m['본문'] for m in turn.materials] + [turn.ai_text, turn.draft_tree])


def _tool_save_merged(turn, args):
    tree, err = _clean_tree(args.get("tree"))
    if err:
        return {"저장됨": False, "문제": [err]}
    if tree == turn.draft_tree:   # 같은 일을 되풀이하지 않는다
        return {"저장됨": False, "문제": ["현재 취합안과 똑같은 트리야. 이미 저장된 상태니 다시 저장하지 말고, 실무자의 말에 글로 답해(바꿀 곳이 있었다면 그 부분을 실제로 바꿔서 다시 불러)."]}
    problems = find_missing_items(tree, _source_text(turn))   # 결과의 링크·숫자는 자료에 있어야 한다(지어낸 값 방지)
    if turn.draft_tree and not args.get("removed_on_purpose"):   # 구성만 바꾸는 일이면 이전 취합안의 링크·숫자가 그대로여야 한다
        problems += [p for p in find_missing_items(turn.draft_tree, tree) if p not in problems]
    reply, left = _verify(turn, "merged", problems)
    if reply:
        return reply
    msg = {"role": "assistant", "text": tree, "draft": True}
    _attach_checks(turn, msg, "merged", left, kind="취합안")
    turn.trees.append(msg)
    return {"저장됨": True, "항목수": len(parse_report_tree(tree)),
            **({"안내": "일부 항목이 확인되지 않은 채 저장됐어. 실무자에게 확인해 달라고 알려줘: " + ", ".join(left[:_MAX_MISSING_SHOWN])} if left else {})}


def _tool_example(turn, args):
    tree, err = _clean_tree(args.get("tree"))
    if err:
        return {"보임": False, "문제": [err]}
    problems = find_missing_items(tree, _source_text(turn))
    if problems:   # 예시도 자료에 없는 링크·숫자를 지어내면 안 된다
        return {"보임": False, "문제": problems[:_MAX_MISSING_SHOWN], "안내": "자료에 있는 값만 써서 다시 불러."}
    turn.trees.append({"role": "assistant", "text": tree})
    return {"보임": True}


def _tool_check_data(turn, args):
    question = str(args.get("question") or "").strip()
    tables = turn.tables()
    if not question or tables is None:
        return {"오류": "확인할 내용이 비었거나 불러온 시청 데이터가 없어."}
    reply, error = quick_check(tables, question)
    if error:
        return {"오류": error}
    keep = args.get("save_as_material") is not False
    if keep:
        turn.new_materials.append({**build_material(question, reply), "종류": "분석"})
    return {"결과": reply['text'], "안내": ("이 결과는 보고서 자료로 담겼어. " if keep else "") + "계산에 쓴 집단·기간을 밝혀서 알려줘. 데이터 컬럼 이름 같은 내부 용어는 쓰지 마."}


def _attach_checks(turn, msg, name, left, kind):
    if turn.checks.get(name):
        msg["checks"] = turn.checks.pop(name)
    if left:
        msg["missing"] = left[:_MAX_MISSING_SHOWN * 2]
        if kind:
            msg["missing_kind"] = kind


_TOOLS = {"save_so_report": _tool_save_so_report, "save_merged_mindmap": _tool_save_merged,
          "show_example_mindmap": _tool_example, "check_data": _tool_check_data}


def _saved_after(messages, i):
    """i번째 사용자 메시지 뒤, 다음 사용자 메시지 전에 SO 보고서로 정리된 답이 있는지."""
    for m in messages[i + 1:]:
        if m.get('role') == 'user':
            return False
        if m.get('report_meta'):
            return True
    return False


def _history_contents(messages):
    """AI에게 보낼 이전 대화(contents). 정리가 끝난 긴 원문은 한 줄로 줄이고(저장본이 [현재 상태]에 있다), 정리되지 않은 원문은 그대로 둔다.
    트리는 [현재 상태]에 따로 있으니 말풍선에 보인 글만 남긴다."""
    pairs = []
    for i, m in enumerate(messages):
        text = m.get('text', '')
        if m.get('role') == 'user':
            if len(text) >= _LONG_PASTE_CHARS and _saved_after(messages, i):
                text = text[:40].replace("\n", " ") +f"… (보고서 원문 {len(text)}자 - 정리본은 [현재 상태] 참고)"
            pairs.append(('user', text[:_MAX_HISTORY_PASTE]))
        else:
            pairs.append(('model', (strip_tree_lines(text) or "(알마인드를 저장했어요)") if parse_report_tree(text) else text))
    return contents_from(pairs)


def _state_block(reports, materials, draft, db_audience, attachments):
    try:
        period = data_period_line(db_audience) if db_audience is not None else "(불러온 시청 데이터 없음)"
    except Exception:
        period = ""
    draft_text = tree_block(draft['text']) if draft else ""
    return (f"SO 보고서와 자료:\n{so_reports_context(reports, materials) or '(아직 저장된 자료 없음)'}\n\n"
            f"현재 취합안(없으면 아직 만들지 않은 것):\n{draft_text or '(없음)'}\n\n"
            f"시청 데이터: {period}\n이번에 올린 첨부 파일: {', '.join(a['제목'] for a in attachments) or '없음'}")


def process_report_turn(messages, user_text, profile_df, db_audience=None, db_content=None, excluded_so=(), excluded_materials=(),
                        attachments=(), display_text=None):
    """반환: (new_messages, None) - 화면이 쓰던 형태를 유지한다.
    excluded_so / excluded_materials: 취합에서 뺀 SO 이름들 / 분석 자료의 메시지 번호 - AI 재료에서도 뺀다.
    attachments: 이번에 올린 파일들 [{'제목', '본문'}] - AI에게는 "[첨부 파일: 이름]" 블록으로 전달한다.
    display_text: 화면·저장용 사용자 말풍선 글. 없으면 user_text."""
    attachments = list(attachments)
    ai_text = (user_text + "".join(f"\n\n[첨부 파일: {a['제목']}]\n{a['본문']}" for a in attachments)).strip()
    user_turn = {"role": "user", "text": user_text if display_text is None else display_text}
    reports = [r for r in collect_so_reports(messages) if r['SO'] not in set(excluded_so)]
    materials = collect_materials(messages, excluded_materials)
    draft = latest_draft(messages)
    contents = _history_contents(messages) + [{"role": "user", "parts": [{"text": ai_text}]}]
    system = get_report_agent_prompt(_state_block(reports, materials, draft, db_audience, attachments))

    # 정리할 원문: 이번에 올린 글이 길면 그것, 짧으면(예: "대전 보고서야") 아직 정리되지 않은 가장 최근 긴 원문
    source = ai_text if len(ai_text) >= _LONG_PASTE_CHARS else next(
        (m['text'] for i, m in reversed(list(enumerate(messages))) if m.get('role') == 'user'
         and len(m['text']) >= _LONG_PASTE_CHARS and not _saved_after(messages, i)), ai_text)
    turn = _Turn(ai_text, source, reports, materials, tree_block(draft['text']) if draft else "", lazy_tables(db_audience, db_content, profile_df))

    final_text, api_error = run_agent(system, contents, _DECLARATIONS, _TOOLS, turn)

    if api_error and not turn.worked:
        return messages + [user_turn, {"role": "assistant", "text": api_error}], None
    if not final_text:
        final_text = "" if turn.trees else ("분석 결과를 자료로 담아 뒀어요." if turn.worked else _CONFUSED)

    notes = [{"role": "assistant", "text": f"분석 자료를 담았어요: {m['제목']}", "material": m} for m in turn.new_materials]
    if not any(t.get('report_meta') for t in turn.trees):   # SO 보고서로 쓰인 첨부가 아니면 이후 대화에서도 쓸 수 있게 자료로 담아 둔다
        notes += [{"role": "assistant", "text": f"첨부 자료를 담았어요: {a['제목']}", "material": {**a, "종류": "첨부"}} for a in attachments]
    trees = turn.trees
    if len(trees) == 1 and final_text:
        trees[0]["text"] += "\n\n" + _plain_reply(final_text)
        reply = []
    else:
        reply = [{"role": "assistant", "text": final_text}] if final_text else []
    return messages + [user_turn] + notes + trees + reply, None


if __name__ == "__main__":
    import services.answer_check as _ac
    _ac.check_answer = lambda p: '{"문제": []}'   # 검수 AI는 테스트에서 부르지 않는다(문제 없음)
    import sys
    me = sys.modules[__name__]
    import services.agent_loop as loop

    SRC = ("● 9월 4주차\n1. 캠페인 진행 https://rainbowtv.app.link/tbVHa8ZYs6b 콘텐츠 조회수 14회에서 70회로 400% 증가, "
           "총 13개소 참여, 사진 125건.\n" + "참여 경로당 확대 ")
    SRC = SRC + "대전 보고입니다 " * 20
    TREE = ("📌 대전 · 9월 4주차 활동 보고\n  ▸ 캠페인 진행\n    · https://rainbowtv.app.link/tbVHa8ZYs6b\n    · 콘텐츠 조회수 14회에서 70회로 400% 증가\n"
            "    · 총 13개소 참여, 사진 125건\n    · 참여 경로당 확대\n    · 대전 보고입니다")
    sent = []

    def script(*steps):
        """steps: 모델의 응답들 - 문자열이면 글 답, (이름, 인자) 튜플이면 도구 호출."""
        it = iter(steps)

        def fake(system, contents, tool_declarations=None, temperature=0.3, force_tool=False):
            sent.append((system, [c for c in contents], force_tool))
            step = next(it)
            if isinstance(step, tuple):
                return {"role": "model", "parts": [{"functionCall": {"name": step[0], "args": step[1]}}]}, None
            return {"role": "model", "parts": [{"text": step}]}, None
        loop.call_agent = fake

    def run(msgs, text, **kw):
        out, _ = process_report_turn(msgs, text, None, **kw)
        return out

    # 1) 원문 정리: 첫 시도에서 링크가 빠지면 도구가 알려주고, 고쳐서 다시 부르면 저장 + 답 글이 같은 말풍선에 붙는다
    bad = TREE.replace("https://rainbowtv.app.link/tbVHa8ZYs6b", "링크").replace("400%", "4배")
    script(("save_so_report", {"so": "대전", "period": "9월 4주차", "tree": bad}), ("save_so_report", {"so": "대전", "period": "9월 4주차", "tree": TREE}),
           "대전 보고서를 정리해 뒀어요.")
    m1 = run([], "대전 SO 9월 4주차 보고서야. 정리해줘.\n" + SRC)
    assert len(m1) == 2 and m1[1]["report_meta"] == {"SO": "대전", "기간": "9월 4주차"} and "https://rainbowtv" in m1[1]["text"], m1
    assert m1[1]["text"].endswith("대전 보고서를 정리해 뒀어요.") and m1[1]["checks"] and "missing" not in m1[1]
    assert collect_so_reports(m1)[0]["SO"] == "대전" and strip_tree_lines(m1[1]["text"]) == "대전 보고서를 정리해 뒀어요."
    assert tree_block(m1[1]["text"]) == TREE

    # 2) 두 번 고쳐도 빠지면 경고를 남기고 저장한다 / 형식이 틀린 트리는 저장하지 않는다
    script(("save_so_report", {"so": "대전", "tree": "트리 아님"}), ("save_so_report", {"so": "대전", "tree": bad}),
           ("save_so_report", {"so": "대전", "tree": bad}), ("save_so_report", {"so": "대전", "tree": bad}), "정리했어요. 일부 확인이 필요해요.")
    m = run([], SRC)
    assert m[1].get("missing") and "report_meta" in m[1], m

    # 3) 질문에는 도구 없이 답한다 - 트리·자료가 늘지 않는다 + 대화에 직전 답이 보인다
    script("피드백은 요청하실 때만 드려요. 지금은 반영된 피드백이 없어요.")
    m3 = run(m1, "분석한 내용의 피드백이 들어간거야?")
    assert len(m3) == 4 and m3[-1]["text"].startswith("피드백은") and not parse_report_tree(m3[-1]["text"])
    last = sent[-1][1]
    assert last[0]["role"] == "user" and last[-1]["parts"][0]["text"].startswith("분석한 내용") and "정리를 마쳤" not in str(last), last
    assert "(보고서 원문" in last[0]["parts"][0]["text"] and "참여 경로당 확대" in sent[-1][0], "정리가 끝난 원문은 줄이고 저장본은 [현재 상태]에 있어야 한다"

    # 4) 취합안: 지어낸 숫자(자료에 없음)는 되돌려 보내고, 고치면 draft로 저장한다. 이어서 구성 변경 시 이전 안의 값이 빠지면 되돌린다
    merged = "📌 9월 4주차 활동 보고 취합\n  ▸ 대전\n    · 캠페인 진행\n      - https://rainbowtv.app.link/tbVHa8ZYs6b\n      - 조회수 14회에서 70회로 400% 증가\n      - 총 13개소 참여, 사진 125건"
    script(("save_merged_mindmap", {"tree": merged + "\n      - 만족도 987명"}), ("save_merged_mindmap", {"tree": merged}), "SO를 가장 큰 가지로 취합했어요.")
    m4 = run(m1, "SO가 가장 큰 가지가 되게 취합해줘")
    assert m4[-1].get("draft") and "987" not in m4[-1]["text"] and m4[-1]["checks"] and len(m4) == 4
    script(("save_merged_mindmap", {"tree": merged.replace("총 13개소 참여, 사진 125건", "참여 많음")}),
           ("save_merged_mindmap", {"tree": merged.replace("      - 조회수 14회에서 70회로 400% 증가\n", "")}),
           ("save_merged_mindmap", {"tree": merged.replace("      - 조회수 14회에서 70회로 400% 증가\n", "")}), "x")
    m5 = run(m4, "순서 바꿔줘")   # 이전 안의 값(400%·70 등)이 빠지면 되돌리고, 끝내 안 고치면 경고를 달고 저장
    assert m5[-1].get("draft") and m5[-1].get("missing") and m5[-1]["missing_kind"] == "취합안", m5[-1]
    script(("save_merged_mindmap", {"tree": merged.replace("      - 조회수 14회에서 70회로 400% 증가\n", ""), "removed_on_purpose": True}), "조회수는 뺐어요.")
    m6 = run(m4, "조회수 빼줘")
    assert m6[-1].get("draft") and "missing" not in m6[-1], m6[-1]
    assert [x for x in m6 if x.get("draft")][-1] is m6[-1]

    script(("save_merged_mindmap", {"tree": merged}), "이미 반영돼 있어요.")
    m4b = run(m4, "피드백이 들어간거야?")   # 질문인데 같은 안을 다시 저장하려 하면 막고 글로 답하게 한다
    assert len(m4b) == len(m4) + 2 and not m4b[-1].get("draft") and m4b[-1]["text"] == "이미 반영돼 있어요."
    # 4-2) 도구를 부르지 않고 "저장했어요"라고만 하면 버리고, 도구를 반드시 부르게 해서 다시 시킨다
    script("정리해서 저장했어요.", ("save_so_report", {"so": "대전", "tree": TREE}), "이제 저장했어요.")
    m = run([], SRC)
    assert m[1].get("report_meta") and sent[-2][2] is True and sent[-3][2] is False and "시스템 알림" in str(sent[-2][1][-1]), m

    # 5) 구성 예시는 저장되지 않는 트리(draft·report_meta 없음)
    script(("show_example_mindmap", {"tree": merged}), "이런 모양이에요. 어떠세요?")
    m7 = run(m1, "SO 먼저 나누면 어떻게 보여?")
    assert not m7[-1].get("draft") and not m7[-1].get("report_meta") and parse_report_tree(m7[-1]["text"])

    # 6) 첨부: SO 보고서로 쓰이지 않으면 자료로 담긴다 / 이해 못 한 말은 되묻는다(빈 응답일 때도 사람 말) / 도구가 계속 불려도 끝난다
    script("엑셀 내용을 확인했어요. 어떻게 쓰고 싶으세요?")
    m8 = run(m1, "이거 봐줘", attachments=[{"제목": "표.xlsx", "본문": "SO | 시청자\n대전 | 120"}], display_text="이거 봐줘\n[첨부] 표.xlsx")
    assert m8[-2].get("material", {}).get("종류") == "첨부" and m8[-1]["text"].startswith("엑셀") and "[첨부 파일: 표.xlsx]" in sent[-1][1][-1]["parts"][0]["text"]
    script("")
    assert run([], "ㅇㅇ")[-1]["text"] == _CONFUSED
    script(*[("check_data", {"question": "x"})] * 9)
    import services.code_analyst as ca
    ca.build_tables = lambda *a: {}
    ca.run_analyst_turn = lambda *a, **k: {"text": "대전 6월 MAU는 120명이에요.", "table": [{"SO": "대전", "MAU": 120}]}
    out, _ = process_report_turn([], "대전 6월 MAU 알려줘", None, db_audience=object(), db_content=object())
    assert any(m.get("material", {}).get("종류") == "분석" for m in out) and out[-1]["role"] == "assistant", out
    loop.call_agent = lambda *a, **k: (None, "⚠️ 서버 과부하")
    assert run([], "안녕")[-1]["text"].startswith("⚠️")

    # 7) 이전 구성의 취합안(구성 메모 문단이 뒤에 붙은 옛 대화)도 그대로 읽힌다
    old = [{"role": "assistant", "text": merged + "\n\n구성 메모: 묶었어요.", "draft": True}]
    assert tree_block(old[0]["text"]) == merged and latest_draft(old) is old[0]
    print("report_agent self-check OK")
