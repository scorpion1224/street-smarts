import unittest

import numpy as np
import pandas as pd

from ss_scanner.engine import build_frame, generate, make_ctx, simulate
from ss_scanner.markets import normalize_symbol, tick_size


def base_df(n=160, seed=0, drift=0.0):
    rng = np.random.default_rng(seed)
    c = 100 + np.cumsum(rng.normal(drift, 1, n))
    o = c + rng.normal(0, 0.3, n)
    h = np.maximum(o, c) + rng.uniform(0.2, 1.0, n)
    l = np.minimum(o, c) - rng.uniform(0.2, 1.0, n)
    idx = pd.bdate_range("2024-01-01", periods=n)
    return pd.DataFrame({"open": o, "high": h, "low": l, "close": c}, index=idx)


def mirror(df):
    """价格倒置（以 1000 为轴），做多信号应在镜像数据上变成做空信号。"""
    return pd.DataFrame({"open": 1000 - df.open, "high": 1000 - df.low, "low": 1000 - df.high,
                         "close": 1000 - df.close}, index=df.index)


class TestMarkets(unittest.TestCase):
    def test_normalize(self):
        cases = {
            "AAPL": ("US", "AAPL"), "brk.b": ("US", "BRK-B"), "600519": ("CN", "600519.SS"),
            "SZ000001": ("CN", "000001.SZ"), "300750": ("CN", "300750.SZ"), "688981": ("CN", "688981.SS"),
            "700": ("HK", "0700.HK"), "00700.HK": ("HK", "0700.HK"), "HK:9988": ("HK", "9988.HK"),
            "005930.KS": ("KR", "005930.KS"), "KQ:247540": ("KR", "247540.KQ"), "CN:510300": ("CN", "510300.SS"),
        }
        for raw, (m, y) in cases.items():
            inst = normalize_symbol(raw)
            self.assertEqual((inst.market, inst.candidates[0]), (m, y), raw)
        self.assertEqual(normalize_symbol("KR:005930").candidates, ["005930.KS", "005930.KQ"])

    def test_tick(self):
        self.assertEqual(tick_size("HK", 350), 0.2)
        self.assertEqual(tick_size("HK", 5.5), 0.01)
        self.assertEqual(tick_size("KR", 60000), 100)
        self.assertEqual(tick_size("KR", 1500), 1)
        self.assertEqual(tick_size("CN", 4.1, "510300"), 0.001)
        self.assertEqual(tick_size("US", 0.5), 0.0001)


class TestStrategies(unittest.TestCase):
    inst = normalize_symbol("AAPL")

    def sigs(self, df, sid, side="long", i=None):
        F = build_frame(df, side)
        i = len(df) - 1 if i is None else i
        return F, generate(F, i, [sid], side, make_ctx(self.inst))

    def test_eighty_twenty_and_mirror(self):
        df = base_df()
        i = len(df) - 1
        prev = df.close.iloc[i - 1]
        df.iloc[i] = [prev + 2, prev + 2.2, prev - 3, prev - 2.8]  # 开在顶部，收在底部
        _, s = self.sigs(df, "eighty_twenty")
        self.assertEqual(len(s), 1)
        self.assertAlmostEqual(s[0].entry, round(df.low.iloc[i], 2) + (0.01 if round(df.low.iloc[i], 2) < df.low.iloc[i] else 0), places=2)
        _, s2 = self.sigs(mirror(df), "eighty_twenty", side="short")
        self.assertEqual(len(s2), 1)

    def test_id_nr4_both_sides(self):
        df = base_df()
        i = len(df) - 1
        p = df.iloc[i - 1]
        mid = (p.high + p.low) / 2
        df.iloc[i] = [mid, mid + 0.05, mid - 0.05, mid + 0.01]
        for side in ("long", "short"):
            _, s = self.sigs(df, "id_nr4", side)
            self.assertEqual(len(s), 1, side)

    def test_turtle_soup_plus_one(self):
        df = base_df(seed=3)
        i = len(df) - 1
        L = df.low.iloc[i - 20:i].min() - 5
        df.iloc[i - 10] = [L + 1, L + 2, L, L + 1.5]          # 前20日低点在 10 天前
        df.iloc[i] = [L + 0.5, L + 0.6, L - 1.0, L - 0.5]      # 今日创新低且收盘低于前低
        _, s = self.sigs(df, "turtle_soup_plus_one")
        self.assertEqual(len(s), 1)
        self.assertAlmostEqual(s[0].entry, round(L, 2) + (0.01 if round(L, 2) < L else 0), places=2)

    def test_mirror_symmetry_all_strategies(self):
        """镜像价格后：做多信号数 == 镜像数据上的做空信号数（HV 策略除外）。"""
        from ss_scanner.strategies import STRATEGIES
        df = base_df(n=400, seed=11)
        mdf = mirror(df)
        ids = [x.id for x in STRATEGIES if x.id != "hv_crabel"]
        cx = make_ctx(self.inst)
        F1, F2 = build_frame(df, "long"), build_frame(mdf, "short")
        for sid in ids:
            a = sum(len(generate(F1, i, [sid], "long", cx)) for i in range(120, 400))
            b = sum(len(generate(F2, i, [sid], "short", cx)) for i in range(120, 400))
            self.assertEqual(a, b, sid)

    def test_whiplash_and_sim(self):
        df = base_df()
        i = len(df) - 2
        pl = df.low.iloc[i - 1]
        df.iloc[i] = [pl - 2, pl + 0.5, pl - 2.2, pl + 0.3]   # 跳空低开、收在上半部
        df.iloc[i + 1] = [pl + 0.6, pl + 1.5, pl + 0.4, pl + 1.2]
        F, s = self.sigs(df, "whiplash", i=i)
        self.assertEqual(len(s), 1)
        tr = simulate(F, s[0], self.inst, make_ctx(self.inst))
        self.assertIsNotNone(tr)
        self.assertEqual(tr.fill_i, i)

    def test_stop_entry_fill_and_stop(self):
        df = base_df()
        i = len(df) - 3
        p = df.iloc[i - 1]
        mid = (p.high + p.low) / 2
        df.iloc[i] = [mid, mid + 0.05, mid - 0.05, mid]
        df.iloc[i + 1] = [mid, mid + 1, mid - 0.02, mid + 0.8]       # 向上突破成交
        df.iloc[i + 2] = [mid + 0.8, mid + 0.9, mid - 1, mid - 0.9]  # 跌破止损
        F, s = self.sigs(df, "id_nr4", i=i)
        tr = simulate(F, s[0], self.inst, make_ctx(self.inst))
        self.assertEqual(tr.fill_i, i + 1)
        self.assertEqual(tr.reason, "止损")
        self.assertLess(tr.ret(), 0)

    def test_t_plus_one_intraday(self):
        cn = normalize_symbol("600519")
        df = base_df()
        i = len(df) - 3
        prev = df.close.iloc[i - 1]
        df.iloc[i] = [prev + 2, prev + 2.2, prev - 3, prev - 2.8]
        L = df.low.iloc[i]
        df.iloc[i + 1] = [L - 0.1, L + 1, L - 1, L + 0.8]
        df.iloc[i + 2] = [L + 0.9, L + 1.2, L + 0.5, L + 1]
        F = build_frame(df, "long")
        cx = make_ctx(cn)
        s = generate(F, i, ["eighty_twenty"], "long", cx)
        tr = simulate(F, s[0], cn, cx)
        self.assertEqual(tr.exit_i, i + 2)  # A股 T+1：次日开盘平仓
        self.assertIn("T+1", tr.reason)


if __name__ == "__main__":
    unittest.main()


def test_longbridge_code_conversion():
    from ss_scanner.watchlist import convert_code
    assert convert_code("700.HK")[0] == "0700.HK"
    assert convert_code("BRK.B.US")[0] == "BRK.B"
    assert convert_code("600089.SH")[0] == "600089.SS"
    assert convert_code(".SPX.US")[0] == "^GSPC"
    assert convert_code("HSI.HK")[0] == "^HSI"
    assert convert_code("7203.JP")[0] == "7203.T"
    assert convert_code("HSBA.UK")[0] == "HSBA.L"
    assert convert_code("AAPL270115C300000.US")[0] is None      # 期权
    assert convert_code("12345.HK")[0] is None                  # 牛熊证
    assert convert_code("XX0000000000.FD")[0] is None           # 基金
    from ss_scanner.markets import normalize_symbol
    assert normalize_symbol("BRK.B").candidates == ["BRK-B"]
    assert normalize_symbol("^GSPC").is_index
    assert normalize_symbol("7203.T").market == "JP"


def test_daily_email_build():
    import pandas as pd
    from datetime import datetime
    from ss_scanner.daily import build_email, load_cfg, merge_brackets, should_send
    rows = [
        {"市场": "美股", "代码": "AAA", "名称": "甲", "策略": "ID/NR4", "strategy_id": "id_nr4", "方向": d,
         "类型": "挂单", "入场价": p, "止损": q, "风险%": 1.0, "距入场%": 0.5, "收盘价": 10.0, "操作": "x",
         "备注": "", "指数": ""}
        for d, p, q in [("做多", 10.1, 9.9), ("做空", 9.9, 10.1)]
    ]
    sig = pd.DataFrame(rows)
    m = merge_brackets(sig)
    assert len(m) == 1 and m.iloc[0]["方向"] == "双向"
    cfg = load_cfg(None)
    res = dict(session="us_eu", markets=["US"], time=datetime.now(), n_inst=1, n_scanned=1, signals=sig,
               positions=pd.DataFrame(), errors=[], stale=[], skipped=[], lowvol=[], bar_date="2026-01-02")
    subj, body, text = build_email(res, cfg)
    assert "挂单 2" in subj and "双向" in body
    assert should_send(res, cfg)
