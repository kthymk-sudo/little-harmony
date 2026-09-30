# services/code_sandbox.py
# ============================================================
# 📊 분석 탭 - AI가 쓴 분석 코드를 "안전하게" 실행하는 부분 (Streamlit/config 비의존 - 워커 프로세스가 가볍게 뜨도록).
#
# 두 겹으로 막는다.
#  1) 코드 검사 + 제한된 실행 환경: import/파일·네트워크/모듈 타고 들어가기/무한 반복 등을 미리 막고,
#     분석에 필요한 pandas·numpy 함수만 담은 꾸러미와 데이터 복사본만 준다(execute).
#  2) 별도 프로세스 격리(SandboxSession): 코드를 앱과 다른 프로세스에서 돌린다.
#     - 시간 제한: 넘으면 프로세스를 실제로 종료한다(스레드는 못 멈춰서 예전에는 계산이 계속 남았다).
#     - 메모리 제한: 리눅스(배포 환경)에서는 프로세스 메모리 상한을 걸어, 무거운 계산이 앱 전체를 죽이지 못하게 한다.
#     - 프로세스가 죽어도(메모리 초과 등) 앱은 그대로이고, 다음 단계에서 새 프로세스로 이어간다.
#     프로세스를 시작하지 못하는 환경이면 SandboxUnavailable을 내고, 호출부가 같은 프로세스 실행(execute)으로 대신한다.
# ============================================================
import ast
import builtins
import io
import os
import pickle
import queue
import re
import struct
import subprocess
import sys
import threading
import types

import numpy as np
import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAX_AUDIENCE = 50_000        # 타겟팅으로 보낼 수 있는 집단 크기 상한
MAX_RESULT_ROWS = 200_000    # 프로세스 사이로 돌려주는 결과표 행 수 상한

# ------------------------------------------------------------
# 1) 코드 검사 + 제한된 실행 환경
# ------------------------------------------------------------
_BLOCKED_NAMES = {
    'eval', 'exec', 'open', 'compile', '__import__', 'globals', 'locals', 'getattr', 'setattr', 'delattr',
    'vars', 'input', 'breakpoint', 'exit', 'quit', 'help', 'dir', 'memoryview', 'type', 'object', 'super',
}
_BLOCKED_ATTR = re.compile(
    r'^(_.*|to_(csv|excel|sql|pickle|parquet|hdf|feather|stata|clipboard|html|latex|markdown|json|xml|orc|string)'
    r'|read_\w+|eval|query|system|popen|load|save|savetxt|tofile|fromfile|loadtxt|genfromtxt|savefig|style|plot|dumps?'
    r'|os|sys|io|subprocess|shutil|pathlib|builtins|importlib|ctypes|compat|core|util|lib|api|common|testing)$'
)
_BLOCKED_NODES = (ast.Import, ast.ImportFrom, ast.While, ast.Global, ast.Nonlocal, ast.With, ast.AsyncWith,
                  ast.AsyncFunctionDef, ast.ClassDef, ast.Try, ast.Raise)
_MAX_RANGE = 1_000_000


def _capped_range(*args):
    r = range(*args)
    if len(r) > _MAX_RANGE:
        raise ValueError(f"range가 너무 커요({len(r):,}). 반복문 대신 pandas 집계로 계산해 주세요.")
    return r


_SAFE_BUILTINS = {n: getattr(builtins, n) for n in (
    'len', 'min', 'max', 'sum', 'sorted', 'round', 'abs', 'list', 'dict', 'set', 'tuple', 'str', 'int',
    'float', 'bool', 'enumerate', 'zip', 'isinstance', 'any', 'all', 'map', 'filter', 'reversed', 'divmod',
)}
_SAFE_BUILTINS['range'] = _capped_range

# pd/np 모듈을 그대로 주면 pd.io.common.os처럼 모듈 속성을 타고 os(파일 삭제 등)에 닿을 수 있었다.
# 분석에 필요한 함수만 담은 대리 객체를 준다 - 별칭(p = pd)을 써도 없는 속성은 없다.
_PD_ALLOWED = (
    'DataFrame', 'Series', 'Index', 'MultiIndex', 'Categorical', 'CategoricalDtype', 'Timestamp', 'Timedelta',
    'Period', 'NaT', 'NA', 'to_datetime', 'to_numeric', 'to_timedelta', 'date_range', 'period_range', 'merge',
    'merge_asof', 'concat', 'cut', 'qcut', 'pivot_table', 'crosstab', 'melt', 'get_dummies', 'isna', 'isnull',
    'notna', 'notnull', 'unique', 'factorize', 'Grouper', 'IndexSlice', 'DateOffset',
)
_NP_ALLOWED = (
    'nan', 'inf', 'where', 'select', 'round', 'mean', 'median', 'sum', 'std', 'var', 'min', 'max', 'abs', 'sqrt',
    'log', 'log1p', 'exp', 'percentile', 'quantile', 'clip', 'arange', 'linspace', 'array', 'isnan', 'isfinite',
    'maximum', 'minimum', 'cumsum', 'diff', 'sort', 'argsort', 'unique', 'floor', 'ceil', 'int64', 'float64',
    'divide', 'nanmean', 'nansum', 'nanmedian', 'count_nonzero', 'histogram', 'corrcoef', 'average', 'sign',
    'zeros', 'ones', 'full', 'concatenate', 'repeat', 'tile', 'logical_and', 'logical_or', 'logical_not', 'isin',
)
_SAFE_PD = types.SimpleNamespace(**{n: getattr(pd, n) for n in _PD_ALLOWED})
_SAFE_NP = types.SimpleNamespace(**{n: getattr(np, n) for n in _NP_ALLOWED})

_PRELOADED_IMPORT = re.compile(r'^\s*import\s+(pandas|numpy)(\s+as\s+\w+)?\s*$', re.MULTILINE)


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


def normalize_audience(value):
    """🌟 [분석 → 타겟 연결] 코드가 `대상자`에 담은 집단(set/list/Series/R고객번호 컬럼이 있는 표)을 고객번호 문자열 목록으로."""
    if value is None:
        return None
    try:
        if isinstance(value, pd.DataFrame):
            if 'R고객번호' not in value.columns:
                return None
            value = value['R고객번호']
        items = value.tolist() if isinstance(value, (pd.Series, pd.Index, np.ndarray)) else list(value)
        ids = sorted({str(v).strip() for v in items if not pd.isna(v) and str(v).strip()})
        return ids[:MAX_AUDIENCE] or None
    except Exception:
        return None


def execute(code, tables, previous, region_order, timeout=30):
    """코드를 데이터 복사본으로 실행. 반환: {'value', 'printed', 'audience'} 또는 {'error', 'printed'}.
    timeout이 있으면 스레드로 돌려 응답만 끊는다(계산 자체는 못 멈추므로 격리 프로세스를 우선 쓴다). None이면 그 자리에서 실행."""
    code = _PRELOADED_IMPORT.sub('', code)  # pd/np는 이미 준비돼 있으니 그 import 줄만 지운다(다른 import는 막힘)
    problem = check_code(code)
    if problem:
        return {'error': problem, 'printed': ''}
    out = io.StringIO()

    def _print(*args, sep=' ', end='\n', **_):
        out.write(sep.join(str(a) for a in args) + end)

    namespace = {
        '__builtins__': {**_SAFE_BUILTINS, 'print': _print},
        'pd': _SAFE_PD, 'np': _SAFE_NP, 'SO권역순서': list(region_order), '이전결과': dict(previous),
        **{name: df.copy() for name, df in tables.items()},
    }
    outcome = {}

    def _target():
        try:
            exec(compile(code, '<분석코드>', 'exec'), namespace)
            # AI가 자주 영어 이름(result)에 담는다 - 같은 뜻으로 받아준다
            outcome['value'] = namespace['결과'] if namespace.get('결과') is not None else namespace.get('result')
            outcome['audience'] = normalize_audience(namespace.get('대상자'))
        except Exception as e:  # AI 코드의 어떤 오류든 AI에게 돌려줘서 고치게 한다
            outcome['error'] = f"{type(e).__name__}: {e}"

    if timeout:
        worker = threading.Thread(target=_target, daemon=True)
        worker.start()
        worker.join(timeout)
        if worker.is_alive():
            return {'error': f"{timeout}초 안에 끝나지 않았어요. 더 가벼운 방법으로 계산해 주세요.", 'printed': out.getvalue()[-3000:]}
    else:
        _target()
    printed = out.getvalue()[-3000:]
    if 'error' in outcome:
        return {'error': outcome['error'], 'printed': printed}
    if outcome.get('value') is None:
        return {'error': "`결과` 변수에 값이 없어요. 보여줄 결과를 `결과 = ...`로 담아 주세요.", 'printed': printed}
    return {'value': outcome['value'], 'printed': printed, 'audience': outcome.get('audience')}


# ------------------------------------------------------------
# 2) 별도 프로세스 격리
# ------------------------------------------------------------
_EOF = object()


def _send(stream, obj):
    data = pickle.dumps(obj, protocol=4)
    stream.write(struct.pack('>Q', len(data)))
    stream.write(data)
    stream.flush()


def _recv(stream):
    header = stream.read(8)
    if len(header) < 8:
        return _EOF
    (size,) = struct.unpack('>Q', header)
    data = stream.read(size)
    if len(data) < size:
        return _EOF
    return pickle.loads(data)


def _safe_payload(result):
    """결과를 프로세스 밖으로 돌려주기 전에: 너무 큰 표는 자르고, 직렬화할 수 없는 값은 글자로 바꾼다."""
    value = result.get('value')
    if isinstance(value, (pd.DataFrame, pd.Series)) and len(value) > MAX_RESULT_ROWS:
        result['value'] = value.head(MAX_RESULT_ROWS)
    try:
        pickle.dumps(result, protocol=4)
    except Exception:
        result['value'] = str(result.get('value'))
    return result


def _apply_memory_limit():
    """리눅스에서만 프로세스 메모리 상한을 건다(윈도우에는 없음). 실패해도 그냥 진행한다."""
    try:
        import resource
        limit_mb = int(os.environ.get('HP_SANDBOX_MEM_MB', '0'))
        if limit_mb > 0:
            limit = limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except Exception:
        pass


def worker_main():
    """격리 프로세스의 본체: 표를 한 번 받아 두고, 코드를 받는 대로 실행해 결과를 돌려준다."""
    stdin, stdout = sys.stdin.buffer, sys.stdout.buffer
    sys.stdout = sys.stderr  # 코드 안의 출력이 통신 채널을 오염시키지 않게
    _apply_memory_limit()
    init = _recv(stdin)
    if init is _EOF:
        return
    tables, region_order, values = init['tables'], init['region_order'], dict(init['previous'])
    _send(stdout, {'ready': True})
    while True:
        message = _recv(stdin)
        if message is _EOF or message.get('quit'):
            return
        result = execute(message['code'], tables, values, region_order, timeout=None)
        if 'value' in result:
            values[message['n']] = result['value']
        _send(stdout, _safe_payload(result))


class SandboxUnavailable(Exception):
    """격리 프로세스를 시작하지 못했다(호출부가 같은 프로세스 실행으로 대신한다)."""


class SandboxSession:
    """한 번의 분석 턴 동안 쓰는 격리 프로세스. 표는 시작할 때 한 번만 보내고, 단계마다 코드만 보낸다."""

    def __init__(self, tables, region_order, timeout=30, memory_mb=2500, startup_timeout=90):
        self.tables, self.region_order = tables, list(region_order)
        self.timeout, self.memory_mb, self.startup_timeout = timeout, memory_mb, startup_timeout
        self.values = {}   # 성공한 단계 결과 - 프로세스가 죽어 다시 시작할 때 이어서 넘긴다
        self.proc = None
        self._queue = None
        self._start()

    def _start(self):
        env = {**os.environ, 'PYTHONPATH': ROOT + os.pathsep + os.environ.get('PYTHONPATH', ''),
               'OPENBLAS_NUM_THREADS': '1', 'OMP_NUM_THREADS': '1', 'MKL_NUM_THREADS': '1',
               'PYTHONIOENCODING': 'utf-8', 'HP_SANDBOX_MEM_MB': str(self.memory_mb)}
        try:
            self.proc = subprocess.Popen(
                [sys.executable, '-m', 'services.code_sandbox'], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, cwd=ROOT, env=env,
                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
            )
        except Exception as e:
            raise SandboxUnavailable(f"프로세스를 시작하지 못했어요: {e}")
        self._queue = queue.Queue()
        threading.Thread(target=self._reader, args=(self.proc, self._queue), daemon=True).start()
        try:
            _send(self.proc.stdin, {'tables': self.tables, 'region_order': self.region_order, 'previous': self.values})
            ready = self._queue.get(timeout=self.startup_timeout)
        except Exception as e:  # 시작 중 종료/시간 초과
            self._kill()
            raise SandboxUnavailable(f"프로세스 준비에 실패했어요: {type(e).__name__}")
        if ready is _EOF or not ready.get('ready'):
            self._kill()
            raise SandboxUnavailable("프로세스가 준비 신호 없이 종료됐어요")

    @staticmethod
    def _reader(proc, q):
        while True:
            message = _recv(proc.stdout)
            q.put(message)
            if message is _EOF:
                return

    def _kill(self):
        proc, self.proc = self.proc, None
        if proc is None:
            return
        try:
            proc.kill()
            proc.wait(timeout=5)
        except Exception:
            pass
        for stream in (proc.stdin, proc.stdout):
            try:
                stream.close()
            except Exception:
                pass

    def run(self, code, step_number):
        """코드 한 단계 실행. 시간 초과/프로세스 종료도 오류 결과로 돌려주고, 다음 호출 때 새 프로세스로 이어간다."""
        if self.proc is None or self.proc.poll() is not None:
            self._kill()
            self._start()  # 시작 실패는 SandboxUnavailable로 올라간다
        try:
            _send(self.proc.stdin, {'code': code, 'n': step_number})
        except Exception:
            self._kill()
            return {'error': "분석 프로세스가 중단돼서 다시 시작해야 해요. 같은 코드를 다시 실행해 주세요.", 'printed': ''}
        try:
            message = self._queue.get(timeout=self.timeout)
        except queue.Empty:
            self._kill()
            return {'error': f"{self.timeout}초 안에 끝나지 않아 계산을 중단했어요. 더 가벼운 방법으로 계산해 주세요.", 'printed': ''}
        if message is _EOF:
            self._kill()
            return {'error': "분석 프로세스가 갑자기 종료됐어요(메모리를 너무 많이 썼을 수 있어요). 더 작은 범위로 나눠서 계산해 주세요.",
                    'printed': ''}
        if 'value' in message:
            self.values[step_number] = message['value']
        return message

    def close(self):
        if self.proc is not None:
            try:
                _send(self.proc.stdin, {'quit': True})
            except Exception:
                pass
        self._kill()


if __name__ == "__main__":
    worker_main()
