#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
三大法人籌碼流向(近 N 個交易日買賣超)更新腳本。

只做「流量」不做「持股水位」——這是刻意的設計決定:
交易所每日僅公布「外資及陸資持股比率」,投信與自營商的持股比率<b>從未</b>公布,
坊間數字多為歷史買賣超累加推估(不含借券/增資/初始部位,會漂移)。
與其三者口徑不一,不如一律只用官方買賣超,三種法人可直接互相比較。

資料源(皆為交易所官方,且都支援指定日期回補):
  上市 TWSE  T86                     欄位固定 19 欄
  上櫃 TPEx  3itrade_hedge_result    欄位固定 24 欄(名稱重複,只能靠位置)

寫入 data/chips.json,並填入各儀表板的 <div class="chips" data-code="..."> 佔位符。
執行:python3 scripts/update_chips.py
"""
import json
import os
import re
import sys
import urllib.request
from datetime import date, timedelta

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STOCKS_JSON = os.path.join(BASE, "data", "stocks.json")
CHIPS_JSON = os.path.join(BASE, "data", "chips.json")

TWSE_T86 = ("https://www.twse.com.tw/rwd/zh/fund/T86"
            "?date={d}&selectType=ALL&response=json")
TPEX_3INSTI = ("https://www.tpex.org.tw/web/stock/3insti/daily_trade/"
               "3itrade_hedge_result.php?l=zh-tw&se=EW&t=D&d={roc}&response=json")

WINDOW = 10   # 呈現用的交易日數
KEEP = 40     # chips.json 保留的交易日數(留餘裕供未來拉長視窗)
LOOKBACK = 40 # 回補時最多往前找幾個日曆日

SHARES_PER_LOT = 1000


def fetch(url, timeout=30, retries=3):
    last = None
    for _ in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (research-notes-updater)",
                "Accept": "application/json,text/csv,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:  # noqa: BLE001
            last = e
    raise last


def _num(s):
    """'-1,104,101' -> -1104101;空字串/破折號視為 0。"""
    s = str(s).strip().replace(",", "")
    if s in ("", "-", "--"):
        return 0
    return int(float(s))


def _lots(shares):
    """股 -> 張(台股慣例 1 張 = 1000 股),四捨五入到整張。"""
    return int(round(shares / SHARES_PER_LOT))


def parse_twse_chips(text):
    """
    TWSE T86 -> {code: {foreign, trust, dealer, total}}(單位:張)

    欄位位置(19 欄):
      [4] 外陸資買賣超(不含外資自營商)  [7] 外資自營商買賣超
      [10] 投信買賣超  [11] 自營商買賣超(自行+避險合計)  [18] 三大法人合計
    外資採 [4]+[7],與市場慣用口徑一致。
    回傳空 dict 代表非交易日或來源異常,呼叫端須視為失敗而非「當天沒人買賣」。
    """
    try:
        d = json.loads(text)
    except Exception:  # noqa: BLE001
        return {}
    if str(d.get("stat", "")).lower() != "ok":   # TWSE 回 'OK'、TPEx 回 'ok'
        return {}
    out = {}
    for r in d.get("data") or []:
        if len(r) < 19:
            continue
        code = str(r[0]).strip()
        if not re.fullmatch(r"\d{4}", code):   # 濾掉權證/ETN 等非四位數代號
            continue
        foreign = _num(r[4]) + _num(r[7])
        trust = _num(r[10])
        dealer = _num(r[11])
        total = _num(r[18])
        out[code] = {"foreign": _lots(foreign), "trust": _lots(trust),
                     "dealer": _lots(dealer), "total": _lots(total)}
    return out


def parse_tpex_chips(text):
    """
    TPEx 3itrade_hedge_result -> {code: {foreign, trust, dealer, total}}(單位:張)

    欄位位置(24 欄,名稱重複所以只能靠 index):
      [10] 外資及陸資買賣超(已含外資自營商)  [13] 投信買賣超
      [22] 自營商買賣超合計  [23] 三大法人買賣超合計
    """
    try:
        d = json.loads(text)
    except Exception:  # noqa: BLE001
        return {}
    if str(d.get("stat", "ok")).lower() != "ok":   # TPEx 用小寫 'ok'
        return {}
    tables = d.get("tables") or []
    rows = []
    for t in tables:
        if t.get("data"):
            rows = t["data"]
            break
    out = {}
    for r in rows:
        if len(r) < 24:
            continue
        code = str(r[0]).strip()
        if not re.fullmatch(r"\d{4}", code):
            continue
        out[code] = {"foreign": _lots(_num(r[10])), "trust": _lots(_num(r[13])),
                     "dealer": _lots(_num(r[22])), "total": _lots(_num(r[23]))}
    return out


def roc_str(d):
    """date -> '115/08/04'(TPEx 用民國年)"""
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def fetch_day(d):
    """
    抓單一日期兩市場。回傳 (twse_dict, tpex_dict)。
    twse 為空即視為非交易日(TWSE 是判斷基準,TPEx 偶有落後)。
    """
    ymd = d.strftime("%Y%m%d")
    try:
        twse = parse_twse_chips(fetch(TWSE_T86.format(d=ymd)))
    except Exception:  # noqa: BLE001
        twse = {}
    if not twse:
        return {}, {}
    try:
        tpex = parse_tpex_chips(fetch(TPEX_3INSTI.format(roc=roc_str(d))))
    except Exception:  # noqa: BLE001
        tpex = {}
    return twse, tpex


def collect(codes_twse, codes_tpex, have, need=WINDOW, today=None):
    """
    自今日往前補到湊滿 need 個交易日。have 是既有的 {ymd: {code: {...}}}。
    只保留我們追蹤的代號,檔案不會膨脹。
    """
    today = today or date.today()
    days = dict(have)
    cur = today
    for _ in range(LOOKBACK):
        if len([k for k in days if days[k]]) >= need:
            break
        ymd = cur.strftime("%Y%m%d")
        if ymd in days:
            cur -= timedelta(days=1)
            continue
        if cur.weekday() >= 5:            # 週末直接跳過,省 API 呼叫
            cur -= timedelta(days=1)
            continue
        twse, tpex = fetch_day(cur)
        if twse:
            row = {}
            for c in codes_twse:
                if c in twse:
                    row[c] = twse[c]
            for c in codes_tpex:
                if c in tpex:
                    row[c] = tpex[c]
            days[ymd] = row
            print(f"  抓到 {ymd}: twse {len(twse)} 檔 / tpex {len(tpex)} 檔 "
                  f"-> 命中追蹤股 {len(row)} 檔")
        cur -= timedelta(days=1)
    # 只留最近 KEEP 個交易日
    for k in sorted(days, reverse=True)[KEEP:]:
        days.pop(k)
    return days


def fmt_lots(v):
    """帶正負號與千分位的張數,並附上色 class。"""
    cls = "green" if v > 0 else ("red" if v < 0 else "")
    return f'<span class="{cls}">{v:+,}</span>' if cls else f"{v:+,}"


LABELS = [("foreign", "外資及陸資"), ("trust", "投信"),
          ("dealer", "自營商"), ("total", "三大法人合計")]


def build_chips_html(code, days):
    """
    days: {ymd: {code: {...}}};取最近 WINDOW 個有該股資料的交易日。
    找不到資料時回傳明確的「查無」字串,不編造 0。
    """
    ymds = [d for d in sorted(days, reverse=True) if code in days[d]][:WINDOW]
    if not ymds:
        return ('三大法人籌碼:<b>查無</b>(交易所來源未回傳此代號,'
                '可能為新上市/興櫃或當期無法人交易)')
    ymds = list(reversed(ymds))           # 由舊到新
    rows = [days[d][code] for d in ymds]

    def d2(s):
        return f"{s[4:6]}/{s[6:8]}"

    # 摘要表
    head = ("<table><tr><th>法人</th><th>近 %d 日累計</th>"
            "<th>買超天數</th><th>最近一日</th></tr>" % len(ymds))
    body = ""
    for key, label in LABELS:
        cum = sum(r[key] for r in rows)
        pos = sum(1 for r in rows if r[key] > 0)
        last = rows[-1][key]
        hl = ' class="hl"' if key == "total" else ""
        body += (f"<tr{hl}><td>{label}</td><td>{fmt_lots(cum)}</td>"
                 f"<td>{pos}/{len(rows)}</td><td>{fmt_lots(last)}</td></tr>")
    summary = head + body + "</table>"

    # 逐日明細(日期為列,行動裝置不會爆版)
    detail = ("<table style=\"margin-top:10px\"><tr><th>日期</th><th>外資及陸資</th>"
              "<th>投信</th><th>自營商</th><th>合計</th></tr>")
    for d, r in zip(reversed(ymds), reversed(rows)):   # 由新到舊,最新在最上
        detail += (f"<tr><td>{d2(d)}</td><td>{fmt_lots(r['foreign'])}</td>"
                   f"<td>{fmt_lots(r['trust'])}</td><td>{fmt_lots(r['dealer'])}</td>"
                   f"<td>{fmt_lots(r['total'])}</td></tr>")
    detail += "</table>"

    note = (f'<div class="note" style="font-size:11.5px;margin-top:8px;'
            f'color:var(--muted);line-height:1.7">'
            f'資料期間 {d2(ymds[0])}~{d2(ymds[-1])}(共 {len(ymds)} 個交易日)|單位:張|'
            f'外資及陸資已含外資自營商,自營商為自行買賣+避險合計|'
            f'來源:TWSE T86 / TPEx 三大法人買賣明細,每日盤後自動更新。<br>'
            f'僅呈現<b>買賣超流量</b>:交易所每日只公布外資及陸資持股比率,'
            f'投信與自營商持股比率未公布,故三者一律以流量口徑呈現以便直接比較。</div>')
    return summary + detail + note


def main():
    stocks = json.load(open(STOCKS_JSON, encoding="utf-8"))
    stocks.pop("_comment", None)
    codes_twse = [c for c, m in stocks.items() if m.get("market") == "twse"]
    codes_tpex = [c for c, m in stocks.items() if m.get("market") == "tpex"]
    tw_codes = codes_twse + codes_tpex
    if not tw_codes:
        sys.exit("no TW stocks in stocks.json")

    have = {}
    if os.path.exists(CHIPS_JSON):
        have = json.load(open(CHIPS_JSON, encoding="utf-8")).get("days", {})

    print(f"追蹤台股 {len(tw_codes)} 檔(上市 {len(codes_twse)}/上櫃 {len(codes_tpex)}),"
          f"既有 {len(have)} 個交易日")
    days = collect(codes_twse, codes_tpex, have)
    trading_days = sorted([d for d in days if days[d]], reverse=True)
    if len(trading_days) < WINDOW:
        print(f"WARN: 只湊到 {len(trading_days)} 個交易日(目標 {WINDOW})")

    json.dump({"updated": date.today().isoformat(), "window": WINDOW, "days": days},
              open(CHIPS_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)

    # 填入各儀表板佔位符
    filled, missing = 0, []
    for code, meta in stocks.items():
        if meta.get("market") == "us":
            continue
        path = os.path.join(BASE, meta["file"])
        if not os.path.exists(path):
            missing.append(meta["file"])
            continue
        s = open(path, encoding="utf-8").read()
        pat = rf'(<div class="chips" data-code="{re.escape(code)}">).*?(</div>\s*<!--/chips-->)'
        if not re.search(pat, s, flags=re.S):
            missing.append(f"{meta['file']}(無佔位符)")
            continue
        html = build_chips_html(code, days)
        s2 = re.sub(pat, lambda m: m.group(1) + html + m.group(2), s, count=1, flags=re.S)
        if s2 != s:
            open(path, "w", encoding="utf-8").write(s2)
        filled += 1

    def covered_in(codes):
        return sum(1 for c in codes if any(c in days[d] for d in trading_days))

    cov_tw, cov_tp = covered_in(codes_twse), covered_in(codes_tpex)
    covered = cov_tw + cov_tp
    print(f"done: 交易日 {len(trading_days)}/{WINDOW} | 有籌碼資料 {covered}/{len(tw_codes)} 檔 "
          f"(上市 {cov_tw}/{len(codes_twse)}、上櫃 {cov_tp}/{len(codes_tpex)}) "
          f"| 已填入 {filled} 份儀表板"
          + (f" | 未處理 {missing}" if missing else ""))
    # 歷史教訓:解析 0 筆照樣回報 success。任一市場全滅都代表該來源改版,必須讓 CI 轉紅
    # (曾因 TPEx 的 stat 是小寫 'ok' 而整個上櫃靜默歸零)。
    dead = [n for n, cov, tot in (("上市", cov_tw, len(codes_twse)),
                                  ("上櫃", cov_tp, len(codes_tpex)))
            if tot and cov == 0]
    if dead:
        sys.exit(f"FATAL: {'/'.join(dead)}全數無籌碼資料,來源可能已改版")


if __name__ == "__main__":
    main()
