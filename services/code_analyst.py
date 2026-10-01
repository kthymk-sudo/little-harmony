# services/code_analyst.py
# ============================================================
# 📊 분석 탭 - AI 코드 실행형 분석 (Streamlit 비의존 - 단위 테스트 가능).
#
# 예전에는 AI가 정해진 메뉴(행 x 측정값 x 집계 1번)에서 고르기만 해서 메뉴에 없는 지표(재방문율),
# 여러 단계 분석(특이 SO 찾기 → 그 SO의 콘텐츠 파고들기), 묶는 규칙 같은 요청을 처리하지 못했다.
# 이제는 한 턴 안에서 아래를 최대 _MAX_STEPS번 반복한다:
#   AI가 pandas 코드 작성 → 시스템이 실제 데이터(복사본)로 실행 → 결과를 AI에게 보여줌
# AI가 충분하다고 판단하면 최종 답변(+ 그래프용 결과표 지정)을 쓴다. 숫자는 실행 결과에서만 나온다.
#
# 안전장치(services/code_sandbox.py): 코드는 import/파일·네트워크 접근/while/모듈 타고 들어가기가 막힌 채로,
# 허용된 함수만 담은 pd/np와 데이터 복사본만 있는 환경에서, 앱과 분리된 별도 프로세스(시간·메모리 제한)로 실행된다.
# 결과를 AI에게 보낼 때는 고객번호 컬럼을 가린다. 원본 DB는 건드릴 수 없다.
# ============================================================
import re

import numpy as np
import pandas as pd

from ai_engine.gemini_api import generate_code_analyst_step, is_api_error
from prompts.code_analyst_prompt import get_code_analyst_prompt, FEEDBACK_INSTRUCTION
from services import code_sandbox as sandbox
from services.code_sandbox import check_code, SandboxSession, SandboxUnavailable
from services.analysis_service import (
    _add_derived_columns, _with_content_attrs, _REGION_ORDER, _REGION_COLUMNS, _region_sort_key,
    CHART_TYPES, normalize_pivot_result,
)
from services.intent_gate import assess, with_notice, last_assistant_text
from utils.response_parser import parse_target_conditions

LIGHT_NOTE = "\n(타겟팅·보고서 화면에서 온 간단한 확인 질문이야: 계산은 평소처럼 정확하게 하고 위에 적힌 조건·기간·대상을 빠짐없이 적용해. 답은 필요한 수치만 짧게 하되 어떤 집단·기간을 기준으로 몇 명인지 밝혀. 그래프는 만들지 마.)"
_MAX_STEPS = 6            # 코드 실행 최대 5번 + 마지막 답변
_MAX_RETRIES = 3          # 형식이 틀리거나 결과를 지어낸 응답을 다시 요청하는 최대 횟수(턴 전체)
_DEEP_QUESTION = re.compile(r'특이|원인|왜|이유|유입|영향|때문|요인')
_DEEP_MIN_STEPS = 3       # 원인·특이사항 질문에 기대하는 최소 성공 계산 단계
_MAX_NUDGES = 2           # 그래도 바로 답하려 하면 되돌려 보내는 최대 횟수
_MAX_VERIFY_RETRIES = 2   # 실행 결과로 확인 안 되는 숫자가 있을 때 코드로 계산하게 되돌려 보내는 최대 횟수
_TOTAL_ROW_LABELS = {'합계', '전체', '총계', '총합', '소계', 'total', 'Total', 'TOTAL', '전체 합계'}
_TIMEOUT_SEC = 30
_PREVIEW_ROWS = 40
_HISTORY_TURNS = 8
_PRIVATE_COLS = ['이웃고객명', '게시자 아이디', '순번']   # AI가 볼 필요 없는 개인정보/내부 컬럼
_HIDDEN_IN_PREVIEW = ['R고객번호']                       # 결과를 AI에게 보낼 때 가리는 컬럼
FEEDBACK_REQUEST_TEXT = "🔍 이 결과를 전체와 비교해서 피드백해줘"
ISOLATED = True           # 분석 코드를 별도 프로세스에서 실행(시간·메모리 제한). 테스트/문제 확인 때만 False

# ------------------------------------------------------------
# 데이터 준비
# ------------------------------------------------------------
def build_tables(db_audience, db_content, profile_df):
    """AI 코드가 쓸 표 3개. 파생 컬럼(나이대/월/SO권역/등록월 등)을 미리 붙여 둔다."""
    views = db_audience.drop(columns=[c for c in _PRIVATE_COLS if c in db_audience.columns])
    views = _add_derived_columns(_with_content_attrs(views, db_content))
    views = views.drop(columns=['__전체', '__완료'], errors='ignore')
    contents = _add_derived_columns(db_content).drop(columns=['__전체', '__완료'], errors='ignore')
    customers = profile_df.drop(columns=[c for c in _PRIVATE_COLS if c in profile_df.columns])
    return {'시청': views, '콘텐츠': contents, '고객': customers}


def describe_tables(tables):
    """AI에게 알려줄 표 구조: 컬럼 이름, 형식, 값 예시."""
    lines = []
    notes = {'시청': '시청 1건 = 1행', '콘텐츠': '콘텐츠 1개 = 1행(누적 통계)', '고객': '고객 1명 = 1행(선호 목록은 리스트)'}
    for name, df in tables.items():
        lines.append(f"[{name}] {len(df):,}행 - {notes.get(name, '')}")
        for col in df.columns:
            s = df[col]
            if col in ('R고객번호', '콘텐츠ID'):
                sample = '식별자'
            elif s.map(lambda v: isinstance(v, list)).any():
                sample = f"리스트, 예: {next(v for v in s if isinstance(v, list))[:3]}"
            elif pd.api.types.is_numeric_dtype(s):
                sample = f"{s.min()} ~ {s.max()}" if s.notna().any() else '값 없음'
            else:
                uniq = s.dropna().astype(str).unique()
                shown = [u if len(u) <= 20 else u[:20] + '…' for u in uniq[:8]]
                sample = ', '.join(shown) + (f" 외 {len(uniq) - 8}개" if len(uniq) > 8 else '')
            lines.append(f"  - {col} ({s.dtype}): {sample}")
    lines.append(f"- SO권역순서 = {list(_REGION_ORDER)}")
    return "\n".join(lines)


# ------------------------------------------------------------
# 안전 실행 - 코드 검사/제한된 환경/별도 프로세스 격리는 services/code_sandbox.py
# ------------------------------------------------------------
def run_code(code, tables, previous):
    """같은 프로세스에서 실행(격리 프로세스를 못 쓸 때의 대체 경로이자 테스트용)."""
    return sandbox.execute(code, tables, previous, list(_REGION_ORDER), _TIMEOUT_SEC)


# 답변 속 숫자는 천 단위 쉼표(1,514)를 허용하고, 실행 결과(CSV·pandas 출력)는 쉼표가 구분자라 숫자만 읽는다
_NUMBER = re.compile(r'(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?')
_PLAIN_NUMBER = re.compile(r'\d+(?:\.\d+)?')


# 실행 결과의 숫자를 사람이 흔히 바꿔 쓰는 형태: 그대로, 비율→%(×100), %→비율(÷100), 초→분(÷60), 초→시간(÷3600)
_UNIT_TRANSFORMS = (1, 100, 0.01, 1 / 60, 1 / 3600)
_REL_TOLERANCE = 0.001   # 반올림 차이 허용(0.1%)
_ABS_TOLERANCE = 0.051   # 소수 첫째 자리 반올림 허용


def ungrounded_numbers(answer, evidence):
    """답변의 숫자 중 실행 결과(evidence)에서 확인되지 않는 것.
    반올림·% 변환·초→분/시간 변환은 확인된 것으로 본다. 연도·월·일 같은 작은 정수는 문맥상 흔해서 제외한다."""
    values = [float(t) for t in _PLAIN_NUMBER.findall(evidence)]
    candidates = np.unique(np.array([v * k for v in values for k in _UNIT_TRANSFORMS] or [np.nan]))
    missing = []
    for token in _NUMBER.findall(answer):
        value = float(token.replace(',', ''))
        if value <= 31 and value.is_integer() or 2000 <= value <= 2100:
            continue
        tolerance = max(_ABS_TOLERANCE, abs(value) * _REL_TOLERANCE)
        i = np.searchsorted(candidates, value)
        near = candidates[max(i - 1, 0):i + 1]
        if not np.any(np.abs(near - value) <= tolerance):
            missing.append(token)
    return missing


def _as_frame(value):
    if isinstance(value, pd.Series):
        value = value.to_frame()
    if isinstance(value, pd.DataFrame):
        return value.reset_index() if not isinstance(value.index, pd.RangeIndex) else value
    return None


def preview(value, limit=_PREVIEW_ROWS):
    """실행 결과를 AI/화면에 보여줄 문자열로. 고객번호 컬럼은 가린다."""
    df = _as_frame(value)
    if df is None:
        return str(value)[:3000]
    df = df.drop(columns=[c for c in _HIDDEN_IN_PREVIEW if c in df.columns])
    head = f"(표 {len(df):,}행 x {df.shape[1]}열" + (f", 앞 {limit}행만)" if len(df) > limit else ")")
    return head + "\n" + df.head(limit).to_csv(index=False)


# ------------------------------------------------------------
# 결과표 → 그래프용 pivot_result (ui/chart_render가 그리는 형식)
# ------------------------------------------------------------
def table_to_pivot_result(value, graph):
    """AI가 지정한 결과표와 그래프 설정을 기존 차트 형식으로 바꾼다. 쓸 수 없으면 None."""
    df = _as_frame(value)
    if df is None or df.empty or not isinstance(graph, dict):
        return None
    df = df.copy()
    x = [c for c in (graph.get('x') or []) if c in df.columns][:2]
    values = [c for c in (graph.get('값') or []) if c in df.columns][:3]
    if not x or not values:
        return None
    # "합계/전체" 행은 다른 항목보다 훨씬 커서 막대 하나가 그래프를 덮어버린다 - 그래프에서는 뺀다
    df = df[~df[x].astype(str).apply(lambda col: col.str.strip()).isin(_TOTAL_ROW_LABELS).any(axis=1)]
    if df.empty:
        return None
    units = {k: v for k, v in (graph.get('단위') or {}).items() if k in values}
    chart_type = graph.get('차트유형') if graph.get('차트유형') in CHART_TYPES else '막대'
    series_col = graph.get('계열') if graph.get('계열') in df.columns and graph.get('계열') not in x else None
    for c in df.columns:  # 날짜 등은 JSON 저장/그래프 라벨용 문자열로
        if pd.api.types.is_datetime64_any_dtype(df[c]) or isinstance(df[c].dtype, pd.PeriodDtype):
            df[c] = df[c].astype(str)

    change = None
    ch = graph.get('증감') or {}
    base, comp = ch.get('기준값'), ch.get('비교값')
    if chart_type == '증감' and base in df.columns and comp in df.columns and base != comp:
        m = values[0]
        rate = ch.get('증감률') if ch.get('증감률') in df.columns and ch.get('증감률') not in (base, comp) else None
        # AI가 값 컬럼으로 기준/비교/증감률 컬럼을 그대로 가리키면(같은 컬럼이 두 번 쓰여 그래프가 깨짐)
        # 증감을 직접 계산한 별도 컬럼으로 쓴다
        diff = m
        if m in (base, comp, rate):
            diff = '증감' if '증감' not in (base, comp, rate) else '증감(계산)'
            df[diff] = pd.to_numeric(df[comp], errors='coerce') - pd.to_numeric(df[base], errors='coerce')
        flat = df[list(dict.fromkeys(x + [base, comp, diff] + ([rate] if rate else [])))]
        series = {diff: [base, comp]}
        labels = {base: base, comp: comp}
        change = {'기준': base, '비교': comp,
                  '측정값': {diff: {'기준': base, '비교': comp, '증감': diff, '증감률': rate}}}
        units = {diff: units.get(m, '')}
    else:
        if chart_type == '증감':
            chart_type = '막대'
        if series_col:
            wide = df.pivot_table(index=x, columns=series_col, values=values, aggfunc='sum', sort=False)
            wide.columns = [f"{s} ({v})" if len(values) > 1 else str(s) for v, s in wide.columns]
            groups = df[series_col].drop_duplicates().tolist()
            if series_col in _REGION_COLUMNS:
                groups = sorted(groups, key=lambda g: _region_sort_key(pd.Series([g], name=series_col))[0])
            series = {v: [f"{g} ({v})" if len(values) > 1 else str(g) for g in groups] for v in values}
            labels = {f"{g} ({v})" if len(values) > 1 else str(g): str(g) for v in values for g in groups}
            flat = wide.reset_index()
            flat = flat[x + [n for names in series.values() for n in names if n in flat.columns]]
        else:
            flat = df[x + values]
            series = {v: [v] for v in values}
            labels = {v: v for v in values}

    flat = flat.astype(object).where(flat.notna(), None)
    return {
        '분석대상': '코드', '데이터기준': graph.get('기준설명') or '', '행': x, '열': [series_col] if series_col else [],
        '측정값': list(series.keys()), '단위': units, '차트유형': chart_type, '증감': change,
        '행컬럼': x, '계열': series, '계열라벨': labels, '전체행수': len(flat), '결과': flat.to_dict('records'),
    }


# ------------------------------------------------------------
# 한 턴 진행
# ------------------------------------------------------------
_CODE_BLOCK = re.compile(r'```(?:python|py)\s*\n(.*?)```', re.DOTALL)


def _history_str(messages):
    lines = []
    for m in messages[-_HISTORY_TURNS:]:
        if m.get('role') != 'user' and is_api_error(m.get('text', '')):
            continue
        speaker = "실무자" if m.get('role') == 'user' else "AI"
        text = m.get('text', '')
        if m.get('steps'):  # 후속 질문("그럼 대전만")을 이어갈 수 있게 마지막 계산 코드를 같이 보여준다
            last_code = next((s['code'] for s in reversed(m['steps']) if s.get('code')), '')
            if last_code:
                text += f"\n[이 답변의 마지막 계산 코드]\n{last_code[:1500]}"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines) or "(아직 대화 없음)"


_LOG_MARK = "◆시스템실행"  # AI가 흉내 내면 곧바로 드러나는 표식(응답에 나오면 무효)


def _steps_str(steps):
    if not steps:
        return "(아직 없음)"
    parts = []
    for s in steps:
        body = f"{_LOG_MARK} #{s['번호']} ({s['설명']})\n실행한 코드:\n{s['code']}\n"
        if s.get('printed'):
            body += f"print 출력:\n{s['printed']}\n"
        body += f"실행 오류: {s['error']}" if s.get('error') else f"`결과` 값:\n{s['preview']}"
        parts.append(body + f"\n{_LOG_MARK} #{s['번호']} 끝")
    return "\n\n".join(parts)


# 🌟 [버그 수정] "결과: 대전이 890명…"처럼 정상 답변에도 쓰는 "결과:" 줄까지 가짜로 몰던 것을 좁혔다.
# 시스템 실행 기록의 표식과 "[2단계]" 머리말, 그리고 뒤에 내용 없이 "코드:"/"출력:"/"결과:"만 적은 줄
# (실행 결과 표가 이어질 자리)만 가짜 실행 결과의 흔적으로 본다. 숫자를 지어낸 경우는 숫자 대조가 따로 막는다.
_FABRICATION_SIGNS = re.compile(
    rf"{_LOG_MARK}|\[\d+\s*단계\]|^\s*(코드|출력|결과|실행한 코드|print 출력|실행 오류)\s*:\s*$", re.MULTILINE
)

# 🌟 [버그 수정] "피드백 많은 콘텐츠 알려줘"처럼 "피드백"이라는 단어만 있어도 미시/거시 피드백으로 답하던 것을,
# 전체와 비교한 피드백을 요청하는 표현일 때만 그렇게 하도록 좁혔다(버튼은 따로 확실히 지정).
_FEEDBACK_REQUEST = re.compile(
    r"(전체.{0,6}비교|비교.{0,6}전체).{0,12}(피드백|평가|시사점)"
    r"|(피드백|평가|시사점).{0,6}(줘|해\s*줘|남겨|부탁|주세요|달라)"
    r"|미시.{0,6}거시|거시.{0,6}미시"
)


def is_feedback_request(text):
    return bool(_FEEDBACK_REQUEST.search(text or ""))


def _invalid_reason(raw, has_code):
    """AI 응답이 쓸 수 없는 이유(없으면 None). 실행하지 않은 결과를 지어내 적은 응답을 걸러낸다."""
    body = _CODE_BLOCK.sub('', raw)
    if _FABRICATION_SIGNS.search(body):
        return "실행하지 않은 코드 결과나 다음 단계를 네가 직접 적었어. 코드블록 하나만 쓰고 멈추거나, 실행 기록의 숫자로만 최종 답변을 써."
    if not has_code and '```json' not in raw:
        return "최종 답변 끝에 JSON 코드블록이 없어. 형식대로 다시 써."
    return None


def _last_data_message(messages):
    return next((m for m in reversed(messages) if m.get('role') == 'assistant' and m.get('data')), None)


_UNVERIFIED_TAIL = re.compile(r"\n\n⚠️ 이 답변의 숫자 중 .*?대조해서 봐주세요\.", re.DOTALL)


def _evidence_text(messages, steps, user_text):
    """🌟 [버그 수정] 답변 속 숫자를 대조할 근거. 예전에는 이전 답변의 글 전체를 근거로 삼아서, 이전 답변에서
    "확인 안 됨"으로 표시된 숫자가 다음 질문에서는 근거로 통과했다. 이제는 이전 답변이 실제로 계산한 결과
    (실행 기록)만 근거로 쓰고, 실행 기록이 없는 메시지(인사·오류 등)는 글에서 확인 안 됨 표시를 뺀 채로 쓴다."""
    parts = [s.get('preview', '') + "\n" + s.get('printed', '') for s in steps]
    for m in messages:
        if m.get('steps'):
            parts += [s.get('preview', '') + "\n" + s.get('printed', '') for s in m['steps']]
        elif m.get('role') == 'user' or not m.get('unverified'):
            parts.append(_UNVERIFIED_TAIL.sub('', m.get('text', '')))
    parts.append(user_text)
    return "\n".join(parts)


class _StepRunner:
    """코드 한 단계를 실행한다. 기본은 별도 프로세스(시간·메모리 제한, 시간 초과 시 실제로 종료).
    프로세스를 시작하지 못하는 환경이면 같은 프로세스 실행으로 대신하고 그 사실을 남긴다."""

    def __init__(self, tables):
        self.tables, self.session, self.isolated, self.fallback_note = tables, None, ISOLATED, None

    def run(self, code, step_number, values):
        if self.isolated:
            try:
                if self.session is None:
                    self.session = SandboxSession(self.tables, _REGION_ORDER, timeout=_TIMEOUT_SEC)
                return self.session.run(code, step_number)
            except SandboxUnavailable as e:
                self.isolated = False
                self.fallback_note = f"격리 실행을 시작하지 못해 같은 프로세스에서 실행했어요 ({e})"
        return run_code(code, self.tables, values)

    def close(self):
        if self.session is not None:
            self.session.close()


def run_analyst_turn(messages, user_text, tables, on_step=None, feedback=False, gate=True):
    """한 턴 처리. 반환: 새 assistant 메시지 dict
       {'text', 'steps': [{'번호','설명','code','preview'|'error','printed'}], 'data'(그래프용, 선택), 'chart'(선택),
        'audience'(타겟팅으로 보낼 수 있는 집단, 선택)}
    🌟 gate: 먼저 질문의 뜻·의도를 판단한다(되묻기·안내·거절은 계산 없이 그 답만). 이미 판단을 거친 질문(타겟팅에서 넘어온 것)과
    시스템이 만든 비교 피드백 요청(feedback)은 다시 판단하지 않는다."""
    verdict = {"판단": "진행", "주의": ""}
    if gate and not feedback:
        verdict = assess('analysis', user_text, last_assistant_text(messages), "시청 데이터 분석 대화")
        if verdict['판단'] != '진행':
            return {'role': 'assistant', 'text': verdict['답변']}
    runner = _StepRunner(tables)
    try:
        reply = _run_turn(messages, user_text, tables, on_step, feedback, runner)
    finally:
        runner.close()
    reply['text'] = with_notice(reply['text'], verdict)
    return reply


def _run_turn(messages, user_text, tables, on_step, feedback, runner):
    schema = describe_tables(tables)
    history = _history_str(messages)
    steps, values, audiences = [], {}, {}
    valid_ids = set(tables['고객']['R고객번호'].astype(str)) if '고객' in tables else None
    correction, retries, nudges, verify_retries = "", 0, 0, 0
    checks = []  # 답변 검증 기록(지어낸 숫자 차단 등) - "계산 과정 보기"에 같이 보여준다
    unverified = []
    i = 0
    while i < _MAX_STEPS:
        prompt = get_code_analyst_prompt(schema, history, user_text, _steps_str(steps), i == _MAX_STEPS - 1,
                                         FEEDBACK_INSTRUCTION if feedback else "", correction)
        raw = generate_code_analyst_step(prompt)
        if is_api_error(raw):
            return {'role': 'assistant', 'text': raw, 'steps': steps}
        code_match = _CODE_BLOCK.search(raw)
        has_code = bool(code_match) and i < _MAX_STEPS - 1
        # 코드 앞부분(할 일 설명)만 검사한다 - 코드블록 뒤에 지어낸 결과가 붙어 있어도 코드만 쓰고 나머지는 버린다
        reason = _invalid_reason(raw[:code_match.end()] if has_code else raw, has_code)
        # "다시 해봐" 요청(nudge) - 무시해도 답변을 버릴 정도는 아니므로, 정해진 횟수를 넘기면 그 답변을 받는다
        nudge, nudge_label = None, ""
        unverified = []
        if not has_code and not reason:
            # 🌟 최종 답변의 숫자가 실제 실행 결과(또는 이전 답변)에 있는지 대조 - 지어낸 숫자 차단
            evidence = _evidence_text(messages, steps, user_text)
            missing = ungrounded_numbers(parse_target_conditions(_CODE_BLOCK.sub('', raw))[0], evidence)
            if missing and verify_retries < _MAX_VERIFY_RETRIES:
                verify_retries += 1
                nudge_label = f"확인 안 된 숫자({', '.join(missing[:6])}) → 코드로 계산하도록 다시 요청"
                nudge = (f"답변의 숫자 {', '.join(missing[:6])}가 실행 결과에서 확인되지 않아(차이·비율·합계를 머릿속으로 "
                         f"계산했다면 그것도 해당돼). 이 값들을 코드로 계산해 `결과`에 담아 확인해. "
                         f"이번 응답은 최종 답변이 아니라 반드시 ```python 코드블록```이어야 해.")
            elif missing:
                unverified = missing  # 그래도 안 되면 답변은 보여주되, 확인 안 된 숫자를 표시한다
            elif (nudges < _MAX_NUDGES and _DEEP_QUESTION.search(user_text)
                  and sum('preview' in s for s in steps) < _DEEP_MIN_STEPS):
                # 원인·특이사항 질문인데 너무 일찍 끝내려 하면 더 파고들게 한다
                nudges += 1
                nudge_label = "더 파고들도록 다시 요청"
                nudge = ("원인·특이사항을 묻는 질문인데 근거 계산이 부족해. 전월/다른 권역/전체와 비교해 튀는 곳을 찾고, "
                         "그곳의 원인을 한 단계 더 파고드는 코드를 실행해(예: 신규 시청자가 처음 본 콘텐츠, 그 권역에서만 "
                         "유독 많이 본 콘텐츠의 전체 대비 비중). 이번 응답은 최종 답변이 아니라 반드시 ```python 코드블록```이어야 해.")
        if reason and retries < _MAX_RETRIES:
            correction, retries = reason, retries + 1
            checks.append(f"다시 요청: {reason}")
            continue  # 같은 단계를 다시 요청(단계 수는 늘리지 않음)
        if nudge:
            correction = nudge
            checks.append(nudge_label)
            continue
        correction = ""
        i += 1
        if has_code:
            code = code_match.group(1).strip()
            plan = raw[:code_match.start()].strip().splitlines()
            first_comment = next((ln.strip('# ').strip() for ln in code.splitlines() if ln.strip().startswith('#')), '')
            description = (plan[-1] if plan else first_comment or '계산').strip()[:120]
            step = {'번호': len(steps) + 1, '설명': description, 'code': code}
            if on_step:
                on_step(step['번호'], step['설명'])
            run = runner.run(step['code'], step['번호'], values)
            if runner.fallback_note and runner.fallback_note not in checks:
                checks.append(runner.fallback_note)
            step['printed'] = run.get('printed', '')
            if 'error' in run:
                step['error'] = run['error']
            else:
                values[step['번호']] = run['value']
                step['preview'] = preview(run['value'])
                ids = run.get('audience')
                if ids:  # 🌟 코드가 `대상자`에 담은 집단 - 실제 시청자만 남긴다(타겟팅으로 보낼 수 있다)
                    ids = [v for v in ids if valid_ids is None or v in valid_ids]
                    if ids:
                        audiences[step['번호']] = ids
                        step['audience_count'] = len(ids)
                        step['preview'] += f"\n[`대상자`: 고객 {len(ids):,}명이 지정됨]"
            steps.append(step)
            continue
        if reason:  # 다시 요청해도 지어낸 결과가 섞여 있으면 그 답변은 보여주지 않는다
            checks.append(f"답변 폐기: {reason}")
            return {'role': 'assistant', 'steps': steps, 'checks': checks,
                    'text': "계산은 했지만 답변을 믿을 수 있게 정리하지 못했어요. 아래 '계산 과정 보기'에서 실제 계산 "
                            "결과를 확인하시거나, 질문을 조금 나눠서 다시 물어봐주세요."}
        message = {**_final_message(raw, steps, values, messages, audiences), 'checks': checks}
        if unverified:
            message['text'] += (f"\n\n⚠️ 이 답변의 숫자 중 {', '.join(unverified[:6])}은(는) 실행 결과로 확인되지 않았어요. "
                                f"'계산 과정 보기'의 실제 결과와 대조해서 봐주세요.")
            message['unverified'] = list(unverified)
            checks.append(f"확인 안 된 숫자 표시: {', '.join(unverified[:6])}")
        return message
    return {'role': 'assistant', 'text': "분석을 마무리하지 못했어요. 질문을 조금 나눠서 다시 물어봐주세요.",
            'steps': steps, 'checks': checks}


_MAX_TABLE_ROWS, _MAX_TABLE_COLS = 2000, 30


def _table_records(value):
    """🌟 [표 다운로드] 답변의 핵심 결과표를 대화에 함께 저장할 수 있는 형태로(고객번호 컬럼 제외, 크기 제한). 표가 아니면 None."""
    df = _as_frame(value)
    if df is None or df.empty:
        return None
    df = df.drop(columns=[c for c in _HIDDEN_IN_PREVIEW if c in df.columns]).iloc[:_MAX_TABLE_ROWS, :_MAX_TABLE_COLS].copy()
    for c in df.columns:
        if not (pd.api.types.is_numeric_dtype(df[c]) or pd.api.types.is_bool_dtype(df[c])):
            df[c] = df[c].astype(str).where(df[c].notna(), None)
    df.columns = [str(c) for c in df.columns]
    return df.astype(object).where(df.notna(), None).to_dict('records')


def _final_message(raw, steps, values, messages, audiences=None):
    text, spec = parse_target_conditions(_CODE_BLOCK.sub('', raw))
    message = {'role': 'assistant', 'text': text or "분석을 마쳤어요.", 'steps': steps}
    # 🌟 [분석 → 타겟 연결] 최종 답변이 "이 집단"을 가리킬 때만(JSON의 "대상자") 타겟팅으로 보낼 수 있게 붙인다
    audience_spec = spec.get('대상자') if isinstance(spec.get('대상자'), dict) else None
    if audience_spec and audiences:
        step_no = audience_spec.get('단계') if audience_spec.get('단계') in audiences else max(audiences)
        message['audience'] = {'설명': str(audience_spec.get('설명') or '분석에서 찾은 집단')[:80],
                               'ids': audiences[step_no], 'count': len(audiences[step_no])}
    graph = spec.get('그래프') if isinstance(spec.get('그래프'), dict) else None
    if graph:
        value = values.get(graph.get('단계')) if graph.get('단계') in values else (values[max(values)] if values else None)
        try:
            data = table_to_pivot_result(value, graph)
        except Exception:  # AI가 지정한 표 모양이 그래프로 바꿀 수 없는 형태면 그래프만 생략(답변은 그대로)
            data = None
        if data:
            message['data'] = data
    # 답변의 핵심 결과표(그래프로 가리킨 단계, 없으면 마지막으로 표를 낸 단계)를 다운로드할 수 있게 함께 담는다
    table_step = (graph or {}).get('단계') if (graph or {}).get('단계') in values else None
    if table_step is None:
        table_step = next((n for n in sorted(values, reverse=True) if _as_frame(values[n]) is not None), None)
    if table_step is not None:
        records = _table_records(values[table_step])
        if records:
            message['table'] = records
    if spec.get('그래프요청') is True:
        source = message.get('data') or (_last_data_message(messages) or {}).get('data')
        if source:
            message['data'] = source
            message['chart'] = {"type": "pivot", "data": normalize_pivot_result(source)}
        else:
            message['text'] += "\n\n(그래프로 만들 결과표가 없어서 그래프는 생략했어요.)"
    return message


if __name__ == "__main__":
    import json
    import services.intent_gate as _gate

    _verdicts = []   # 요청 판단: 따로 정하지 않으면 진행
    _gate._ask = lambda prompt: _verdicts.pop(0) if _verdicts else '{"판단": "진행"}'

    ISOLATED = False  # 아래 흐름 테스트는 같은 프로세스에서(빠르게). 격리 프로세스는 마지막에 따로 실제로 띄워서 확인한다
    views = pd.DataFrame({
        'R고객번호': ['1', '2', '1', '3', '4'], '이웃고객명': ['가', '나', '가', '다', '라'], '콘텐츠ID': ['a'] * 5,
        '시청자SO': ['㈜씨엠비', '㈜씨엠비동대전방송', '㈜씨엠비', '㈜씨엠비대구방송', '㈜씨엠비수성방송'],
        '시청일': ['2026-07-01', '2026-07-02', '2026-08-01', '2026-08-02', '2026-08-03'], '시청 유지율': [50.0] * 5,
    })
    content = pd.DataFrame({'콘텐츠ID': ['a'], '업로더SO': ['본부'], '등록일': ['2026-01-01'], '삭제 여부': ['X']})
    profile = pd.DataFrame({'R고객번호': ['1', '2', '3', '4'], '이웃고객명': ['가', '나', '다', '라'], '선호장르': [['드라마']] * 4})
    tables = build_tables(views, content, profile)
    assert '이웃고객명' not in tables['시청'] and '이웃고객명' not in tables['고객'], "개인정보 컬럼은 AI에게 안 보인다"
    assert set(tables['시청']['SO권역']) == {'대전', '대구'} and '리스트' in describe_tables(tables)

    # 안전장치
    for bad in ["import os", "open('x')", "시청.to_csv('x')", "pd.read_csv('x')", "().__class__", "while True: pass",
                "getattr(pd, 'eval')", "시청.query('a>1')"]:
        assert check_code(bad), bad
    assert 'error' in run_code("결과 = 1/0", tables, {})
    # 모듈을 타고 os에 닿는 경로(점검에서 발견)가 막혔는지 - 별칭을 써도 막혀야 한다
    for escape in ["결과 = str(pd.io.common.os)", "p = pd\n결과 = str(p.io)", "결과 = str(np.lib)",
                   "결과 = 시청.values.dump('x')", "결과 = list(range(10**9))"]:
        assert 'error' in run_code(escape, tables, {}), escape
    assert run_code("결과 = pd.to_datetime(pd.Series(['2026-08-01'])).dt.month.sum() + np.sqrt(4)", tables, {})['value'] == 10
    ok = run_code("m = 시청.groupby(['월','SO권역'])['R고객번호'].nunique().reset_index(name='MAU')\nprint(len(m))\n결과 = m", tables, {})
    assert ok['printed'].strip() == '3' and 'R고객번호' not in preview(ok['value'])  # 7월 대구 시청 없음
    assert tables['시청'].shape[0] == 5, "AI 코드가 원본 표를 바꾸지 못한다"

    # 재방문율(전월 시청자 중 이번 달도 본 비율): 대전 7월 {1,2} → 8월 {1} = 50%
    code = """
m = 시청.groupby('월')['R고객번호'].apply(set)
jul, aug = m['2026-07'], m['2026-08']
결과 = pd.DataFrame({'구분': ['재방문율'], '값': [round(len(jul & aug) / len(jul) * 100, 1)]})
"""
    assert run_code(code, tables, {})['value']['값'][0] == 50.0

    # 결과표 → 그래프 형식: 긴 형태(월 계열) → 넓은 형태, 권역 순서 유지, JSON 저장 가능
    long = ok['value']
    data = table_to_pivot_result(long, {'x': ['SO권역'], '값': ['MAU'], '계열': '월', '차트유형': '막대', '단위': {'MAU': '명'}})
    assert data['계열'] == {'MAU': ['2026-07', '2026-08']} and [r['SO권역'] for r in data['결과']] == ['대전', '대구']
    wide = pd.DataFrame({'SO권역': ['대전', '대구'], '7월': [2, 0], '8월': [1, 2], '증감': [-1, 2]})
    change = table_to_pivot_result(wide, {'x': ['SO권역'], '값': ['증감'], '차트유형': '증감', '증감': {'기준값': '7월', '비교값': '8월'}})
    assert change['증감']['측정값']['증감']['증감'] == '증감' and change['차트유형'] == '증감'
    json.dumps([data, change], ensure_ascii=False)
    # 에러 재현: 값 컬럼을 증감률 컬럼과 같게 지정 → 증감은 따로 계산하고, 그래프까지 그려져야 한다
    rate_df = wide.assign(증감률=[-50.0, None])
    same = table_to_pivot_result(rate_df, {'x': ['SO권역'], '값': ['증감률'], '차트유형': '증감',
                                           '증감': {'기준값': '7월', '비교값': '8월', '증감률': '증감률'}})
    info = same['증감']['측정값']['증감']
    assert info['증감률'] == '증감률' and [r['증감'] for r in same['결과']] == [-1, 2]
    from ui.chart_render import build_pivot_chart_figure
    assert build_pivot_chart_figure({'type': 'pivot', 'data': same}) is not None
    # 이미 저장된 깨진 형식(같은 컬럼이 증감·증감률에 둘 다)도 그려진다
    broken = {**change, '증감': {'기준': '7월', '비교': '8월', '측정값': {'증감': {'기준': '7월', '비교': '8월', '증감': '증감', '증감률': '증감'}}}}
    assert build_pivot_chart_figure({'type': 'pivot', 'data': broken}) is not None
    # 기준과 비교가 같은 컬럼이면 증감 대신 막대
    assert table_to_pivot_result(wide, {'x': ['SO권역'], '값': ['8월'], '차트유형': '증감',
                                        '증감': {'기준값': '8월', '비교값': '8월'}})['차트유형'] == '막대'

    # 한 턴 흐름(AI는 가짜): 코드 1번 실행 → 최종 답변 + 그래프 지정
    replies = iter([
        "권역별 MAU를 계산\n```python\n결과 = 시청.groupby('SO권역')['R고객번호'].nunique().reset_index(name='MAU')\n```",
        '대전 2명, 대구 2명이에요.\n```json\n{"그래프": {"단계": 1, "x": ["SO권역"], "값": ["MAU"], "차트유형": "막대"}, "그래프요청": false}\n```',
    ])
    globals()['generate_code_analyst_step'] = lambda prompt: next(replies)
    msg = run_analyst_turn([], "SO별 MAU", tables)
    assert msg['text'] == '대전 2명, 대구 2명이에요.' and msg['data']['결과'] and 'chart' not in msg
    assert msg['steps'][0]['설명'] == '권역별 MAU를 계산' and 'value' not in msg['steps'][0]
    json.dumps(msg, ensure_ascii=False)
    # "그래프로 보여줘": 계산 없이 직전 결과표로
    globals()['generate_code_analyst_step'] = lambda prompt: '그래프로 보여드릴게요.\n```json\n{"그래프": null, "그래프요청": true}\n```'
    graph_msg = run_analyst_turn([{'role': 'user', 'text': 'SO별 MAU'}, msg], "그래프로 보여줘", tables)
    assert graph_msg['chart']['data']['결과'] == msg['data']['결과'] and not graph_msg['steps']
    # 오류가 나면 AI가 다음 단계에서 고칠 수 있게 오류를 넘긴다
    replies = iter(["```python\n결과 = 시청['없는컬럼']\n```", "```python\n결과 = len(시청)\n```", "5건이에요.\n```json\n{\"그래프\": null}\n```"])
    globals()['generate_code_analyst_step'] = lambda prompt: next(replies)
    fixed = run_analyst_turn([], "몇 건?", tables)
    assert 'KeyError' in fixed['steps'][0]['error'] and fixed['steps'][1]['preview'] == '5' and fixed['text'] == '5건이에요.'
    # 실행하지 않은 결과를 지어내 적으면 무효 → 이유를 알려 다시 요청
    prompts_seen = []
    replies = iter([
        "[2단계] 다음 분석\n코드:\nx = 1\n출력:\n  대전 999\n대전은 999명이에요.\n```json\n{\"그래프\": null}\n```",
        "5건이에요.\n```json\n{\"그래프\": null}\n```",
    ])
    globals()['generate_code_analyst_step'] = lambda prompt: prompts_seen.append(prompt) or next(replies)
    honest = run_analyst_turn([], "몇 건?", tables)
    assert honest['text'] == '5건이에요.' and '무효 처리' in prompts_seen[1]
    # 끝까지 지어내면 그 답변은 버리고 안전한 안내
    globals()['generate_code_analyst_step'] = lambda prompt: "출력:\n대전 999\n```json\n{\"그래프\": null}\n```"
    assert '999' not in run_analyst_turn([], "몇 건?", tables)['text']
    # 실행 결과에 없는 숫자를 쓰면 무효(표식 없이 숫자만 지어낸 경우), 계산한 숫자/반올림/연도·월은 통과
    assert ungrounded_numbers("대전 MAU는 1,514명", "SO권역,MAU\n대전,890") == ['1,514']
    assert ungrounded_numbers("대전 890명, 재방문율 17.5%, 2026년 8월", "대전,890,17.51") == []
    # 반올림·%·초→분 변환은 확인된 것으로 본다(스크린샷의 "답변 폐기" 원인)
    assert ungrounded_numbers("평균 645.67명(18.69%), 비중 12.3%, 시청 237.4분", "645.6666666,0.123,14245,18.69") == []
    replies = iter(["대전은 1,514명이에요.\n```json\n{\"그래프\": null}\n```", "5건이에요.\n```json\n{\"그래프\": null}\n```"])
    globals()['generate_code_analyst_step'] = lambda prompt: next(replies)
    assert run_analyst_turn([], "몇 건?", tables)['text'] == '5건이에요.'
    # 계속 확인 안 되는 숫자를 쓰면 답변을 버리지 않고, 그 숫자를 표시해서 보여준다
    globals()['generate_code_analyst_step'] = lambda prompt: "대전은 1,514명이에요.\n```json\n{\"그래프\": null}\n```"
    flagged = run_analyst_turn([], "몇 명?", tables)
    assert flagged['text'].startswith("대전은 1,514명이에요.") and "확인되지 않았어요" in flagged['text']
    # 그래프에서 합계 행은 뺀다
    with_total = pd.DataFrame({'SO권역': ['대전', '대구', '합계'], 'MAU': [2, 2, 4]})
    assert [r['SO권역'] for r in table_to_pivot_result(with_total, {'x': ['SO권역'], '값': ['MAU']})['결과']] == ['대전', '대구']
    assert run_code("x = 1", tables, {})['error'].startswith("`결과`"), "결과를 안 담으면 오류로 알려준다"
    assert run_code("result = 1", tables, {})['value'] == 1, "영어 이름 result도 결과로 받는다"
    assert run_code("import pandas as pd\n결과 = pd.Series([1]).sum()", tables, {})['value'] == 1
    # 오류 1: 정상 답변의 "결과: 대전이…" 줄은 통과, 내용 없는 "출력:" 줄과 "[2단계]"는 여전히 가짜로 본다
    assert _invalid_reason("분석을 마쳤어요.\n결과: 대전이 890명으로 가장 많아요.\n```json\n{\"그래프\": null}\n```", False) is None
    assert _invalid_reason("출력:\n대전 999\n```json\n{}\n```", False) and _invalid_reason("[2단계] 다음\n```json\n{}\n```", False)
    # 오류 2: 이전 답변에서 "확인 안 됨"으로 표시된 숫자는 다음 질문의 근거로 쓰이지 않는다
    prev = [{'role': 'assistant', 'text': "대전 1,514명이에요.\n\n⚠️ 이 답변의 숫자 중 1,514은(는) 실행 결과로 확인되지 않았어요. '계산 과정 보기'의 실제 결과와 대조해서 봐주세요.",
             'unverified': ['1,514']}, {'role': 'assistant', 'text': "890명", 'steps': [{'preview': "대전,890", 'printed': ''}]}]
    assert ungrounded_numbers("대전 1,514명", _evidence_text(prev, [], "다시 알려줘")) == ['1,514']
    assert ungrounded_numbers("대전 890명", _evidence_text(prev, [], "다시 알려줘")) == []
    # 오류 3: "피드백"이라는 단어만으로는 피드백 모드가 아니다
    assert not is_feedback_request("피드백 많은 콘텐츠 알려줘") and not is_feedback_request("댓글 피드백이 많은 영상은?")
    assert is_feedback_request("이거 전체랑 비교해서 피드백 줘") and is_feedback_request("미시 거시 관점으로 평가해줘")
    assert is_feedback_request(FEEDBACK_REQUEST_TEXT) and is_feedback_request("피드백 해줘")
    # 원인 질문인데 너무 일찍 끝내면 한 번 더 파고들게 하고, 그래도 끝내면 그 답변을 받는다
    prompts_seen = []
    replies = iter(["5건이에요.\n```json\n{\"그래프\": null}\n```"] * 3)
    globals()['generate_code_analyst_step'] = lambda prompt: prompts_seen.append(prompt) or next(replies)
    assert run_analyst_turn([], "왜 늘었어?", tables)['text'] == '5건이에요.' and '근거 계산이 부족' in prompts_seen[2]
    assert len(prompts_seen) == 1 + _MAX_NUDGES
    # 코드블록 뒤에 지어낸 결과가 붙어 와도 코드만 실행하고 나머지는 버린다
    replies = iter(["건수 계산\n```python\n결과 = len(시청)\n```\n출력:\n999", "5건이에요.\n```json\n{\"그래프\": null}\n```"])
    globals()['generate_code_analyst_step'] = lambda prompt: next(replies)
    trimmed = run_analyst_turn([], "몇 건?", tables)
    assert trimmed['steps'][0]['preview'] == '5' and trimmed['text'] == '5건이에요.'
    # 분석 → 타겟 연결: 코드가 `대상자`에 담은 집단은 최종 답변이 가리킬 때만 message['audience']로 붙는다
    replies = iter([
        "대전 시청자를 찾을게\n```python\n결과 = 시청.groupby('SO권역')['R고객번호'].nunique().reset_index(name='MAU')\n"
        "대상자 = 시청[시청['SO권역'] == '대전']['R고객번호']\n```",
        '대전 시청자는 2명이에요.\n```json\n{"그래프": null, "대상자": {"단계": 1, "설명": "대전 시청자"}}\n```',
    ])
    globals()['generate_code_analyst_step'] = lambda prompt: next(replies)
    found = run_analyst_turn([], "대전 시청자 찾아줘", tables)
    assert found['audience'] == {'설명': '대전 시청자', 'ids': ['1', '2'], 'count': 2} and '고객 2명이 지정됨' in found['steps'][0]['preview']
    json.dumps(found, ensure_ascii=False)
    replies = iter(["집단 찾기\n```python\n결과 = 시청.head(1)\n대상자 = ['1', '999']\n```", '끝.\n```json\n{"그래프": null, "대상자": null}\n```'])
    assert 'audience' not in run_analyst_turn([], "몇 명?", tables), "최종 답변이 가리키지 않으면 버튼을 만들지 않는다"
    assert sandbox.normalize_audience(pd.DataFrame({'R고객번호': [3, '3', None, 'x']})) == ['3', 'x'] and sandbox.normalize_audience(5) is None

    # 별도 프로세스 격리(실제 프로세스를 띄워서): 결과·집단 전달, 시간 초과 시 실제 종료 + 복구, 프로세스 사망 복구, 탈출 차단
    session = SandboxSession(tables, list(_REGION_ORDER), timeout=5, startup_timeout=90)
    try:
        r1 = session.run("결과 = 시청.groupby('SO권역')['R고객번호'].nunique().reset_index(name='MAU')\n대상자 = 시청[시청['SO권역'] == '대구']['R고객번호']", 1)
        assert list(r1['value']['MAU']) == [2, 2] and r1['audience'] == ['3', '4'], r1
        slow = session.run("결과 = sum([sum(range(900000)) for _ in range(900000)])", 2)
        assert '초 안에' in slow['error'] and session.proc is None, slow
        r3 = session.run("결과 = 이전결과[1]['MAU'].sum()", 3)   # 새 프로세스가 앞 단계 결과를 이어받는다
        assert r3['value'] == 4, r3
        session.proc.kill(); session.proc.wait()
        r4 = session.run("결과 = 1 + 1", 4)                      # 죽은 프로세스는 자동으로 다시 뜬다
        assert r4['value'] == 2, r4
        assert 'error' in session.run("결과 = str(pd.io.common.os)", 5) and 'error' in session.run("결과 = 시청['없음']", 6)
        big = session.run("결과 = pd.DataFrame({'a': list(range(300000))})", 7)   # 큰 표는 잘려서(20만 행) 돌아온다
        assert len(big['value']) == sandbox.MAX_RESULT_ROWS
    finally:
        session.close()
    # 프로세스를 시작하지 못하는 환경이면 같은 프로세스로 대신하고, 그 사실을 남긴다
    class _Unavailable:
        def __init__(self, *a, **k): raise SandboxUnavailable("테스트")
    ISOLATED, real_session = True, SandboxSession
    globals()['SandboxSession'] = _Unavailable
    replies = iter(["계산\n```python\n결과 = len(시청)\n```", '5건이에요.\n```json\n{"그래프": null}\n```'])
    fallback = run_analyst_turn([], "몇 건?", tables)
    globals()['SandboxSession'] = real_session
    assert fallback['text'] == '5건이에요.' and any('같은 프로세스에서 실행' in c for c in fallback['checks']), fallback
    # 표 다운로드용 결과표: 고객번호 컬럼 제외, 날짜는 글자로, JSON 저장 가능
    recs = _table_records(pd.DataFrame({'R고객번호': ['1', '2'], '월': pd.to_datetime(['2026-08-01', '2026-09-01']), 'MAU': [3, None]}))
    assert recs == [{'월': '2026-08-01', 'MAU': 3.0}, {'월': '2026-09-01', 'MAU': None}], recs
    json.dumps(recs)
    # 요청 판단: 거절·되묻기·안내는 계산(AI 호출) 없이 그 답만 돌려주고, 비교 피드백과 gate=False는 판단을 거치지 않으며, 진행의 '주의'는 답 끝에 붙는다
    globals()['generate_code_analyst_step'] = lambda prompt: (_ for _ in ()).throw(AssertionError("계산을 시작하면 안 된다"))
    _verdicts.append('{"판단": "안내", "답변": "분석 탭에서는 시청 데이터로 MAU, 재방문율 같은 것을 계산해요. 예: 6월 SO별 MAU"}')
    hello = run_analyst_turn([], "안녕 뭐 할 수 있어?", tables)
    assert hello['text'].startswith("분석 탭에서는") and 'steps' not in hello
    _verdicts.append('{"판단": "거절", "답변": "개인을 식별하는 정보는 알려드릴 수 없어요."}')
    assert run_analyst_turn([], "고객 전화번호 알려줘", tables)['text'].startswith("개인을 식별")
    _verdicts.append('{"판단": "거절", "답변": "x"}')
    for kwargs in ({"feedback": True}, {"gate": False}):   # 판단을 거치지 않으면 AI 판단을 부르지 않으므로 계산이 시작돼 위의 AssertionError가 난다
        try:
            run_analyst_turn([], "몇 건?", tables, **kwargs)
            raise SystemExit("계산이 시작돼야 함")
        except AssertionError as e:
            assert "계산을 시작" in str(e)
    _verdicts.clear()
    print("code_analyst self-check OK")
