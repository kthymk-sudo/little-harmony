# ui/job_runner.py
# ============================================================
# 🌟 [백그라운드 작업] services/background.py의 작업을 화면(Streamlit)에서 시작하고, 진행 표시를 보여주며 결과를 꺼내는 도우미.
# 사용자가 다른 탭이나 대화로 넘어가면 이 화면 스크립트는 중단되지만, 작업 스레드는 계속 돌아 끝나면 대화를 저장한다.
# 나중에 그 대화 화면으로 돌아오면 wait_for()가 끝난 결과를 바로 꺼내 주거나(아직 진행 중이면 이어서 진행 표시를 보여준다).
# ============================================================
import threading
import time

import streamlit as st
from streamlit.runtime.scriptrunner import add_script_run_ctx, get_script_run_ctx

from services import background


def start(key, work, persist=None, label="답변을 만드는 중..."):
    """work(job) -> 결과를 백그라운드로 시작한다. work와 persist 안에서는 st.session_state를 쓰면 안 된다(값을 미리 복사해 둘 것)."""
    ctx = get_script_run_ctx()

    def run(job):
        if ctx is not None:
            add_script_run_ctx(threading.current_thread(), ctx)   # 스레드 안에서 st.cache 등을 써도 경고가 나지 않게
        return work(job)

    background.submit(key, run, persist=persist, label=label)


def wait_for(key):
    """이 대화에 진행 중이거나 끝난 작업이 있으면 끝날 때까지 진행 표시를 보여주고 결과를 꺼낸다: ('ok', 결과) / ('error', 예외).
    작업이 없으면 None. 기다리는 동안 다른 탭으로 넘어가면 이 스크립트만 멈추고 작업은 계속된다."""
    job = background.get(key)
    if job is None:
        return None
    if not job.done():
        with st.status(job.label, expanded=False) as status:
            while not job.done():
                status.update(label=job.progress or job.label)
                time.sleep(0.4)
            status.update(label="완료", state="complete")
    return background.take(key)
