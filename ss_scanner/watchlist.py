"""自选股清单：从 Longbridge 导出的 Excel 导入为 config/watchlist.csv，并读取为 Instrument 列表。

watchlist.csv 列：symbol（扫描用代码）, name, market, groups, source_code（原始代码）, enabled, note
- enabled=0 的行会被跳过（期权、轮证/牛熊证、基金、无行情数据的标的），保留在文件中方便查看
- 可直接手工编辑 CSV：新增一行、改 enabled 即可
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

from .markets import Instrument, normalize_symbol

DEFAULT_CSV = Path("config/watchlist.csv")
COLUMNS = ["symbol", "name", "market", "groups", "source_code", "enabled", "note"]

# Longbridge 指数代码 → Yahoo
INDEX_MAP = {".SPX": "^GSPC", ".NDX": "^NDX", ".IXIC": "^IXIC", ".DJI": "^DJI", ".VIX": "^VIX",
             ".HXC": "^HXC", "HSI": "^HSI", "HSTECH": "HSTECH.HK", "HSCEI": "^HSCE"}
SFX_MAP = {"SH": "SS", "SZ": "SZ", "HK": "HK", "JP": "T", "UK": "L", "DE": "DE", "AU": "AX", "SG": "SI"}
_OPTION = re.compile(r"^[A-Z]+\d{6}[CP]\d+$")
# Yahoo 上无可用日线的标的（2026 实测）：code -> 原因
NO_DATA = {
           "^HXC": "指数；Yahoo 历史日线不足，无法计算",
           "HSTECH.HK": "指数；Yahoo 历史日线不足，可用恒生科技 ETF 3033.HK 代替"}
# 额外加入韩股原股（示例）
EXTRA = [
    dict(symbol="005930.KS", name="三星电子", market="KR", groups="示例", source_code="(自动添加)",
         enabled=1, note="韩股原股（示例）"),
    dict(symbol="000660.KS", name="SK海力士", market="KR", groups="示例", source_code="(自动添加)",
         enabled=1, note="韩股原股（示例）"),
]


def convert_code(code: str) -> tuple[str | None, str]:
    """Longbridge 代码 → (扫描代码 或 None, 备注)。"""
    c = str(code).strip().upper()
    if c.endswith(".FD"):
        return None, "基金，无日线行情"
    body, _, sfx = c.rpartition(".")
    if not body:
        body, sfx = c, ""
    if body in INDEX_MAP:
        sym = INDEX_MAP[body]
        return (None, NO_DATA[sym]) if sym in NO_DATA else (sym, "指数，仅作参考")
    if sfx == "US":
        if _OPTION.match(body):
            return None, "期权合约，不扫描"
        sym = body
    elif sfx == "HK":
        if body.isdigit() and len(body) == 5 and int(body) >= 10000:
            return None, "港股轮证/牛熊证，不扫描"
        sym = f"{int(body):04d}.HK" if body.isdigit() else f"{body}.HK"
    elif sfx in SFX_MAP:
        sym = f"{body}.{SFX_MAP[sfx]}"
    else:
        sym = c
    if sym in NO_DATA or body in NO_DATA:
        return None, NO_DATA.get(sym) or NO_DATA[body]
    return sym, ""


def _read_sheet(df: pd.DataFrame) -> pd.DataFrame | None:
    """兼容表头在第 1 行或在说明行之后。"""
    if "代码" in df.columns:
        return df
    for i in range(min(6, len(df))):
        row = [str(x).strip() for x in df.iloc[i].tolist()]
        if "代码" in row:
            out = df.iloc[i + 1:].copy()
            out.columns = row
            return out
    return None


def import_longbridge(xlsx: str | Path, add_extra: bool = True) -> pd.DataFrame:
    sheets = pd.read_excel(xlsx, sheet_name=None, dtype=str)
    rows, seen = [], set()
    for name, raw in sheets.items():
        if name in ("汇总",) or "韩股相关" in name:   # 韩股相关为交叉索引，不重复计入
            continue
        df = _read_sheet(raw)
        if df is None:
            continue
        for _, r in df.iterrows():
            code = str(r.get("代码") or "").strip()
            if not code or code.lower() == "nan":
                continue
            sym, note = convert_code(code)
            key = sym or code
            if key in seen:
                continue
            seen.add(key)
            groups = str(r.get("所属自选分组") or "").strip()
            mkt = ""
            if sym:
                try:
                    mkt = normalize_symbol(sym).market
                except Exception:
                    pass
            rows.append(dict(symbol=sym or "", name=str(r.get("名称") or "").strip(), market=mkt,
                             groups="" if groups.lower() == "nan" else groups.replace("、", ";"),
                             source_code=code, enabled=int(bool(sym)), note=note))
    if add_extra:
        for e in EXTRA:
            if e["symbol"] not in seen:
                rows.append(dict(e))
    return pd.DataFrame(rows, columns=COLUMNS)


def save_csv(df: pd.DataFrame, path: str | Path = DEFAULT_CSV) -> Path:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8")
    return path


def read_csv(path: str | Path = DEFAULT_CSV) -> pd.DataFrame:
    df = pd.read_csv(path, dtype=str, keep_default_na=False)
    for c in COLUMNS:
        if c not in df.columns:
            df[c] = ""
    return df


def load_instruments(path: str | Path = DEFAULT_CSV, markets: set[str] | None = None) -> list[Instrument]:
    df = read_csv(path)
    out, seen = [], set()
    for _, r in df.iterrows():
        if str(r["enabled"]).strip() in ("0", "false", "False", "") or not r["symbol"]:
            continue
        inst = normalize_symbol(r["symbol"])
        if markets and inst.market not in markets:
            continue
        key = (inst.market, inst.code)
        if key in seen:
            continue
        seen.add(key)
        inst.name, inst.groups = r["name"], r["groups"]
        out.append(inst)
    return out
