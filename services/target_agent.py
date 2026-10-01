# services/target_agent.py
# ============================================================
# 🎯 타겟 조건 대화 한 턴 처리 (Streamlit 비의존 - 단위 테스트 가능).
#
# 🌟 [대화형 AI] 정해진 명령어·유형 이름·문구 정규식으로 길을 나누지 않는다. AI가 대화 속에서 뜻을 읽고 스스로 도구를 골라 일한다:
#   set_conditions(조건 추가·수정·삭제 + 예상 인원) / preview_count(인원 확인) / segment_insight(집단이 많이 보는 것) /
#   content_ranking(콘텐츠·채널 순위) / group_breakdown(그룹별 분포) / check_data(간단한 수치 확인)
#   계산은 모두 시스템이 실제 데이터로 하고 AI는 그 결과로만 말한다. 뜻이 불분명하면 되묻고, 불법·부당한 요청은 같은 대화 안에서 거절한다.
# ============================================================
import json

from database.db_manager import (
    summarize_profile_context, summarize_segment_insight, summarize_content_ranking, summarize_group_breakdown,
    apply_target_conditions, _normalize_field_name, describe_term_matches, describe_period_warnings, data_period_line, format_conditions_line,
)
from prompts.target_agent_prompt import get_target_agent_prompt
from services.agent_loop import run_agent, confused, contents_from
from services.code_analyst import lazy_tables, quick_check

_MAX_HISTORY = 40   # AI에게 보내는 이전 대화 수(오래된 것은 뺀다)
_CONFUSED = confused("'4050 여성 중 운동 콘텐츠 좋아하는 분들', '대전 6월 시청자 수는?'")
_DECLARATIONS = [
    {"name": "set_conditions",
     "description": "타겟 조건을 추가·수정·삭제하고 예상 인원을 돌려준다. 이번에 바뀌는 필드만 담는다.",
     "parameters": {"type": "OBJECT", "properties": {
         "changes_json": {"type": "STRING", "description": "바꿀 조건 필드의 JSON 객체 문자열. 예: {\"성별\": \"여자\", \"나이대\": [\"40대\"], \"선호장르\": [\"운동\"]}(장르·채널은 짧은 핵심 키워드). 삭제만 할 때는 {}"},
         "remove": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "실무자가 분명히 빼 달라고 한 조건의 필드 이름들"}},
         "required": ["changes_json"]}},
    {"name": "preview_count",
     "description": "조건을 바꾸지 않고 인원을 확인한다. 비우면 지금 조건, 채우면 그 조건(현재 조건에 덧씌운 가정)의 인원.",
     "parameters": {"type": "OBJECT", "properties": {"what_if_json": {"type": "STRING", "description": "가정할 조건 JSON 객체 문자열(선택)"}}}},
    {"name": "segment_insight",
     "description": "특정 집단이 무엇을 가장 많이 보는지 등을 계산한다. 조건은 바뀌지 않는다.",
     "parameters": {"type": "OBJECT", "properties": {"conditions_json": {"type": "STRING", "description": "집단을 나타내는 조건 JSON 객체 문자열(타겟 조건과 같은 필드)"}},
                    "required": ["conditions_json"]}},
    {"name": "content_ranking",
     "description": "콘텐츠 또는 채널의 인기/비인기 순위(전체 또는 특정 집단).",
     "parameters": {"type": "OBJECT", "properties": {
         "target": {"type": "STRING", "description": "'콘텐츠' 또는 '채널'"},
         "order": {"type": "STRING", "description": "'인기'(많이 본 순) 또는 '비인기'(적게 본 순)"},
         "filters_json": {"type": "STRING", "description": "집단 조건 JSON 객체 문자열. 전체면 {}"}},
         "required": ["target", "order"]}},
    {"name": "group_breakdown",
     "description": "한 기준으로 나눈 인원 분포. SO는 8개 권역으로 묶고, 세부 SO로 나누려면 'SO세부'.",
     "parameters": {"type": "OBJECT", "properties": {
         "field": {"type": "STRING", "description": "기준 필드(성별, 나이대, SO, SO세부, 활동세그먼트, 선호장르 등)"},
         "filters_json": {"type": "STRING", "description": "집단 조건 JSON 객체 문자열. 전체면 {}"}},
         "required": ["field"]}},
    {"name": "check_data",
     "description": "위 도구로 안 되는 간단한 시청 데이터 수치 확인(재방문율, 지난달 대비 증감, 요일·시간대 등). 그래프·깊은 분석은 하지 않는다.",
     "parameters": {"type": "OBJECT", "properties": {"question": {"type": "STRING", "description": "확인할 내용. 집단·기간·기준을 빠짐없이 적는다"}}, "required": ["question"]}},
]


def _jsonable(value):
    """numpy 숫자 등이 섞인 계산 결과를 AI에게 보낼 수 있는 값으로 바꾼다."""
    return json.loads(json.dumps(value, ensure_ascii=False, default=lambda o: o.item() if hasattr(o, 'item') else str(o)))


def _parse_object(text):
    """JSON 객체 문자열 -> (dict, None) 또는 (None, 문제). 빈 문자열은 빈 dict."""
    if not str(text or "").strip():
        return {}, None
    try:
        value = json.loads(text)
    except (TypeError, ValueError):
        return None, "JSON 객체 형식이 아니야. {\"필드\": 값} 모양으로 다시 줘."
    return (value, None) if isinstance(value, dict) else (None, "JSON 객체({...})여야 해.")


def _visible(conditions):
    return {k: v for k, v in (conditions or {}).items() if not k.startswith('__')}   # '__'로 시작하는 키(분석 탭에서 가져온 고객번호 목록 등)는 내부용


class _Turn:
    """한 턴 동안 도구가 바꾼 것: 조건, 계산용 데이터."""

    def __init__(self, conditions, profile_df, db_audience, db_content):
        self.conditions, self.profile_df, self.db_audience = dict(conditions or {}), profile_df, db_audience
        self.worked, self.tables = False, lazy_tables(db_audience, db_content, profile_df)

    def count(self, conditions):
        _, stats = apply_target_conditions(self.profile_df, conditions, self.db_audience)
        n, total = int(stats.get('대상자수', 0)), int(stats.get('전체시청자수', 0))
        return {"예상인원": n, "전체시청자수": total, "비중": f"{n / total * 100:.1f}%" if total else "-"}


def _merge(conditions, changes, remove):
    """조건 병합: 빈 값(None·[]·"")은 '언급 안 함'이라 기존 값을 지키고, 빈 객체도 마찬가지. 삭제는 remove에 명시된 필드만.
    반환: (새 조건, 알 수 없어 무시한 필드 목록)."""
    merged, ignored = dict(conditions), []
    for key, value in changes.items():
        name = _normalize_field_name(key)
        if name is None:
            ignored.append(key)
        elif isinstance(value, dict):
            if value:
                merged[name] = value
        elif value not in (None, [], ""):
            merged[name] = value
    for field in remove or []:
        name = _normalize_field_name(field)
        if name:
            merged.pop(name, None)
    return merged, ignored


def _tool_set_conditions(turn, args):
    changes, err = _parse_object(args.get("changes_json"))
    if err:
        return {"적용됨": False, "문제": err}
    remove = [r for r in (args.get("remove") or []) if isinstance(r, str)]
    merged, ignored = _merge(turn.conditions, changes, remove)
    if merged == turn.conditions and not ignored:
        return {"적용됨": False, "문제": "바뀐 것이 없어. 이미 같은 조건이니 다시 설정하지 말고 실무자의 말에 글로 답해.",
                "현재조건": format_conditions_line(turn.conditions)}
    if merged != turn.conditions:
        added = {k: v for k, v in merged.items() if turn.conditions.get(k) != v}
        notes = list(dict.fromkeys(describe_period_warnings(turn.db_audience, added) + describe_term_matches(turn.db_audience, added)))
        turn.conditions, turn.worked = merged, True
    else:
        notes = []
    result = {"적용됨": True, "현재조건": format_conditions_line(turn.conditions), **turn.count(turn.conditions)}
    if notes:
        result["참고"] = notes
    if ignored:
        result["무시한필드"] = ignored + ["(위 이름은 조건 필드가 아니라 반영하지 못했어. 알맞은 필드로 다시 부르거나 실무자에게 뜻을 물어봐)"]
    return result


def _tool_preview_count(turn, args):
    extra, err = _parse_object(args.get("what_if_json"))
    if err:
        return {"문제": err}
    conditions, _ = _merge(turn.conditions, extra, [])
    return {"조건": format_conditions_line(conditions), **turn.count(conditions)}


def _tool_segment_insight(turn, args):
    conditions, err = _parse_object(args.get("conditions_json"))
    if err or not conditions:
        return {"문제": err or "집단 조건이 비었어."}
    insight = summarize_segment_insight(turn.profile_df, conditions, turn.db_audience)
    if not insight or not insight.get('대상자수'):
        return {"결과": "그 조건에 해당하는 시청자가 없어. 조건을 다르게 해석해 다시 불러 보거나 실무자에게 물어봐."}
    return _jsonable(insight)


def _tool_content_ranking(turn, args):
    filters, err = _parse_object(args.get("filters_json"))
    if err:
        return {"문제": err}
    ranking = summarize_content_ranking(turn.db_audience, profile_df=turn.profile_df, conditions=filters or None,
                                        target=args.get("target") or '콘텐츠', order=args.get("order") or '인기')
    return _jsonable(ranking) if ranking and ranking.get('항목') else {"결과": "순위를 계산할 데이터가 없어."}


def _tool_group_breakdown(turn, args):
    filters, err = _parse_object(args.get("filters_json"))
    if err:
        return {"문제": err}
    breakdown = summarize_group_breakdown(turn.profile_df, args.get("field"), conditions=filters or None, db_audience=turn.db_audience)
    return _jsonable(breakdown) if breakdown and breakdown.get('그룹') else {"결과": "그 기준으로는 나눌 수 없어. 다른 기준(성별, 나이대, SO 등)을 써 봐."}


def _tool_check_data(turn, args):
    question = str(args.get("question") or "").strip()
    tables = turn.tables()
    if not question or tables is None:
        return {"오류": "확인할 내용이 비었거나 불러온 시청 데이터가 없어."}
    visible = _visible(turn.conditions)
    if visible:
        question += f"\n(참고: 지금 대화 중인 타겟 조건은 {json.dumps(visible, ensure_ascii=False)}예요. SO 값(대전·광주 등)은 8개 권역 이름이라 'SO권역' 기준으로 맞춰. 질문이 이 집단을 가리킬 때만 그 조건으로 좁혀 계산해.)"
    reply, error = quick_check(tables, question)
    return {"오류": error} if error else {"결과": reply['text'], "안내": "계산에 쓴 집단·기간을 밝혀서 알려줘. 데이터 컬럼 이름 같은 내부 용어는 쓰지 마."}


_TOOLS = {"set_conditions": _tool_set_conditions, "preview_count": _tool_preview_count, "segment_insight": _tool_segment_insight,
          "content_ranking": _tool_content_ranking, "group_breakdown": _tool_group_breakdown, "check_data": _tool_check_data}


def _pairs(messages):
    return [('user' if m.get('role') == 'user' else 'model', m.get('text', '')) for m in messages[-_MAX_HISTORY:]]


def _state_block(conditions, profile_df, db_audience):
    try:
        profile = summarize_profile_context(profile_df)
    except Exception:
        profile = "(시청자 프로필 요약을 불러오지 못함)"
    return (f"지금까지 잡은 타겟 조건: {format_conditions_line(conditions)}\n"
            f"{data_period_line(db_audience) if db_audience is not None else '(불러온 시청 데이터 없음)'}\n\n"
            f"시청자 프로필 데이터 요약:\n{profile}")


def process_target_turn(messages, conditions, user_text, profile_df, db_audience=None, db_content=None):
    """타겟 설정 대화 한 턴 처리. 반환: (new_messages, new_conditions)."""
    history = messages + [{"role": "user", "text": user_text}]
    turn = _Turn(conditions, profile_df, db_audience, db_content)
    system = get_target_agent_prompt(_state_block(conditions, profile_df, db_audience))
    text, api_error = run_agent(system, contents_from(_pairs(history)), _DECLARATIONS, _TOOLS, turn)
    if api_error and not turn.worked:
        return history + [{"role": "assistant", "text": api_error}], conditions
    reply = text or ("조건을 반영했어요. 이어서 말씀해 주세요." if turn.worked else _CONFUSED)
    return history + [{"role": "assistant", "text": reply}], turn.conditions


if __name__ == "__main__":
    import services.answer_check as _ac
    _ac.check_answer = lambda p: '{"문제": []}'   # 검수 AI는 테스트에서 부르지 않는다(문제 없음)
    import sys
    import pandas as pd
    import services.agent_loop as loop

    me = sys.modules[__name__]
    sent = []

    def script(*steps):
        it = iter(steps)

        def fake(system, contents, tool_declarations=None, temperature=0.3, force_tool=False):
            sent.append((system, list(contents), force_tool))
            step = next(it)
            if isinstance(step, tuple):
                return {"role": "model", "parts": [{"functionCall": {"name": step[0], "args": step[1]}}]}, None
            return {"role": "model", "parts": [{"text": step}]}, None
        loop.call_agent = fake

    profile = pd.DataFrame({'R고객번호': [str(i) for i in range(10)], '성별': ['여자'] * 6 + ['남자'] * 4, '나이': [45] * 4 + [62] * 2 + [45] * 4, '시청자SO': ['동대전방송'] * 10})
    me.summarize_profile_context = lambda p: "프로필 요약"
    me.describe_term_matches = lambda a, c: []
    me.describe_period_warnings = lambda a, c: []
    me.data_period_line = lambda a: ""

    def run(msgs, conds, text, **kw):
        return process_target_turn(msgs, conds, text, profile, **kw)

    # 1) 조건을 말하면 AI가 set_conditions를 불러 조건이 쌓이고 예상 인원이 돌아온다
    script(("set_conditions", {"changes_json": '{"성별": "여자", "나이대": ["40대"]}'}), "여성 40대로 잡았어요. 예상 4명이에요.")
    m1, c1 = run([], {}, "40대 여성 뽑아줘")
    assert c1 == {"성별": "여자", "나이대": ["40대"]} and m1[-1]["text"].startswith("여성 40대") and len(m1) == 2
    res = sent[-1][1][-1]["parts"][0]["functionResponse"]["response"]
    assert res["예상인원"] == 4 and res["전체시청자수"] == 10 and "성별" in res["현재조건"], res

    # 2) 빈 값은 기존 조건을 지키고, 삭제는 remove에 명시된 필드만, 모르는 필드는 무시하고 알려준다
    script(("set_conditions", {"changes_json": '{"성별": "", "나이대": [], "SO": ["대전"], "마음": "x"}'}),
           ("set_conditions", {"changes_json": "{}", "remove": ["나이"]}), "대전을 더했고 나이 조건은 뺐어요.")
    m2, c2 = run(m1, c1, "대전만, 나이는 빼줘")
    assert c2 == {"성별": "여자", "SO": ["대전"]}, c2
    assert sent[-2][1][-1]["parts"][0]["functionResponse"]["response"]["무시한필드"][0] == "마음"

    # 3) 질문에는 조건이 바뀌지 않고, 같은 조건을 다시 설정하려 하면 막는다
    script(("set_conditions", {"changes_json": '{"성별": "여자"}'}), "이미 그 조건이에요. 지금 4명이에요.")
    m3, c3 = run(m2, c2, "성별 조건 들어간 거야?")
    assert c3 == c2 and m3[-1]["text"].startswith("이미") and not sent[-1][1][-1]["parts"][0]["functionResponse"]["response"]["적용됨"]

    # 4) 가정 인원 확인은 조건을 바꾸지 않는다 / 조회 도구 결과는 numpy가 섞여도 AI에게 갈 수 있다
    script(("preview_count", {"what_if_json": '{"성별": "남자"}'}), "남자로 바꾸면 0명이에요.")
    m4, c4 = run(m2, c2, "남자로 하면 몇 명?")
    assert c4 == c2 and "예상인원" in sent[-1][1][-1]["parts"][0]["functionResponse"]["response"]
    import numpy as np
    assert _jsonable({"a": np.int64(3), "b": [np.float64(1.5)]}) == {"a": 3, "b": [1.5]}
    assert _parse_object("깨짐")[0] is None and _parse_object("[1]")[0] is None and _parse_object("")[0] == {}

    # 5) 내부 키('__')는 조건 요약에서 AI에게 보이지 않고 유지된다 / 도구만 부르다 끝나도 / 이해 못 한 말에도 사람 말로 답한다
    script("안녕하세요! 어떤 분들께 보내고 싶으세요?")
    m5, c5 = run([], {"__고객번호": ["1"]}, "안녕")
    assert c5 == {"__고객번호": ["1"]} and "__고객번호" not in sent[-1][0] and "(없음)" in sent[-1][0] or "없음" in sent[-1][0]
    script("")
    assert run([], {}, "ㅇㅇ")[0][-1]["text"] == _CONFUSED
    loop.call_agent = lambda *a, **k: (None, "⚠️ 서버 과부하")
    m6, c6 = run([], {"성별": "여자"}, "안녕")
    assert m6[-1]["text"].startswith("⚠️") and c6 == {"성별": "여자"}
    # 도구를 부르지 않고 "설정했어요"라고만 하면 버리고 도구를 반드시 부르게 한다
    script("조건을 설정했어요.", ("set_conditions", {"changes_json": '{"성별": "남자"}'}), "남자로 잡았어요.")
    m7, c7 = run([], {}, "남자만")
    assert c7 == {"성별": "남자"} and sent[-2][2] is True
    # 답변 검수: 문제가 나오면 AI에게 돌려줘 고쳐 쓰게 하고(검수 근거에 도구 결과가 들어간다), 고친 뒤에도 남으면 답 끝에 밝힌다
    verdicts = iter(['{"문제": [{"문장": "카피도 써 뒀어요", "이유": "이번에 카피를 쓴 기록이 없음"}]}', '{"문제": []}'])
    checked = []
    _ac.check_answer = lambda p: checked.append(p) or next(verdicts)
    script(("set_conditions", {"changes_json": '{"성별": "여자"}'}), "여자로 잡았어요. 카피도 써 뒀어요.", "여자로 잡았어요.")
    m8, c8 = run([], {}, "여자만")
    assert m8[-1]["text"] == "여자로 잡았어요." and "시스템 검수" in str(sent[-1][1][-1]) and "타겟 조건을 추가·수정·삭제" in checked[0] and "예상인원" in checked[0]
    _ac.check_answer = lambda p: '{"문제": [{"문장": "x", "이유": "근거 없음"}]}'
    script("대전은 999명이에요.", "대전은 999명이에요.")
    assert run([], {}, "대전 몇 명?")[0][-1]["text"].startswith("대전은 999명이에요.\n\n※ 다음 숫자는 데이터에서 확인되지 않았어요. 참고만 해 주세요: 999")
    _ac.check_answer = lambda p: '{"문제": []}'
    print("target_agent self-check OK")
