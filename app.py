"""Street Smarts 信号查询网页：输入代码即可查看书中策略信号。

本地运行：  streamlit run app.py
"""
from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _reload_if_changed():
    """Streamlit Cloud 拉取新代码后只重跑 app.py，已导入的 ss_scanner 模块仍是旧版本；检测到文件变化时强制重新导入。"""
    pkg = Path(__file__).parent / "ss_scanner"
    stamp = tuple(sorted((f.name, f.stat().st_mtime_ns, f.stat().st_size) for f in pkg.glob("*.py")))
    old = sys.modules.get("ss_scanner")
    if old is not None and getattr(old, "_stamp", None) != stamp:
        for name in [m for m in sys.modules if m == "ss_scanner" or m.startswith("ss_scanner.")]:
            del sys.modules[name]
    import ss_scanner
    ss_scanner._stamp = stamp


_reload_if_changed()

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from ss_scanner.data import load_history
from ss_scanner.engine import backtest_instrument, scan_instrument, sides_for, summarize
from ss_scanner.markets import MARKETS, last_bar_incomplete, normalize_symbol
from ss_scanner.strategies import STRATEGIES, STRATEGY_MAP

st.set_page_config(page_title="Street Smarts 信号查询", page_icon="📈", layout="centered")


# ------------------------------------------------------------------ 数据（缓存 30 分钟）
@st.cache_data(ttl=1800, show_spinner=False)
def get_data(raw: str, period: str, source: str):
    inst = normalize_symbol(raw)
    df = load_history(inst, period=period, source=source, use_cache=False)
    return inst, df


@st.cache_data(ttl=3600, show_spinner=False)
def watch_names() -> dict:
    """自选股清单里的中文名：Yahoo 代码 → 名称。"""
    try:
        from ss_scanner.watchlist import read_csv
        df = read_csv("config/watchlist.csv")
        return {normalize_symbol(r["symbol"]).candidates[0]: r["name"] for _, r in df.iterrows() if r["symbol"]}
    except Exception:
        return {}


@st.cache_data(ttl=86400, show_spinner=False)
def get_name(yahoo: str) -> str:
    if yahoo in watch_names():
        return watch_names()[yahoo]
    try:
        import yfinance as yf
        info = yf.Ticker(yahoo).info
        return info.get("shortName") or info.get("longName") or ""
    except Exception:
        return ""


def chart(df: pd.DataFrame, sig: pd.DataFrame, title: str) -> go.Figure:
    d = df.tail(120)
    fig = go.Figure(go.Candlestick(x=d.index, open=d["open"], high=d["high"], low=d["low"], close=d["close"],
                                   increasing_line_color="#E5484D", decreasing_line_color="#30A46C", name="K线"))
    ema = df["close"].ewm(span=20, adjust=False).mean().tail(120)
    fig.add_trace(go.Scatter(x=d.index, y=ema, line=dict(color="#F5A524", width=1.2), name="EMA20"))
    seen = set()
    for _, r in sig.iterrows():
        key = (r.get("入场价"), r.get("止损"))
        if key in seen:
            continue
        seen.add(key)
        if pd.notna(r.get("入场价")):
            fig.add_hline(y=r["入场价"], line_dash="dash", line_color="#2E6BE6", line_width=1,
                          annotation_text=f"入场 {r['入场价']}", annotation_position="top left")
        if pd.notna(r.get("止损")):
            fig.add_hline(y=r["止损"], line_dash="dot", line_color="#888", line_width=1,
                          annotation_text="止损", annotation_position="bottom left")
    fig.update_layout(height=380, margin=dict(l=10, r=10, t=30, b=10), title=title,
                      xaxis_rangeslider_visible=False, showlegend=False,
                      xaxis=dict(rangebreaks=[dict(bounds=["sat", "mon"])]))
    return fig


# ------------------------------------------------------------------ 侧边栏
with st.sidebar:
    st.header("⚙️ 设置")
    opts = {s.id: f"{s.name}（{s.chapter}）" for s in STRATEGIES}
    chosen = st.multiselect("启用策略", list(opts), default=[s.id for s in STRATEGIES if s.default],
                            format_func=lambda x: opts[x])
    long_only = st.checkbox("只看做多", value=False)
    allow_short = st.checkbox("A股也显示做空（需融券）", value=False)
    source = st.selectbox("数据源", ["yahoo", "akshare"], format_func=lambda x: {
        "yahoo": "Yahoo Finance（四市场）", "akshare": "AkShare（A股，失败自动回退 Yahoo）"}[x])
    recent_n = st.slider("跟踪最近 N 根K线已触发的信号", 1, 15, 5)
    st.caption("代码写法：AAPL｜600519 / 300750｜0700 / 9988｜005930.KS / 247540.KQ")

# ------------------------------------------------------------------ 主界面
st.title("📈 Street Smarts 信号查询")
st.caption("输入股票代码，判断是否满足《Street Smarts》(Connors & Raschke) 书中的短线交易信号 · 美股 / A股 / 港股 / 韩股")

with st.form("q"):
    text = st.text_input("股票代码（多个用空格或逗号分隔）", value=st.query_params.get("s", "AAPL 0700 600519 005930.KS"),
                         placeholder="例如：AAPL 0700 600519 005930.KS")
    submitted = st.form_submit_button("🔍 查询信号", width="stretch", type="primary")

tab_sig, tab_watch, tab_bt, tab_help = st.tabs(["信号", "自选股每日扫描", "历史回测", "策略说明"])
symbols = [s for s in re.split(r"[\s,，;；]+", text.strip()) if s][:10]
if submitted:
    st.query_params["s"] = " ".join(symbols)

with tab_sig:
    if not chosen:
        st.warning("请至少启用一个策略")
    for raw in symbols:
        try:
            with st.spinner(f"获取 {raw} 行情…"):
                inst, df = get_data(raw, "2y", source)
        except Exception as e:
            st.error(f"{raw}：{e}")
            continue
        name = get_name(inst.yahoo)
        mkt = MARKETS[inst.market]
        last = df.iloc[-1]
        chg = (last["close"] / df["close"].iloc[-2] - 1) * 100 if len(df) > 1 else 0.0
        with st.container(border=True):
            c1, c2 = st.columns([3, 2])
            c1.subheader(f"{inst.yahoo} {name}")
            c1.caption(f"{mkt.name} · {mkt.currency} · 最新K线 {df.index[-1]:%Y-%m-%d}")
            c2.metric("收盘价", f"{last['close']:,.4g}" if last["close"] < 1000 else f"{last['close']:,.0f}", f"{chg:+.2f}%", delta_color="inverse")
            if last_bar_incomplete(inst.market, df.index[-1]):
                st.info("⏱️ 当前处于交易时段，最后一根K线未收盘，信号为盘中临时结果。")
            if len(df) < 60:
                st.warning(f"数据不足（{len(df)} 根K线，至少需 60）")
                continue
            if len(df) < 120:
                st.caption(f"ℹ️ 上市仅 {len(df)} 个交易日，HV 等长周期策略不可用")
            new, recent = scan_instrument(df, inst, chosen, sides_for(inst, long_only, allow_short),
                                          recent=recent_n, min_bars=60)
            sig = pd.DataFrame(new)
            if sig.empty:
                st.success("今日没有满足书中策略的信号。")
            else:
                st.markdown(f"**🎯 今日信号 {len(sig)} 条**")
                for _, r in sig.iterrows():
                    icon = "🟢" if r["方向"] == "做多" else "🔴"
                    with st.expander(f"{icon} {r['策略']} · {r['方向']} · {r['类型']}", expanded=True):
                        e = "—" if pd.isna(r["入场价"]) else f"{r['入场价']:,}"
                        sl = "见操作" if pd.isna(r["止损"]) else f"{r['止损']:,}"
                        rk = "" if pd.isna(r["风险%"]) else f"　风险 **{r['风险%']}%**"
                        st.markdown(f"入场 **{e}**　止损 **{sl}**{rk}")
                        st.markdown(f"**操作**：{r['操作']}")
                        if r["备注"]:
                            st.caption(r["备注"])
                        if r["做空限制"]:
                            st.caption("⚠️ " + r["做空限制"])
            st.plotly_chart(chart(df, sig, "近 120 日 K线（虚线=入场，点线=止损）"), width="stretch",
                            config={"displayModeBar": False})
            if recent:
                with st.expander(f"📋 最近 {recent_n} 根K线已触发的信号（{len(recent)} 条）"):
                    st.dataframe(pd.DataFrame(recent).drop(columns=["市场", "代码"]), hide_index=True,
                                 width="stretch")

with tab_watch:
    LATEST = Path("latest")
    SESS = {"us_eu": "美欧收盘（美股/英股/德股）", "asia": "亚洲收盘（A股/港股/韩股/日股）", "all": "全部市场"}
    avail = [k for k in SESS if (LATEST / f"{k}_summary.json").exists()]
    if not avail:
        st.info("还没有每日扫描结果。GitHub Actions 每个交易日收盘后会自动运行并把结果写入 latest/ 目录。")
    else:
        sess = st.radio("时段", avail, format_func=lambda k: SESS[k], horizontal=True)
        summ = json.loads((LATEST / f"{sess}_summary.json").read_text(encoding="utf-8"))
        st.caption(f"K线日期 {summ['bar_date']} · 扫描于 {summ['time']}（北京时间） · 自选 {summ['n_inst']} 只，"
                   f"有效扫描 {summ['n_scanned']} 只 · 新信号 {summ['n_signals']} 条")
        f = LATEST / f"{sess}_signals.csv"
        sig = pd.read_csv(f) if f.exists() and f.stat().st_size > 5 else pd.DataFrame()
        if sig.empty:
            st.success("最近一次扫描没有新信号。")
        else:
            sig["分组"] = sig["分组"].fillna("")
            c1, c2, c3 = st.columns(3)
            kinds = c1.multiselect("类型", list(sig["类型"].unique()), default=[k for k in ["挂单", "收盘入场"]
                                                                              if k in set(sig["类型"])])
            strats = c2.multiselect("策略", sorted(sig["策略"].unique()))
            mkts = c3.multiselect("市场", sorted(sig["市场"].unique()))
            c4, c5, c6 = st.columns(3)
            groups = sorted({g for x in sig["分组"] for g in str(x).split("、") if g})
            grp = c4.multiselect("自选分组", groups)
            side = c5.selectbox("方向", ["全部", "做多", "做空"])
            kw = c6.text_input("代码/名称包含")
            d = sig
            if kinds:
                d = d[d["类型"].isin(kinds)]
            if strats:
                d = d[d["策略"].isin(strats)]
            if mkts:
                d = d[d["市场"].isin(mkts)]
            if grp:
                d = d[d["分组"].apply(lambda x: bool(set(str(x).split("、")) & set(grp)))]
            if side != "全部":
                d = d[d["方向"] == side]
            if kw:
                d = d[d["代码"].astype(str).str.contains(kw, case=False) | d["名称"].astype(str).str.contains(kw, case=False)]
            st.markdown(f"**{len(d)} 条**（点击代码可在「信号」页查看K线图）")
            show = d.copy()
            base = st.context.url.split("?")[0] if hasattr(st, "context") and getattr(st.context, "url", None) else ""
            show["代码"] = show["代码"].astype(str).map(lambda c: f"{base}?s={c}")
            st.dataframe(show[[c for c in ["代码", "名称", "市场", "策略", "方向", "类型", "入场价", "止损", "风险%",
                                           "距入场%", "收盘价", "操作", "分组", "备注"] if c in show]],
                         hide_index=True, width="stretch",
                         column_config={"代码": st.column_config.LinkColumn("代码", display_text=r"\?s=(.*)")})
            st.download_button("下载 CSV", d.to_csv(index=False).encode("utf-8-sig"), f"{sess}_signals.csv")
        p = LATEST / f"{sess}_positions.csv"
        pos = pd.read_csv(p) if p.exists() and p.stat().st_size > 5 else pd.DataFrame()
        if not pos.empty:
            with st.expander(f"📋 近 5 日已触发、仍持有中的模拟仓位（{len(pos)}）"):
                st.dataframe(pos, hide_index=True, width="stretch")
        notes = [("无新K线", summ.get("stale")), ("数据失败", summ.get("errors")), ("低波动跳过", summ.get("lowvol")),
                 ("上市过短跳过", summ.get("skipped"))]
        for t, v in notes:
            if v:
                st.caption(f"{t}：" + "、".join(v))

with tab_bt:
    st.caption("日线级近似回测：信号级统计、同一策略不重叠持仓。用于验证策略在该标的上是否仍然有效。")
    c1, c2 = st.columns(2)
    years = c1.selectbox("回测区间", ["5y", "10y", "max"], index=1)
    cost = c2.number_input("单边成本（bp）", 0.0, 100.0, 10.0, 1.0)
    if st.button("▶️ 运行回测", width="stretch"):
        rows = []
        with st.spinner("回测中…"):
            for raw in symbols:
                try:
                    inst, df = get_data(raw, years, source)
                except Exception as e:
                    st.error(f"{raw}：{e}")
                    continue
                rows += backtest_instrument(df, inst, chosen, sides_for(inst, long_only, allow_short))
        if not rows:
            st.info("无交易")
        else:
            t = pd.DataFrame(rows)
            s = summarize(t, cost, ["strategy", "side"])
            s["strategy"] = s["strategy"].map(lambda x: STRATEGY_MAP[x].name)
            s["side"] = s["side"].map({"long": "做多", "short": "做空"})
            st.dataframe(s.rename(columns={"strategy": "策略", "side": "方向"}), hide_index=True,
                         width="stretch")
            st.download_button("下载全部交易明细 CSV", t.to_csv(index=False).encode("utf-8-sig"),
                               "trades.csv", "text/csv", width="stretch")

with tab_help:
    st.markdown("""
| 策略 | 章节 | 设置（做多；做空相反） |
| --- | --- | --- |
| Turtle Soup | Ch4 | 跌破 20 日低点（低点距今≥4天）后回升到低点上方 5 tick 买入 |
| Turtle Soup +1 | Ch5 | 创 20 日新低且收盘≤前低 → 次日在前低挂买入止损 |
| 80-20's | Ch6 | 昨日开在区间上 20%、收在下 20% → 今日跌破昨低后回到昨低买入（日内） |
| Momentum Pinball | Ch7 | LBR/RSI<30 → 次日第一小时高点上方买入（需盘中数据） |
| The "Anti" | Ch9 | %D 向上、%K 回调后拐头 → 高点上方买入，持有 2-4 根 |
| Holy Grail | Ch10 | ADX>30 的强趋势首次回撤到 20EMA → 高点上方买入 |
| ADX Gapper | Ch11 | 强趋势中跳空低开于昨低 → 回到昨低买入 |
| Whiplash | Ch12 | 跳空低开但收盘强（收>开且在上半区）→ 收盘买入 |
| 三日缺口反转 | Ch13 | 未回补的向下缺口，3 天内突破缺口日高点买入 |
| ID/NR4 | Ch19 | 内包日 + 4 日最窄 → 次日双向突破挂单 |
| HV meets Crabel | Ch20 | 6日/100日波动率<50% 且内包/NR4 → 双向突破挂单 |

**信号类型**：挂单＝下一交易日直接挂；条件单＝满足前提（如先跌破某价）才挂；收盘入场＝需在当日收盘前确认；需盘中数据＝只提示设置。

**市场规则**：价位按各市场 tick 表取整；A股 T+1、涨跌停一字板无法成交；A股默认不显示做空；港股/韩股做空需借券。
""")
    st.warning("仅用于学习与研究，不构成投资建议。书中规则源自 1990 年代，参数需在当前市场重新验证。")
