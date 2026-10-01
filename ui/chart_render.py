# ui/chart_render.py
# ============================================================
# 🌟 [모듈화] chart_spec(dict, JSON 직렬화 가능한 순수 피벗 데이터)을 plotly
# figure/표 DataFrame으로 되돌리는 공용 렌더링 로직(📊 분석 탭과, 예전에 저장된 보고서 대화의 차트가 함께 쓴다).
#
# 🌟 [다중 행/열/값] 행이 2개면 가로축 라벨을 "채널A · 여자"처럼 합치고, 측정값이 여러
# 개면 단위가 달라도 각각 잘 보이도록 측정값마다 그래프를 나눠 그린다(두 축 그래프는 쓰지 않음).
#
# 🌟 [목적별 그래프] 차트유형(막대/가로막대/선/누적막대/히트맵/증감)마다 그리는 방식이 다르다.
#   - 증감: 기준 대비 늘면 파랑, 줄면 빨강인 가로 막대(0 기준선), 큰 순으로 정렬
#   - 히트맵: 한 가지 색(파랑)의 진하기로 크기를 표현
#   - 계열(열 값)이 8개를 넘으면 색으로 구분할 수 없으므로 히트맵으로 바꿔 그린다
# 색은 고정 순서의 범주 팔레트를 쓰고, 같은 항목은 모든 그래프에서 같은 색이다.
# ============================================================
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots
from services.analysis_service import normalize_pivot_result, describe_pivot

# 색각 이상 검증을 통과한 범주 팔레트(순서가 곧 구분성 - 순서를 바꾸거나 돌려쓰지 않는다)
_CATEGORICAL = ['#2563EB', '#F97316', '#10B981', '#F59E0B', '#EC4899', '#0EA5E9', '#7C3AED', '#EF4444']  # 첫 색 = 앱 브랜드 파랑
_SEQUENTIAL = [[0, '#DBE7FE'], [0.25, '#93B4F8'], [0.5, '#4F86F0'], [0.75, '#2563EB'], [1, '#1E3A8A']]
_UP, _DOWN = '#2563EB', '#EF4444'
_GRID, _AXIS, _INK = '#E3E8F2', '#CBD5E1', '#334155'  # 앱 UI(chat_styles.py)와 같은 차가운 블루그레이
_BAR_LABEL_MAX_ROWS = 40   # 막대가 이보다 많으면 막대 위 숫자는 생략(빽빽해짐)
_HEATMAP_TEXT_MAX_CELLS = 300


def pivot_table_df(pivot_result):
    """chart_spec의 pivot 결과(dict)를 화면 표시용 DataFrame으로 되돌린다(증감 컬럼 포함)."""
    data = normalize_pivot_result(pivot_result or {})
    rows = data.get("결과") or []
    if not rows:
        return None
    df = pd.DataFrame(rows)
    series_cols = [c for cols in (data.get("계열") or {}).values() for c in cols]
    change_cols = [c for info in ((data.get("증감") or {}).get("측정값") or {}).values()
                   for c in (info.get("증감"), info.get("증감률")) if c]
    # 같은 컬럼이 두 번 들어가면 df[컬럼]이 표로 꺼내져 그래프가 깨진다 - 한 번씩만
    ordered_cols = [c for c in dict.fromkeys((data.get("행컬럼") or []) + series_cols + change_cols) if c in df.columns]
    return df[ordered_cols] if ordered_cols else df


def _category_labels(df, row_cols):
    return df[row_cols].astype(str).agg(' · '.join, axis=1) if len(row_cols) > 1 else df[row_cols[0]].astype(str)


def _fmt(v, unit=''):
    if v is None or pd.isna(v):
        return ''
    return f"{v:,.0f}{unit}" if float(v).is_integer() else f"{v:,.1f}{unit}"


def _effective_type(data):
    chart_type = data.get("차트유형") or '막대'
    has_cols = bool(data.get("열"))
    n_series = max((len(s) for s in (data.get("계열") or {}).values()), default=0)
    if chart_type == '증감' and not data.get("증감"):
        chart_type = '막대'
    if chart_type in ('누적막대', '히트맵') and not has_cols:
        chart_type = '막대'
    if chart_type in ('막대', '가로막대', '선', '누적막대') and n_series > len(_CATEGORICAL):
        chart_type = '히트맵'  # 9개 이상 계열은 색으로 구분 불가 → 크기를 진하기로
    return chart_type


def _change_figure(data, df, cats):
    """증감: 측정값마다 가로 막대. 늘면 파랑, 줄면 빨강, 0 기준선, 증감 큰 순."""
    change = data["증감"]
    measures = list(change["측정값"].keys())
    units = data.get("단위") or {}
    fig = make_subplots(rows=1, cols=len(measures), horizontal_spacing=0.12,
                        subplot_titles=[f"{m} 증감" for m in measures] if len(measures) > 1 else None)
    for i, m in enumerate(measures, start=1):
        info = change["측정값"][m]
        unit = units.get(m, '')
        part = pd.DataFrame({
            'cat': cats, 'diff': pd.to_numeric(df[info['증감']]),
            'base': df[info['기준']], 'comp': df[info['비교']],
            'rate': pd.to_numeric(df[info['증감률']]) if info.get('증감률') else None,
        }).sort_values('diff')
        rate_unit = '%' if info.get('증감률') else ''
        texts = [
            f"{'+' if d > 0 else ''}{_fmt(d, unit if unit != '%' else '%p')}"
            + (f" ({'+' if r > 0 else ''}{_fmt(r, '%')})" if rate_unit and pd.notna(r) else '')
            for d, r in zip(part['diff'], part['rate'] if info.get('증감률') else [None] * len(part))
        ]
        # 막대 끝 숫자가 잘리지 않게 양쪽에 여백을 둔다
        lo, hi = min(part['diff'].min(), 0), max(part['diff'].max(), 0)
        pad = (hi - lo or 1) * 0.35
        fig.update_xaxes(range=[lo - (pad if lo < 0 else 0), hi + (pad if hi > 0 else 0)], row=1, col=i)
        fig.add_trace(go.Bar(
            y=part['cat'], x=part['diff'], orientation='h', text=texts, textposition='outside', cliponaxis=False,
            marker_color=[_UP if d >= 0 else _DOWN for d in part['diff']], showlegend=False,
            customdata=list(zip(part['base'].map(lambda v: _fmt(v, unit)), part['comp'].map(lambda v: _fmt(v, unit)))),
            hovertemplate=f"%{{y}}<br>{change['기준']}: %{{customdata[0]}}<br>{change['비교']}: %{{customdata[1]}}"
                          "<br>증감: %{text}<extra></extra>",
        ), row=1, col=i)
        fig.add_vline(x=0, line_color=_AXIS, line_width=1, row=1, col=i)
    return fig, max(420, 28 * len(cats) + 160)


def _heatmap_figure(data, df, cats):
    measures = list(data["계열"].keys())
    units = data.get("단위") or {}
    labels = data.get("계열라벨") or {}
    fig = make_subplots(rows=len(measures), cols=1, vertical_spacing=0.1,
                        subplot_titles=[f"{m}({units.get(m, '')})" for m in measures])
    for i, m in enumerate(measures, start=1):
        series = data["계열"][m]
        z = df[series].apply(pd.to_numeric).T.values  # 세로: 열 값, 가로: 행 항목
        show_text = z.size <= _HEATMAP_TEXT_MAX_CELLS
        fig.add_trace(go.Heatmap(
            x=list(cats), y=[labels.get(s, s) for s in series], z=z, colorscale=_SEQUENTIAL,
            text=[[_fmt(v) for v in row] for row in z] if show_text else None,
            texttemplate='%{text}' if show_text else None, xgap=2, ygap=2,
            colorbar=dict(len=1 / len(measures), y=1 - (i - 0.5) / len(measures), title=units.get(m, '')),
            hovertemplate=f"%{{x}} · %{{y}}<br>{m}: %{{z:,.1f}}{units.get(m, '')}<extra></extra>",
        ), row=i, col=1)
    height = max(420, len(measures) * (26 * max(len(s) for s in data["계열"].values()) + 140))
    return fig, height


def _series_figure(data, df, cats, chart_type):
    """막대/가로막대/선/누적막대: 측정값마다 그래프를 나누고, 같은 열 값은 같은 색."""
    measures = list(data["계열"].keys())
    units = data.get("단위") or {}
    labels = data.get("계열라벨") or {}
    horizontal = chart_type == '가로막대'
    n = len(measures)

    order = list(cats)
    if horizontal:
        # 순위가 한눈에 보이게 첫 측정값 기준 큰 순(가로 막대는 위가 큰 값)
        first = data["계열"][measures[0]]
        totals = df[first].apply(pd.to_numeric).sum(axis=1)
        order = [c for _, c in sorted(zip(totals, cats), key=lambda x: (x[0] if pd.notna(x[0]) else float('-inf')))]

    fig = (make_subplots(rows=1, cols=n, shared_yaxes=True, horizontal_spacing=0.06,
                         subplot_titles=[f"{m}({units.get(m, '')})" for m in measures] if n > 1 else None)
           if horizontal else
           make_subplots(rows=n, cols=1, shared_xaxes=True, vertical_spacing=0.12 if n > 1 else 0.02,
                         subplot_titles=[f"{m}({units.get(m, '')})" for m in measures] if n > 1 else None))
    colors, shown = {}, set()
    for i, m in enumerate(measures, start=1):
        pos = dict(row=1, col=i) if horizontal else dict(row=i, col=1)
        series = data["계열"][m]
        show_text = len(series) == 1 and len(df) <= _BAR_LABEL_MAX_ROWS
        for s in series:
            label = labels.get(s, s)
            color = colors.setdefault(label, _CATEGORICAL[len(colors) % len(_CATEGORICAL)])
            common = dict(name=label, legendgroup=label, showlegend=len(series) > 1 and label not in shown)
            shown.add(label)
            if chart_type == '선':
                fig.add_trace(go.Scatter(x=cats, y=df[s], mode='lines+markers', line=dict(color=color, width=2),
                                         marker=dict(size=8), **common), **pos)
                continue
            bar = dict(marker_color=color, text=[_fmt(v) for v in df[s]] if show_text else None,
                       textposition='outside' if show_text else None, **common)
            if horizontal:
                fig.add_trace(go.Bar(y=cats, x=df[s], orientation='h', **bar), **pos)
            else:
                fig.add_trace(go.Bar(x=cats, y=df[s], **bar), **pos)
        if n == 1:
            axis_title = f"{m}({units.get(m, '')})"
            (fig.update_xaxes if horizontal else fig.update_yaxes)(title_text=axis_title, **pos)

    if horizontal:
        fig.update_yaxes(categoryorder='array', categoryarray=order)
        height = max(420, 26 * len(cats) + 160)
    else:
        fig.update_xaxes(categoryorder='array', categoryarray=order)
        fig.update_xaxes(title_text=' · '.join(data.get('행') or []), row=n, col=1)
        height = max(420, 300 * n + 120)
    fig.update_layout(barmode='stack' if chart_type == '누적막대' else 'group', bargap=0.25, bargroupgap=0.06)
    return fig, height


def build_pivot_chart_figure(chart_spec):
    """chart_spec(dict)에서 plotly figure를 만든다. 저장/복원 시에는 이 dict만
    오가고, figure 자체는 매번 이 함수로 다시 만든다."""
    if not chart_spec or chart_spec.get("type") != "pivot":
        return None
    data = normalize_pivot_result(chart_spec.get("data") or {})
    df = pivot_table_df(data)
    if df is None or df.empty or not data.get("행컬럼"):
        return None

    cats = _category_labels(df, data["행컬럼"])
    chart_type = _effective_type(data)
    if chart_type == '증감':
        fig, height = _change_figure(data, df, cats)
    elif chart_type == '히트맵':
        fig, height = _heatmap_figure(data, df, cats)
    else:
        fig, height = _series_figure(data, df, cats, chart_type)

    title = describe_pivot(data)
    if chart_type == '증감':
        title = f"{'·'.join(data['행'])}별 {', '.join(data['증감']['측정값'])} 증감 ({data['증감']['기준']} → {data['증감']['비교']})"
    if data.get('데이터기준'):  # 시청이력 기준인지 누적 통계 기준인지 차트에서 바로 보이게
        title += f"<br><sup>{data['데이터기준']}</sup>"
    fig.update_layout(
        title=title, height=height, margin=dict(t=100, b=10, l=10, r=30), barcornerradius=4,
        paper_bgcolor='rgba(0,0,0,0)', plot_bgcolor='rgba(0,0,0,0)', font=dict(color=_INK, family='"Source Sans", "Malgun Gothic", sans-serif'),
        legend_title_text=' · '.join(data.get('열') or []) or None,
    )
    fig.update_xaxes(gridcolor=_GRID, linecolor=_AXIS, zeroline=False, automargin=True)
    fig.update_yaxes(gridcolor=_GRID, linecolor=_AXIS, zeroline=False, automargin=True)  # 긴 항목명이 잘리지 않게
    fig.update_traces(cliponaxis=False, selector=dict(type='bar'))
    return fig


def render_chart_card(chart_spec, key):
    """chart_spec이 있으면 그래프+표를 한 카드로 그린다(분석/보고서 탭 공용). 그래프 하나가 깨져도 화면 전체가 멈추지 않게 한다."""
    try:
        fig = build_pivot_chart_figure(chart_spec)
    except Exception:
        st.caption(":material/warning: 이 그래프는 그리지 못했어요.")
        return
    if fig is None:
        return
    with st.container(border=True):
        st.plotly_chart(fig, width='stretch', key=key)
        table_df = pivot_table_df(chart_spec.get("data") or {})
        if table_df is not None:
            with st.expander("표로 보기", icon=":material/table:"):
                st.dataframe(table_df, width='stretch')
