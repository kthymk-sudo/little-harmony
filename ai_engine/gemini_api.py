# ai_engine/gemini_api.py
import requests
import time
import streamlit as st
from config import GEMINI_API_KEY
from prompts.target_chat_prompt import get_target_chat_prompt, get_target_reasoning_prompt, get_segment_insight_prompt
from prompts.push_prompt import get_push_prompt, get_sms_prompt
from prompts.analysis_chat_prompt import get_pivot_insight_prompt
from prompts.report_prompt import get_report_prompt

_http_session = requests.Session()

# 🌟 [404 대응] 모델 목록 조회 자체가 실패했을 때의 최종 폴백값
_LATEST_FLASH_ALIAS = "models/gemini-flash-latest"

# 🌟 [429 대응 고도화] 실험/미리보기 계열 등 무료 등급 한도가 낮은 모델 키워드 배제
_AVOID_MODEL_KEYWORDS = ("exp", "preview", "thinking", "image", "audio", "tts", "embedding", "vision", "native")

# 🌟 [한도 초과 모델 관리 - 시간 기반 만료] 429(일일 한도 초과) 에러로 더 이상 쓸 수 없는
# 모델을 "모델명 -> 배제된 시각(epoch)"으로 기록한다. 구글의 일일 한도는 자정마다
# 초기화되는데, 예전에는 이 목록이 프로세스가 재시작되기 전까지 영원히 비워지지 않아서,
# 어제 한도 초과로 배제한 모델이 오늘 한도가 다시 찼는데도 계속 후순위로 밀리는 문제가
# 있었다. 이제 배제 후 24시간이 지나면 자동으로 다시 후보에 포함시킨다.
_EXHAUSTED_MODELS = {}
_EXHAUSTED_TTL_SECONDS = 24 * 60 * 60


def _mark_exhausted(model_name):
    _EXHAUSTED_MODELS[model_name] = time.time()


def _is_still_exhausted(model_name):
    exhausted_at = _EXHAUSTED_MODELS.get(model_name)
    if exhausted_at is None:
        return False
    if time.time() - exhausted_at >= _EXHAUSTED_TTL_SECONDS:
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


def _pick_best_flash_model(available_models):
    """available_models 중 'flash'가 들어간 모델을 우선 후보로 삼되:
    1순위. "-latest" 별칭(구글이 항상 최신 지원 버전으로 자동 교체해주는 안전한 이름)
    2순위. 실험/미리보기 등 무료 한도가 낮거나 언제 서비스 종료될지 모르는 이름을 피한 안정판
    3순위. 그마저도 없으면(전부 실험판만 있는 경우) 맨 처음 찾은 flash 모델 그대로"""

    # 🌟 [핵심 우회 로직] 이미 한도가 초과되어(24시간 이내) 블랙리스트에 들어간 모델은
    # 처음부터 사용할 수 있는 모델(valid_models) 목록에서 아예 빼버립니다.
    valid_models = [m for m in available_models if not _is_still_exhausted(m)]

    # 살아남은 모델(valid_models) 안에서만 flash 모델을 찾습니다.
    flash_models = [m for m in valid_models if 'flash' in m.lower()]
    latest_alias = next((m for m in flash_models if m.lower().endswith('flash-latest')), None)

    if latest_alias:
        return latest_alias

    stable = [m for m in flash_models if not any(k in m.lower() for k in _AVOID_MODEL_KEYWORDS)]
    candidates = stable or flash_models
    return candidates[0] if candidates else None


@st.cache_data(show_spinner=False, ttl=3600)
def _discover_target_model():
    """사용 가능한 모델 목록은 자주 바뀌지 않으므로 1시간 동안 캐시해서 재사용한다."""
    target_model = _LATEST_FLASH_ALIAS
    url_models = f"https://generativelanguage.googleapis.com/v1beta/models?key={GEMINI_API_KEY}"
    try:
        res_models = _http_session.get(url_models, timeout=5)
        if res_models.status_code == 200:
            available_models = [m['name'] for m in res_models.json().get('models', []) if 'generateContent' in m.get('supportedGenerationMethods', [])]
            picked = _pick_best_flash_model(available_models)
            if picked:
                target_model = picked
    except requests.exceptions.RequestException:
        pass
    return target_model


def _switch_model(target_model, prompt, temperature, files=None):
    """이 모델을 소진으로 표시하고 다른 모델로 다시 호출한다(한도 초과·서버 과부하·무응답 공통). 바꿀 모델이 없으면 None."""
    _mark_exhausted(target_model)
    _discover_target_model.clear()
    new_target_model = _discover_target_model()
    if new_target_model and (new_target_model != target_model) and not _is_still_exhausted(new_target_model):
        return _call_gemini_api(prompt, temperature, files)
    return None


def _call_gemini_api(prompt, temperature=0.55, files=None):
    """Google Gemini API 호출, 예외 처리, Timeout, 재시도를 모두 담당하는 코어 함수.
    files: 함께 보낼 파일 [(mime_type, base64 문자열)] - 사진·PDF를 읽힐 때 쓴다."""
    if GEMINI_API_KEY == "여기에_발급받으신_GEMINI_API_KEY를_붙여넣으세요" or not GEMINI_API_KEY:
        return "⚠️ 시스템 은닉형 API 키가 설정되지 않았습니다. .env 또는 config 설정을 확인하세요."

    if not (GEMINI_API_KEY.startswith("AIza") or GEMINI_API_KEY.startswith("AQ.")):
        return "⚠️ 입력하신 API 키의 형식이 올바르지 않습니다."

    try:
        target_model = _discover_target_model()
        parts = [{"text": prompt}] + [{"inline_data": {"mime_type": mime, "data": data}} for mime, data in (files or [])]
        payload = {"contents": [{"parts": parts}], "generationConfig": {"temperature": temperature}}

        # 🌟 [속도 최적화] 구글 서버가 아플 때 너무 오래 기다리지 않도록 재시도는 총 2번, 타임아웃은 20초.
        for attempt in range(2):
            first_try = attempt == 0
            url_generate = f"https://generativelanguage.googleapis.com/v1beta/{target_model}:generateContent?key={GEMINI_API_KEY}"
            try:
                res_gen = _http_session.post(url_generate, headers={"Content-Type": "application/json"}, json=payload, timeout=20)

                if res_gen.status_code == 200:
                    try:
                        return res_gen.json()['candidates'][0]['content']['parts'][0]['text']
                    except (KeyError, IndexError, ValueError):
                        return "⚠️ 답변을 생성하지 못했습니다(안전 필터에 의해 차단됐을 수 있어요). 표현을 조금 바꿔 다시 시도해주세요."

                elif res_gen.status_code == 429:
                    if first_try:
                        retry_after = res_gen.headers.get("Retry-After")
                        try:
                            wait_s = float(retry_after) if retry_after else (2 ** attempt) + 1
                        except ValueError:
                            wait_s = (2 ** attempt) + 1
                        time.sleep(min(wait_s, 20))
                        continue
                    switched = _switch_model(target_model, prompt, temperature, files)
                    return switched if switched is not None else (
                        "⚠️ [모든 AI 모델 한도 초과] 현재 사용 가능한 모든 AI 모델의 일일 한도를 모두 소진했습니다. "
                        "내일 다시 시도하시거나, Google AI Studio에서 결제 설정을 확인해주세요."
                    )

                elif res_gen.status_code in [500, 503]:
                    if first_try:
                        time.sleep(2)
                        continue
                    # 🌟 [503 서버 과부하 우회] 구글 서버가 뻗었을 때도 즉시 다른 모델로 갈아탑니다.
                    switched = _switch_model(target_model, prompt, temperature, files)
                    return switched if switched is not None else (
                        f"⚠️ [서버 과부하] 구글 AI 서버가 혼잡하여 다른 모델로 우회하려 했으나 모두 실패했습니다. (상태코드: {res_gen.status_code})"
                    )

                elif res_gen.status_code == 404:
                    _discover_target_model.clear()
                    if target_model != _LATEST_FLASH_ALIAS and first_try:
                        target_model = _LATEST_FLASH_ALIAS
                        continue
                    return "⚠️ [모델 오류] 사용하려던 AI 모델을 찾을 수 없어 안전 모델로 자동 재시도 중 실패했습니다."

                else:
                    return f"⚠️ API 요청 거부 ({res_gen.status_code}): {res_gen.text}"

            except requests.exceptions.RequestException as req_e:
                if first_try:
                    time.sleep(2)
                    continue
                # 🌟 [타임아웃 무응답 우회] 응답이 너무 오래 걸려도 버리고 다른 모델로 갈아탑니다.
                switched = _switch_model(target_model, prompt, temperature, files)
                return switched if switched is not None else f"⚠️ 네트워크 통신 오류(Timeout 등)가 지속되어 중지합니다: {str(req_e)}"

    except Exception as e:
        return f"⚠️ 시스템 통신 중 치명적 오류 발생: {str(e)}"


def _format_chat_history(chat_history):
    """[{'role': 'user'/'ai', 'text': '...'}] 리스트를 프롬프트에 넣을 문자열로 변환.
    🌟 [에러 오염 방지] 과거 턴 중 API 실패로 인한 안내 문구(⚠️로 시작)는 실제 AI
    발언이 아니므로, 다음 턴 프롬프트에 다시 섞여 들어가지 않도록 제외한다."""
    if not chat_history:
        return "(아직 대화 없음)"
    lines = []
    for turn in chat_history:
        text = turn.get('text', '')
        if turn.get("role") != "user" and is_api_error(text):
            continue
        speaker = "실무자" if turn.get("role") == "user" else "AI"
        lines.append(f"{speaker}: {text}")
    return "\n".join(lines)


# =====================================================================
# 🚀 기능별 서비스 함수 (코어 엔진 호출)
# =====================================================================
def generate_target_chat_reply(chat_history, profile_context_str, current_conditions_str, term_context_str=""):
    chat_history_str = _format_chat_history(chat_history)
    prompt = get_target_chat_prompt(chat_history_str, profile_context_str, current_conditions_str, term_context_str)
    return _call_gemini_api(prompt, temperature=0.6)

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

def generate_segment_insight_reply(question_str, conditions_str, insight_stats_str):
    prompt = get_segment_insight_prompt(question_str, conditions_str, insight_stats_str)
    return _call_gemini_api(prompt, temperature=0.4)

def generate_pivot_insight_reply(question_str, spec_str, result_str):
    prompt = get_pivot_insight_prompt(question_str, spec_str, result_str)
    return _call_gemini_api(prompt, temperature=0.4)

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

def generate_report_reply(chat_history, profile_context_str, so_reports_str=""):
    chat_history_str = _format_chat_history(chat_history)
    prompt = get_report_prompt(chat_history_str, profile_context_str, so_reports_str)
    return _call_gemini_api(prompt, temperature=0.2)  # 원문을 빠짐없이 옮기는 정리가 기본이라 낮게