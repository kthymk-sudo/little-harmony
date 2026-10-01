# ai_engine/gemini_api.py
import re
import requests
import time
import streamlit as st
from config import GEMINI_API_KEY
from prompts.target_chat_prompt import get_target_reasoning_prompt
from prompts.push_prompt import get_push_prompt, get_sms_prompt

_http_session = requests.Session()

# 🌟 [404 대응] 모델 목록 조회 자체가 실패했을 때의 최종 폴백값
_LATEST_FLASH_ALIAS = "models/gemini-flash-latest"

# 🌟 [429 대응 고도화] 실험/미리보기 계열 등 무료 등급 한도가 낮은 모델 키워드 배제
_UNUSABLE_MODEL_KEYWORDS = ("image", "audio", "tts", "embedding", "vision", "native", "omni", "live")   # 글 대화용이 아닌 모델 - 절대 쓰지 않는다
_AVOID_MODEL_KEYWORDS = ("exp", "preview", "thinking") + _UNUSABLE_MODEL_KEYWORDS

# 🌟 [한도 초과 모델 관리 - 시간 기반 만료] 429(일일 한도 초과) 에러로 더 이상 쓸 수 없는
# 모델을 "모델명 -> 배제된 시각(epoch)"으로 기록한다. 구글의 일일 한도는 자정마다
# 초기화되는데, 예전에는 이 목록이 프로세스가 재시작되기 전까지 영원히 비워지지 않아서,
# 어제 한도 초과로 배제한 모델이 오늘 한도가 다시 찼는데도 계속 후순위로 밀리는 문제가
# 있었다. 이제 배제 후 24시간이 지나면 자동으로 다시 후보에 포함시킨다.
_EXHAUSTED_MODELS = {}   # 모델명 -> 이 시각(epoch)까지 쉬게 한다
_DAILY_REST = 24 * 60 * 60   # 일일 한도 초과·더 이상 쓸 수 없는 모델
_MINUTE_REST = 60            # 분당 한도(요청이 몰린 것)는 잠깐만 쉬면 풀린다 - 하루 종일 배제하면 안 된다
_TROUBLE_REST = 5 * 60       # 서버 과부하·무응답은 일시적일 수 있다


def _mark_exhausted(model_name, rest=_DAILY_REST):
    _EXHAUSTED_MODELS[model_name] = time.time() + rest


def _is_still_exhausted(model_name):
    until = _EXHAUSTED_MODELS.get(model_name)
    if until is None:
        return False
    if time.time() >= until:
        _EXHAUSTED_MODELS.pop(model_name, None)
        return False
    return True


# 🌟 [에러 오염 방지] _call_gemini_api가 실패 시 반환하는 안내 문구는 항상 이 접두사로
# 시작한다. 이 문구를 정상 AI 답변과 구분 없이 대화 기록에 남기거나 근거/카피 생성에
# 재사용하면, 다음 턴 프롬프트에 "AI: ⚠️ ..." 식으로 그대로 섞여 들어가 AI가 오류
# 문구 자체를 실제 발언으로 착각하거나, 실패한 근거가 카피 생성에 재사용되는 문제가
# 생긴다. 호출부는 반환값을 화면/후속 로직에 쓰기 전에 반드시 is_api_error()로
# 먼저 확인해야 한다.
_ERROR_PREFIX = "⚠️"


def is_api_error(text):
    """_call_gemini_api가 실패 시 반환하는 안내 문구인지 판별한다."""
    return bool(text) and text.startswith(_ERROR_PREFIX)


def _pick_best_flash_model(available_models, lite=False):
    """available_models 중 'flash'가 들어간 모델을 우선 후보로 삼되:
    1순위. "-latest" 별칭(구글이 항상 최신 지원 버전으로 자동 교체해주는 안전한 이름)
    2순위. 실험/미리보기 등 무료 한도가 낮거나 언제 서비스 종료될지 모르는 이름을 피한 안정판
    3순위. 그마저도 없으면(전부 실험판만 있는 경우) 맨 처음 찾은 flash 모델 그대로
    lite=True면 가벼운(lite) 모델부터 고른다(답변 검수처럼 간단하고 자주 하는 일 - 본 모델의 하루 한도를 아낀다)."""

    # 🌟 [핵심 우회 로직] 이미 한도가 초과되어(24시간 이내) 블랙리스트에 들어간 모델은
    # 처음부터 사용할 수 있는 모델(valid_models) 목록에서 아예 빼버립니다.
    valid_models = [m for m in available_models if not _is_still_exhausted(m)]

    # 살아남은 모델(valid_models) 안에서만 flash 모델을 찾습니다.
    flash_models = [m for m in valid_models if 'flash' in m.lower()]
    latest_alias = next((m for m in flash_models if m.lower().endswith('flash-lite-latest' if lite else 'flash-latest')), None)

    if latest_alias:
        return latest_alias

    stable = [m for m in flash_models if not any(k in m.lower() for k in _AVOID_MODEL_KEYWORDS)]

    def rank(name):   # 상위 모델부터: lite가 아닌 것 먼저, 버전이 높은 것 먼저
        version = re.search(r"gemini-(\d+(?:\.\d+)?)", name)
        return (('lite' in name.lower()) != lite, -(float(version.group(1)) if version else 0))
    # 안정판이 다 막혔을 때만 미리보기판까지 쓴다(글 대화용이 아닌 모델은 끝까지 쓰지 않는다)
    candidates = sorted(stable, key=rank) or sorted([m for m in flash_models if not any(k in m.lower() for k in _UNUSABLE_MODEL_KEYWORDS)], key=rank)
    return candidates[0] if candidates else None


@st.cache_data(show_spinner=False, ttl=3600)
def _discover_target_model(lite=False):
    """사용 가능한 모델 목록은 자주 바뀌지 않으므로 1시간 동안 캐시해서 재사용한다."""
    target_model = _LATEST_FLASH_ALIAS
    url_models = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        res_models = _http_session.get(url_models, timeout=5)
        if res_models.status_code == 200:
            available_models = [m['name'] for m in res_models.json().get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
            picked = _pick_best_flash_model(available_models, lite)
            if picked:
                target_model = picked
    except requests.exceptions.RequestException:
        pass
    return target_model


def _switch_model(target_model, payload, rest=_DAILY_REST, lite=False):
    """이 모델을 rest초 동안 쉬게 하고 다른 모델로 다시 호출한다(한도 초과·서버 과부하·무응답 공통). 바꿀 모델이 없으면 None."""
    _mark_exhausted(target_model, rest)
    _discover_target_model.clear()
    new_target_model = _discover_target_model(lite)
    if new_target_model and (new_target_model != target_model) and not _is_still_exhausted(new_target_model):
        return _post_generate(payload, lite)
    return None


def _post_generate(payload, lite=False):
    """Google Gemini generateContent 호출, 예외 처리, Timeout, 재시도, 모델 갈아타기를 모두 담당하는 코어 함수.
    반환: ('ok', 응답 JSON) 또는 ('error', '⚠️ ...안내 문구')."""
    if GEMINI_API_KEY == "여기에_발급받으신_GEMINI_API_KEY를_붙여넣으세요" or not GEMINI_API_KEY:
        return "error", "⚠️ 시스템 은닉형 API 키가 설정되지 않았습니다. .env 또는 config 설정을 확인하세요."

    if not (GEMINI_API_KEY.startswith("AIza") or GEMINI_API_KEY.startswith("AQ.")):
        return "error", "⚠️ 입력하신 API 키의 형식이 올바르지 않습니다."

    try:
        target_model = _discover_target_model(lite)

        # 🌟 [속도 최적화] 구글 서버가 아플 때 너무 오래 기다리지 않도록 재시도는 총 2번, 타임아웃은 20초(도구를 쓰는 대화는 길어서 40초).
        timeout = 40 if payload.get("tools") else 20
        for attempt in range(2):
            first_try = attempt == 0
            url_generate = f"https://generativelanguage.googleapis.com/v1beta/{target_model}:generateContent?key={GEMINI_API_KEY}"
            try:
                res_gen = _http_session.post(url_generate, headers={"Content-Type": "application/json"}, json=payload, timeout=timeout)

                if res_gen.status_code == 200:
                    try:
                        return "ok", res_gen.json()
                    except ValueError:
                        return "error", "⚠️ 답변을 생성하지 못했습니다. 잠시 뒤 다시 시도해주세요."

                elif res_gen.status_code == 429:
                    if first_try and "PerDay" not in res_gen.text:   # 하루 한도는 기다려도 안 풀리니 바로 다른 모델로 넘어간다
                        retry_after = res_gen.headers.get("Retry-After")
                        try:
                            wait_s = float(retry_after) if retry_after else (2 ** attempt) + 1
                        except ValueError:
                            wait_s = (2 ** attempt) + 1
                        time.sleep(min(wait_s, 20))
                        continue
                    # 일일 한도는 하루 쉬게 하고, 분당 한도(요청이 몰림)는 1분만 쉬게 한다 - 구글 응답의 한도 이름으로 구분
                    switched = _switch_model(target_model, payload, _DAILY_REST if "PerDay" in res_gen.text else _MINUTE_REST, lite)
                    return switched if switched is not None else ("error",
                        "⚠️ [모든 AI 모델 한도 초과] 현재 사용 가능한 모든 AI 모델의 일일 한도를 모두 소진했습니다. "
                        "내일 다시 시도하시거나, Google AI Studio에서 결제 설정을 확인해주세요.")

                elif res_gen.status_code in [500, 503]:
                    if first_try:
                        time.sleep(2)
                        continue
                    # 🌟 [503 서버 과부하 우회] 구글 서버가 뻗었을 때도 즉시 다른 모델로 갈아탑니다.
                    switched = _switch_model(target_model, payload, _TROUBLE_REST, lite)
                    return switched if switched is not None else ("error",
                        f"⚠️ [서버 과부하] 구글 AI 서버가 혼잡하여 다른 모델로 우회하려 했으나 모두 실패했습니다. (상태코드: {res_gen.status_code})")

                elif res_gen.status_code == 404:   # 더 이상 쓸 수 없는 모델(예: 서비스 종료) - 오래 쉬게 하고 다른 모델로
                    switched = _switch_model(target_model, payload, lite=lite)
                    return switched if switched is not None else ("error", "⚠️ [모델 오류] 사용할 수 있는 AI 모델을 찾지 못했습니다.")

                else:
                    if res_gen.status_code == 400 and "not" in res_gen.text and ("enabled" in res_gen.text or "supported" in res_gen.text):
                        switched = _switch_model(target_model, payload, lite=lite)   # 이 모델로는 할 수 없는 요청 - 다른 모델로
                        if switched is not None:
                            return switched
                    return "error", f"⚠️ AI가 요청을 처리하지 못했어요(오류 {res_gen.status_code}). 잠시 뒤 다시 시도해 주세요."

            except requests.exceptions.RequestException as req_e:
                if first_try:
                    time.sleep(2)
                    continue
                # 🌟 [타임아웃 무응답 우회] 응답이 너무 오래 걸려도 버리고 다른 모델로 갈아탑니다.
                switched = _switch_model(target_model, payload, _TROUBLE_REST, lite)
                return switched if switched is not None else ("error", f"⚠️ 네트워크 통신 오류(Timeout 등)가 지속되어 중지합니다: {str(req_e)}")

    except Exception as e:
        return "error", f"⚠️ 시스템 통신 중 치명적 오류 발생: {str(e)}"
    return "error", "⚠️ 답변을 받지 못했습니다. 잠시 뒤 다시 시도해주세요."


_BLOCKED = "⚠️ 답변을 생성하지 못했습니다(안전 필터에 의해 차단됐을 수 있어요). 표현을 조금 바꿔 다시 시도해주세요."


_SEED = 7   # 같은 질문에 같은 답이 나오도록 무작위성을 고정한다


def _call_gemini_api(prompt, temperature=0.55, files=None, lite=False):
    """글 프롬프트 하나를 보내고 답 글을 돌려준다. 실패하면 '⚠️'로 시작하는 안내 문구(is_api_error로 확인).
    files: 함께 보낼 파일 [(mime_type, base64 문자열)] - 사진·PDF를 읽힐 때 쓴다."""
    parts = [{"text": prompt}] + [{"inline_data": {"mime_type": mime, "data": data}} for mime, data in (files or [])]
    status, data = _post_generate({"contents": [{"parts": parts}], "generationConfig": {"temperature": temperature, "seed": _SEED}}, lite)
    if status == "error":
        return data
    try:
        return data['candidates'][0]['content']['parts'][0]['text']
    except (KeyError, IndexError):
        return _BLOCKED


def call_agent(system_text, contents, tool_declarations=None, temperature=0.3, force_tool=False):
    """🌟 [도구를 쓰는 대화] 시스템 지침 + 대화 내역(contents)을 보내고 모델의 한 차례 응답을 돌려준다.
    반환: (모델 content {'role': 'model', 'parts': [...]}, None) 또는 (None, '⚠️ ...안내 문구').
    parts에는 글({'text'})과 도구 호출({'functionCall': {'name', 'args'}})이 섞여 있을 수 있다. 호출부가 이 content를 그대로 대화에
    이어 붙이고 도구 결과({'functionResponse'})를 user content로 넣어 다시 부른다.
    force_tool: 이번에는 글로만 답하지 못하고 반드시 도구를 부르게 한다(모델이 도구를 부르지 않고 "했다"고만 말할 때의 재시도용)."""
    payload = {"systemInstruction": {"parts": [{"text": system_text}]}, "contents": contents,
               "generationConfig": {"temperature": temperature, "seed": _SEED}}
    if tool_declarations:
        payload["tools"] = [{"functionDeclarations": tool_declarations}]
        if force_tool:
            payload["toolConfig"] = {"functionCallingConfig": {"mode": "ANY"}}
    for _ in range(2):   # 가끔 빈 응답이 오므로 한 번은 다시 시도한다
        status, data = _post_generate(payload)
        if status == "error":
            return None, data
        try:
            parts = data['candidates'][0]['content']['parts']
        except (KeyError, IndexError):
            continue
        if parts:
            return {"role": "model", "parts": parts}, None
    return None, _BLOCKED


def generate_target_reasoning(conditions_str, target_stats_str):
    prompt = get_target_reasoning_prompt(conditions_str, target_stats_str)
    return _call_gemini_api(prompt, temperature=0.4)

def generate_ai_push_copy(target_profile_str, reasoning_str, extra_request_str="", copy_type='push'):
    """copy_type: 'push'(앱푸시) 또는 'sms'(문자) - 타입에 따라 다른 프롬프트(글자수/광고표기 등
    제약이 다름)를 사용한다."""
    if copy_type == 'sms':
        prompt = get_sms_prompt(target_profile_str, reasoning_str, extra_request_str)
    else:
        prompt = get_push_prompt(target_profile_str, reasoning_str, extra_request_str)
    return _call_gemini_api(prompt, temperature=0.7)

def generate_code_analyst_step(prompt):
    """📊 분석 탭 코드 실행형: 한 단계(코드 또는 최종 답변). 프롬프트는 services/code_analyst.py가 조립한다."""
    return _call_gemini_api(prompt, temperature=0.2)

def read_file_text(data, mime_type):
    """🌟 [보고서 첨부] 사진·PDF 속 글자와 표를 그대로 옮겨 적은 텍스트를 돌려준다(요약·해석 없이). 실패하면 ⚠️ 문구."""
    import base64
    prompt = ("첨부된 파일에 보이는 글자와 표를 빠짐없이 그대로 옮겨 적어줘. 요약·해석·추측·평가는 하지 말고, 읽을 수 없는 글자는 (판독불가)로 표시해. "
              "표는 한 행을 한 줄에 '값 | 값 | 값' 형태로 옮기고, 목록과 들여쓰기 구조는 유지해. "
              "글자가 거의 없는 사진이면 보이는 것을 사실만 짧게 설명해. 인사말이나 설명 없이 옮긴 내용만 출력해.")
    return _call_gemini_api(prompt, temperature=0.1, files=[(mime_type, base64.b64encode(data).decode("ascii"))])

def check_answer(prompt):
    """🌟 답변 검수(services/answer_check.py): 가벼운 모델로, 결정적으로(온도 0) 판단한 JSON 글. 실패하면 ⚠️ 문구."""
    return _call_gemini_api(prompt, temperature=0.0, lite=True)
