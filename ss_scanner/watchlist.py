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
# 指数：Yahoo 历史日线不足，无法计算
NO_DATA = {"^HXC": "指数；Yahoo 历史日线不足，无法计算",
           "HSTECH.HK": "指数；Yahoo 历史日线不足，可用恒生科技 ETF 3033.HK 代替"}
# 韩国公司的美股 ADR / 场外代码 → 韩国交易所原股（清单中出现时自动补充原股，便于按韩股规则扫描）
KR_ORIGINALS = {
    "SSNGY": ("005930.KS", "三星电子"), "SSNLF": ("005930.KS", "三星电子"), "SKHY": ("000660.KS", "SK海力士"),
    "KB": ("105560.KS", "KB金融"), "SHG": ("055550.KS", "新韩金融"), "WF": ("316140.KS", "友利金融"),
    "PKX": ("005490.KS", "POSCO控股"), "KEP": ("015760.KS", "韩国电力"), "SKM": ("017670.KS", "SK电讯"),
    "KT": ("030200.KS", "KT"), "LPL": ("034220.KS", "LG显示"), "HYMTF": ("005380.KS", "现代汽车"),
}
# 已知在 Yahoo 无日线的韩国 ADR：导入时禁用（由原股替代）
KR_NO_DATA = {"SSNGY", "SSNLF", "HYMTF"}
LOCAL_DISABLED_NOTE = "（本地手动禁用，重新导入时保留）"


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
    if sym in KR_NO_DATA:
        return None, f"Yahoo 无该 ADR 日线；已改用韩股原股 {KR_ORIGINALS[sym][0]}"
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


def import_longbridge(xlsx: str | Path, add_extra: bool = True, keep_from: str | Path | None = None) -> pd.DataFrame:
    """keep_from：已有的 watchlist.csv；其中 enabled=0 的行（如 Yahoo 无数据而手动禁用）在重新导入后继续禁用。"""
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
        for r in list(rows):
            body = r["source_code"].upper().rsplit(".", 1)[0]
            if body in KR_ORIGINALS and KR_ORIGINALS[body][0] not in seen:
                sym, name = KR_ORIGINALS[body]
                seen.add(sym)
                rows.append(dict(symbol=sym, name=name, market="KR", groups=r["groups"], source_code="(自动添加)",
                                 enabled=1, note=f"韩股原股，对应 {r['source_code']}"))
    df = pd.DataFrame(rows, columns=COLUMNS)
    if keep_from and Path(keep_from).exists():
        old = read_csv(keep_from)
        off = old[old["enabled"].astype(str) == "0"]
        off_codes = dict(zip(off["source_code"], off["note"]))
        m = df["source_code"].isin(off_codes) & (df["enabled"].astype(int) == 1)
        df.loc[m, "enabled"] = 0
        df.loc[m, "note"] = df.loc[m, "source_code"].map(lambda c: (off_codes[c] or "") + LOCAL_DISABLED_NOTE)
    return df


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
