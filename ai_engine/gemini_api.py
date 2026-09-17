# ai_engine/gemini_api.py
import requests
import time
import streamlit as st
from config import GEMINI_API_KEY
from prompts.target_chat_prompt import get_target_chat_prompt, get_target_reasoning_prompt, get_segment_insight_prompt
from prompts.push_prompt import get_push_prompt, get_sms_prompt

_http_session = requests.Session()

# 🌟 [404 대응] 모델 목록 조회 자체가 실패했을 때의 최종 폴백값
_LATEST_FLASH_ALIAS = "models/gemini-flash-latest"

# 🌟 [429 대응 고도화] 실험/미리보기 계열 등 무료 등급 한도가 낮은 모델 키워드 배제
_AVOID_MODEL_KEYWORDS = ("exp", "preview", "thinking", "image", "audio", "tts", "embedding", "vision", "native")

# 🌟 [한도 초과 모델 관리] 429(일일 한도 초과) 에러로 더 이상 쓸 수 없는 모델을 기록하여,
# 다음 탐색 시 우선순위에서 배제하고 다른 모델을 선택하도록 합니다. (초기화 시 빈 상태)
_EXHAUSTED_MODELS = set()


def _pick_best_flash_model(available_models):
    """available_models 중 'flash'가 들어간 모델을 우선 후보로 삼되:
    1순위. "-latest" 별칭(구글이 항상 최신 지원 버전으로 자동 교체해주는 안전한 이름)
    2순위. 실험/미리보기 등 무료 한도가 낮거나 언제 서비스 종료될지 모르는 이름을 피한 안정판
    3순위. 그마저도 없으면(전부 실험판만 있는 경우) 맨 처음 찾은 flash 모델 그대로"""
    
    # 🌟 [핵심 우회 로직] 이미 한도가 초과되어 블랙리스트(_EXHAUSTED_MODELS)에 들어간 
    # 모델은 처음부터 사용할 수 있는 모델(valid_models) 목록에서 아예 빼버립니다.
    valid_models = [m for m in available_models if m not in _EXHAUSTED_MODELS]
    
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


def _call_gemini_api(prompt, temperature=0.55):
    """Google Gemini API 호출, 예외 처리, Timeout, 재시도를 모두 담당하는 코어 함수."""
    if GEMINI_API_KEY == "여기에_발급받으신_GEMINI_API_KEY를_붙여넣으세요" or not GEMINI_API_KEY:
        return "⚠️ 시스템 은닉형 API 키가 설정되지 않았습니다. .env 또는 config 설정을 확인하세요."

    if not (GEMINI_API_KEY.startswith("AIza") or GEMINI_API_KEY.startswith("AQ.")):
        return "⚠️ 입력하신 API 키의 형식이 올바르지 않습니다."

    try:
        target_model = _discover_target_model()
        payload = {"contents": [{"parts": [{"text": prompt}]}], "generationConfig": {"temperature": temperature}}

        # 🌟 [속도 최적화] 구글 서버가 아플 때 너무 오래 기다리지 않도록 재시도 횟수를 2회로 확 줄입니다.
        max_retries = 2
        max_retries_5xx = 2
        
        for attempt in range(max(max_retries, max_retries_5xx)):
            url_generate = f"https://generativelanguage.googleapis.com/v1beta/{target_model}:generateContent?key={GEMINI_API_KEY}"
            try:
                # 🌟 타임아웃도 30초에서 20초로 줄여서 무한정 멈춰있는 현상을 방지합니다.
                res_gen = _http_session.post(url_generate, headers={"Content-Type": "application/json"}, json=payload, timeout=20)

                if res_gen.status_code == 200:
                    try:
                        return res_gen.json()['candidates'][0]['content']['parts'][0]['text']
                    except (KeyError, IndexError, ValueError):
                        return "⚠️ 답변을 생성하지 못했습니다(안전 필터에 의해 차단됐을 수 있어요). 표현을 조금 바꿔 다시 시도해주세요."

                elif res_gen.status_code == 429:
                    if attempt < max_retries - 1:  
                        retry_after = res_gen.headers.get("Retry-After")
                        try:
                            wait_s = float(retry_after) if retry_after else (2 ** attempt) + 1
                        except ValueError:
                            wait_s = (2 ** attempt) + 1
                        time.sleep(min(wait_s, 20))
                        continue
                    
                    _EXHAUSTED_MODELS.add(target_model)
                    _discover_target_model.clear() 
                    
                    new_target_model = _discover_target_model()
                    if new_target_model and (new_target_model != target_model) and (new_target_model not in _EXHAUSTED_MODELS):
                        return _call_gemini_api(prompt, temperature)
                        
                    return (
                        "⚠️ [모든 AI 모델 한도 초과] 현재 사용 가능한 모든 AI 모델의 일일 한도를 모두 소진했습니다. "
                        "내일 다시 시도하시거나, Google AI Studio에서 결제 설정을 확인해주세요."
                    )

                elif res_gen.status_code in [500, 503]:
                    if attempt < max_retries_5xx - 1:  
                        time.sleep(2) # 대기 시간을 짧게 고정합니다.
                        continue
                    
                    # 🌟 [503 서버 과부하 우회 추가!] 구글 서버가 뻗었을 때도 즉시 다른 모델로 갈아탑니다.
                    _EXHAUSTED_MODELS.add(target_model)
                    _discover_target_model.clear() 
                    
                    new_target_model = _discover_target_model()
                    if new_target_model and (new_target_model != target_model) and (new_target_model not in _EXHAUSTED_MODELS):
                        return _call_gemini_api(prompt, temperature)
                        
                    return f"⚠️ [서버 과부하] 구글 AI 서버가 혼잡하여 다른 모델로 우회하려 했으나 모두 실패했습니다. (상태코드: {res_gen.status_code})"

                elif res_gen.status_code == 404:
                    _discover_target_model.clear()
                    if target_model != _LATEST_FLASH_ALIAS and attempt < max_retries - 1:
                        target_model = _LATEST_FLASH_ALIAS
                        continue
                    return "⚠️ [모델 오류] 사용하려던 AI 모델을 찾을 수 없어 안전 모델로 자동 재시도 중 실패했습니다."
                    
                else:
                    return f"⚠️ API 요청 거부 ({res_gen.status_code}): {res_gen.text}"

            except requests.exceptions.RequestException as req_e:
                if attempt < max_retries - 1:
                    time.sleep(2)
                    continue
                
                # 🌟 [타임아웃 무응답 우회 추가!] 응답이 너무 오래 걸려도 버리고 다른 모델로 갈아탑니다.
                _EXHAUSTED_MODELS.add(target_model)
                _discover_target_model.clear() 
                
                new_target_model = _discover_target_model()
                if new_target_model and (new_target_model != target_model) and (new_target_model not in _EXHAUSTED_MODELS):
                    return _call_gemini_api(prompt, temperature)
                    
                return f"⚠️ 네트워크 통신 오류(Timeout 등)가 지속되어 중지합니다: {str(req_e)}"

    except Exception as e:
        return f"⚠️ 시스템 통신 중 치명적 오류 발생: {str(e)}"


def _format_chat_history(chat_history):
    """[{'role': 'user'/'ai', 'text': '...'}] 리스트를 프롬프트에 넣을 문자열로 변환."""
    if not chat_history:
        return "(아직 대화 없음)"
    lines = []
    for turn in chat_history:
        speaker = "실무자" if turn.get("role") == "user" else "AI"
        lines.append(f"{speaker}: {turn.get('text', '')}")
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

def get_current_model_label():
    return _discover_target_model().replace("models/", "")

def generate_segment_insight_reply(question_str, conditions_str, insight_stats_str):
    prompt = get_segment_insight_prompt(question_str, conditions_str, insight_stats_str)
    return _call_gemini_api(prompt, temperature=0.4)