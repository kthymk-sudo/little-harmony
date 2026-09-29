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
# 안전장치: 코드는 import/파일·네트워크 접근/while/밑줄(_)로 시작하는 속성 접근이 막힌 채로,
# 허용된 내장함수와 pd/np, 데이터 복사본만 있는 환경에서 실행된다. 결과를 AI에게 보낼 때는
# 고객번호 컬럼을 가린다. 원본 DB는 건드릴 수 없다.
# ============================================================
import ast
import builtins
import io
import re
import threading

import numpy as np
import pandas as pd

from ai_engine.gemini_api import generate_code_analyst_step, is_api_error
from prompts.code_analyst_prompt import get_code_analyst_prompt, FEEDBACK_INSTRUCTION
from services.analysis_service import (
    _add_derived_columns, _with_content_attrs, _REGION_ORDER, _REGION_COLUMNS, _region_sort_key,
    CHART_TYPES, normalize_pivot_result,
)
from utils.response_parser import parse_target_conditions

_MAX_STEPS = 6            # 코드 실행 최대 5번 + 마지막 답변
_MAX_RETRIES = 3          # 형식이 틀리거나 결과를 지어낸 응답을 다시 요청하는 최대 횟수(턴 전체)
_DEEP_QUESTION = re.compile(r'특이|원인|왜|이유|유입|영향|때문|요인')
_DEEP_MIN_STEPS = 3       # 원인·특이사항 질문에 기대하는 최소 성공 계산 단계
_MAX_NUDGES = 2           # 그래도 바로 답하려 하면 되돌려 보내는 최대 횟수
_TIMEOUT_SEC = 30
_PREVIEW_ROWS = 40
_HISTORY_TURNS = 8
_PRIVATE_COLS = ['이웃고객명', '게시자 아이디', '순번']   # AI가 볼 필요 없는 개인정보/내부 컬럼
_HIDDEN_IN_PREVIEW = ['R고객번호']                       # 결과를 AI에게 보낼 때 가리는 컬럼
FEEDBACK_REQUEST_TEXT = "🔍 이 결과를 전체와 비교해서 피드백해줘"

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
# 안전 실행
# ------------------------------------------------------------
_BLOCKED_NAMES = {
    'eval', 'exec', 'open', 'compile', '__import__', 'globals', 'locals', 'getattr', 'setattr', 'delattr',
    'vars', 'input', 'breakpoint', 'exit', 'quit', 'help', 'dir', 'memoryview', 'type', 'object', 'super',
}
_BLOCKED_ATTR = re.compile(
    r'^(_.*|to_(csv|excel|sql|pickle|parquet|hdf|feather|stata|clipboard|html|latex|markdown|json|xml|orc)'
    r'|read_\w+|eval|query|system|popen|load|save|savetxt|tofile|fromfile|loadtxt|genfromtxt)$'
)
_BLOCKED_NODES = (ast.Import, ast.ImportFrom, ast.While, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith,
                  ast.AsyncFunctionDef, ast.ClassDef, ast.Try, ast.Raise)
_SAFE_BUILTINS = {n: getattr(builtins, n) for n in (
    'len', 'range', 'min', 'max', 'sum', 'sorted', 'round', 'abs', 'list', 'dict', 'set', 'tuple', 'str', 'int',
    'float', 'bool', 'enumerate', 'zip', 'isinstance', 'any', 'all', 'map', 'filter', 'reversed', 'divmod',
)}


def check_code(code):
    """실행 전 검사. 문제가 있으면 이유 문자열, 없으면 None."""
    try:
        tree = ast.parse(code)
    except SyntaxError as e:
        return f"문법 오류: {e}"
    for node in ast.walk(tree):
        if isinstance(node, _BLOCKED_NODES):
            return f"허용되지 않는 구문: {type(node).__name__}"
        if isinstance(node, ast.Name) and (node.id in _BLOCKED_NAMES or node.id.startswith('__')):
            return f"허용되지 않는 이름: {node.id}"
        if isinstance(node, ast.Attribute) and _BLOCKED_ATTR.match(node.attr):
            return f"허용되지 않는 속성: {node.attr}"
    return None


_PRELOADED_IMPORT = re.compile(r'^\s*import\s+(pandas|numpy)(\s+as\s+\w+)?\s*$', re.MULTILINE)


def run_code(code, tables, previous):
    """코드를 데이터 복사본으로 실행. 반환: {'value': 결과, 'printed': 출력} 또는 {'error': ..., 'printed': ...}"""
    code = _PRELOADED_IMPORT.sub('', code)  # pd/np는 이미 준비돼 있으니 그 import 줄만 지운다(다른 import는 막힘)
    problem = check_code(code)
    if problem:
        return {'error': problem, 'printed': ''}
    out = io.StringIO()

    def _print(*args, sep=' ', end='\n', **_):
        out.write(sep.join(str(a) for a in args) + end)

    namespace = {
        '__builtins__': {**_SAFE_BUILTINS, 'print': _print},
        'pd': pd, 'np': np, 'SO권역순서': list(_REGION_ORDER), '이전결과': dict(previous),
        **{name: df.copy() for name, df in tables.items()},
    }
    outcome = {}

    def _target():
        try:
            exec(compile(code, '<분석코드>', 'exec'), namespace)
            # AI가 자주 영어 이름(result)에 담는다 - 같은 뜻으로 받아준다
            outcome['value'] = namespace['결과'] if namespace.get('결과') is not None else namespace.get('result')
        except Exception as e:  # AI 코드의 어떤 오류든 AI에게 돌려줘서 고치게 한다
            outcome['error'] = f"{type(e).__name__}: {e}"

    # ponytail: 스레드 타임아웃은 응답만 끊고 실행 자체는 멈추지 못함 - 긴 연산이 문제되면 별도 프로세스로
    worker = threading.Thread(target=_target, daemon=True)
    worker.start()
    worker.join(_TIMEOUT_SEC)
    printed = out.getvalue()[-3000:]
    if worker.is_alive():
        return {'error': f"{_TIMEOUT_SEC}초 안에 끝나지 않았어요. 더 가벼운 방법으로 계산해 주세요.", 'printed': printed}
    if 'error' in outcome:
        return {'error': outcome['error'], 'printed': printed}
    if outcome.get('value') is None:
        return {'error': "`결과` 변수에 값이 없어요. 보여줄 결과를 `결과 = ...`로 담아 주세요.", 'printed': printed}
    return {'value': outcome['value'], 'printed': printed}


# 답변 속 숫자는 천 단위 쉼표(1,514)를 허용하고, 실행 결과(CSV·pandas 출력)는 쉼표가 구분자라 숫자만 읽는다
_NUMBER = re.compile(r'(?<![\w.])(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?')
_PLAIN_NUMBER = re.compile(r'\d+(?:\.\d+)?')


def _number_forms(text, pattern=_PLAIN_NUMBER):
    """문자열 속 숫자를 비교용 형태로(쉼표 제거, 소수는 0~2자리 반올림 형태도 포함)."""
    forms = set()
    for token in pattern.findall(text):
        raw = token.replace(',', '')
        try:
            value = float(raw)
        except ValueError:
            continue
        forms.add(raw)
        for digits in (0, 1, 2):
            rounded = round(value, digits)
            forms.add(f"{rounded:.{digits}f}")
            if rounded == int(rounded):
                forms.add(str(int(rounded)))
    return forms


def ungrounded_numbers(answer, evidence):
    """답변의 숫자 중 실행 결과(evidence)에 없는 것. 연도·월·일 같은 작은 수는 문맥상 흔해서 제외한다."""
    known = _number_forms(evidence)
    missing = []
    for token in _NUMBER.findall(answer):
        raw = token.replace(',', '')
        try:
            value = float(raw)
        except ValueError:
            continue
        if value <= 31 and float(value).is_integer() or 2000 <= value <= 2100:
            continue
        if raw not in known and not (_number_forms(raw) & known):
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
    units = {k: v for k, v in (graph.get('단위') or {}).items() if k in values}
    chart_type = graph.get('차트유형') if graph.get('차트유형') in CHART_TYPES else '막대'
    series_col = graph.get('계열') if graph.get('계열') in df.columns and graph.get('계열') not in x else None
    for c in df.columns:  # 날짜 등은 JSON 저장/그래프 라벨용 문자열로
        if pd.api.types.is_datetime64_any_dtype(df[c]) or isinstance(df[c].dtype, pd.PeriodDtype):
            df[c] = df[c].astype(str)

    change = None
    ch = graph.get('증감') or {}
    if chart_type == '증감' and ch.get('기준값') in df.columns and ch.get('비교값') in df.columns:
        m = values[0]
        rate = ch.get('증감률') if ch.get('증감률') in df.columns else None
        flat = df[x + [ch['기준값'], ch['비교값'], m] + ([rate] if rate else [])]
        series = {m: [ch['기준값'], ch['비교값']]}
        labels = {ch['기준값']: ch['기준값'], ch['비교값']: ch['비교값']}
        change = {'기준': ch['기준값'], '비교': ch['비교값'],
                  '측정값': {m: {'기준': ch['기준값'], '비교': ch['비교값'], '증감': m, '증감률': rate}}}
        units = {m: units.get(m, '')}
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


_FABRICATION_SIGNS = re.compile(
    rf"{_LOG_MARK}|\[\d+\s*단계\]|(^|\n)\s*(코드|출력|결과|실행한 코드|print 출력|실행 오류)\s*:", re.MULTILINE
)


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


def run_analyst_turn(messages, user_text, tables, on_step=None, feedback=False):
    """한 턴 처리. 반환: 새 assistant 메시지 dict
       {'text', 'steps': [{'번호','설명','code','preview'|'error','printed'}], 'data'(그래프용, 선택), 'chart'(선택)}"""
    schema = describe_tables(tables)
    history = _history_str(messages)
    steps, values = [], {}
    correction, retries, nudges = "", 0, 0
    checks = []  # 답변 검증 기록(지어낸 숫자 차단 등) - "계산 과정 보기"에 같이 보여준다
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
        nudge = None  # "더 파고들어" - 무시해도 답변 자체는 믿을 수 있으므로, 횟수를 넘기면 그 답변을 받는다
        if not has_code and not reason:
            # 🌟 최종 답변의 숫자가 실제 실행 결과(또는 이전 답변)에 있는지 대조 - 지어낸 숫자 차단
            evidence = "\n".join([s.get('preview', '') + "\n" + s.get('printed', '') for s in steps]
                                 + [m.get('text', '') for m in messages] + [user_text])
            missing = ungrounded_numbers(parse_target_conditions(_CODE_BLOCK.sub('', raw))[0], evidence)
            if missing:
                reason = (f"답변의 숫자 {', '.join(missing[:6])}가 실행 결과에 없어. 필요한 값은 먼저 코드로 계산해서 "
                          f"실행 결과로 확인한 뒤, 실행 결과에 나온 숫자만 써.")
            elif (nudges < _MAX_NUDGES and _DEEP_QUESTION.search(user_text)
                  and sum('preview' in s for s in steps) < _DEEP_MIN_STEPS):
                # 원인·특이사항 질문인데 너무 일찍 끝내려 하면 더 파고들게 한다
                nudges += 1
                nudge = ("원인·특이사항을 묻는 질문인데 근거 계산이 부족해. 전월/다른 권역/전체와 비교해 튀는 곳을 찾고, "
                         "그곳의 원인을 한 단계 더 파고드는 코드를 실행해(예: 신규 시청자가 처음 본 콘텐츠, 그 권역에서만 "
                         "유독 많이 본 콘텐츠의 전체 대비 비중). 이번 응답은 최종 답변이 아니라 반드시 ```python 코드블록```이어야 해.")
        if reason and retries < _MAX_RETRIES:
            correction, retries = reason, retries + 1
            checks.append(f"다시 요청: {reason}")
            continue  # 같은 단계를 다시 요청(단계 수는 늘리지 않음)
        if nudge:
            correction = nudge
            checks.append("더 파고들도록 다시 요청")
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
            run = run_code(step['code'], tables, values)
            step['printed'] = run.get('printed', '')
            if 'error' in run:
                step['error'] = run['error']
            else:
                values[step['번호']] = run['value']
                step['preview'] = preview(run['value'])
            steps.append(step)
            continue
        if reason:  # 다시 요청해도 지어낸 결과가 섞여 있으면 그 답변은 보여주지 않는다
            checks.append(f"답변 폐기: {reason}")
            return {'role': 'assistant', 'steps': steps, 'checks': checks,
                    'text': "계산은 했지만 답변을 믿을 수 있게 정리하지 못했어요. 아래 '계산 과정 보기'에서 실제 계산 "
                            "결과를 확인하시거나, 질문을 조금 나눠서 다시 물어봐주세요."}
        return {**_final_message(raw, steps, values, messages), 'checks': checks}
    return {'role': 'assistant', 'text': "분석을 마무리하지 못했어요. 질문을 조금 나눠서 다시 물어봐주세요.",
            'steps': steps, 'checks': checks}


def _final_message(raw, steps, values, messages):
    text, spec = parse_target_conditions(_CODE_BLOCK.sub('', raw))
    message = {'role': 'assistant', 'text': text or "분석을 마쳤어요.", 'steps': steps}
    graph = spec.get('그래프') if isinstance(spec.get('그래프'), dict) else None
    if graph:
        value = values.get(graph.get('단계')) if graph.get('단계') in values else (values[max(values)] if values else None)
        data = table_to_pivot_result(value, graph)
        if data:
            message['data'] = data
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
    replies = iter(["대전은 1,514명이에요.\n```json\n{\"그래프\": null}\n```", "5건이에요.\n```json\n{\"그래프\": null}\n```"])
    globals()['generate_code_analyst_step'] = lambda prompt: next(replies)
    assert run_analyst_turn([], "몇 건?", tables)['text'] == '5건이에요.'
    assert run_code("x = 1", tables, {})['error'].startswith("`결과`"), "결과를 안 담으면 오류로 알려준다"
    assert run_code("result = 1", tables, {})['value'] == 1, "영어 이름 result도 결과로 받는다"
    assert run_code("import pandas as pd\n결과 = pd.Series([1]).sum()", tables, {})['value'] == 1
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
    print("code_analyst self-check OK")
