#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
研究筆記每日更新腳本(盤後執行):
1. 抓 TWSE/TPEx 收盤價 → 更新 index.html 每張卡片的「現價偏移 % + 資料齡」(.fresh div)
2. 讀處置股儀表板(本機 ~/twse-disposition 優先,失敗改線上) → 同步卡片處置/注意徽章(.autodispo span)
3. 由 data/calendar.json + 處置迄日 產生催化劑日曆(CALENDAR_START/END 標記區)
4. 用 twse-disposition 的 stock_info.json tags 補充卡片 data-tags
5. 寫 data/prices.json 供其他儀表板取用
只改 index.html 的標記區與佔位元素,不動任何分析本文。
"""
import csv
import io
import json
import os
import re
import sys
import urllib.request
from datetime import date, datetime

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
INDEX = os.path.join(BASE, "index.html")
STOCKS_JSON = os.path.join(BASE, "data", "stocks.json")
CAL_JSON = os.path.join(BASE, "data", "calendar.json")
PRICES_JSON = os.path.join(BASE, "data", "prices.json")

TWSE_STOCK_DAY = "https://www.twse.com.tw/exchangeReport/STOCK_DAY_ALL?response=json"
TPEX_QUOTES = "https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes"
DISPO_LOCAL = os.path.expanduser("~/twse-disposition/index.html")
DISPO_URL = "https://nctuwanglin.github.io/stock-research-notes/../twse-disposition/"  # placeholder, real below
DISPO_URL = "https://nctuwanglin.github.io/twse-disposition/"
STOCKINFO_LOCAL = os.path.expanduser("~/twse-disposition/data/stock_info.json")
STOCKINFO_URL = "https://nctuwanglin.github.io/twse-disposition/data/stock_info.json"

TAG_ZH = {"memory": "記憶體", "packaging": "封測", "icmanufacturing": "晶圓代工",
          "icdesign": "IC設計", "power": "電源", "pcb": "PCB", "passive": "被動元件",
          "optical": "光學", "satellite": "衛星", "shipping": "航運",
          "finance": "金融", "telecom": "電信"}


def fetch(url, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (research-notes-updater)",
                "Accept": "text/csv,application/json,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except Exception as e:
            last = e
            import time
            time.sleep(2 * (i + 1))
    raise last


def fetch_twse_closes():
    """TWSE STOCK_DAY_ALL:response=json 實際回 CSV(欄位 0=民國日期 1=代號 8=收盤價)。"""
    try:
        text = fetch(TWSE_STOCK_DAY)
    except Exception as e:
        print(f"WARN twse fetch failed: {e}", file=sys.stderr)
        return "", {}
    resp_date, quotes = "", {}
    for r in csv.reader(io.StringIO(text)):
        if len(r) < 11 or not (r[0].isdigit() and len(r[0]) == 7):
            continue
        if not resp_date:
            resp_date = str(int(r[0][:3]) + 1911) + r[0][3:]
        code = r[1].strip()
        raw = r[8].strip()
        if code and raw not in ("", "--", "---"):
            try:
                quotes[code] = float(raw.replace(",", ""))
            except ValueError:
                pass
    return resp_date, quotes


def fetch_tpex_closes():
    """TPEx openapi 上櫃全股收盤(JSON array)。欄位名歷有變動,防禦性取值。"""
    try:
        data = json.loads(fetch(TPEX_QUOTES))
    except Exception as e:
        print(f"WARN tpex fetch failed: {e}", file=sys.stderr)
        return "", {}
    resp_date, quotes = "", {}
    for row in data if isinstance(data, list) else []:
        code = (row.get("SecuritiesCompanyCode") or row.get("Code") or "").strip()
        raw = (row.get("Close") or row.get("ClosingPrice") or "").strip()
        d = (row.get("Date") or row.get("DataDate") or "").strip()
        if d and not resp_date:
            resp_date = d.replace("/", "").replace("-", "")
            if len(resp_date) == 7:  # 民國
                resp_date = str(int(resp_date[:3]) + 1911) + resp_date[3:]
        if code and raw not in ("", "--", "---"):
            try:
                quotes[code] = float(raw.replace(",", ""))
            except ValueError:
                pass
    return resp_date, quotes


def load_dispo():
    """回傳 (處置 {code:{auction,end}}, 注意 set(codes))。本機優先,線上備援,都失敗回空。"""
    html = ""
    if os.path.exists(DISPO_LOCAL):
        html = open(DISPO_LOCAL, encoding="utf-8").read()
    else:
        try:
            html = fetch(DISPO_URL)
        except Exception as e:
            print(f"WARN dispo fetch failed: {e}", file=sys.stderr)
            return {}, set()
    dispo = {}
    pat = re.compile(
        r'class="ticker[^"]*"[^>]*>(\d{4,6})</span>.*?class="pill[^"]*">([^<]*撮合)</span>'
        r'(?:.*?~\s*([0-9/]+))?', re.S)
    for m in pat.finditer(html):
        code, auction, end = m.group(1), m.group(2), (m.group(3) or "")
        if code not in dispo:
            dispo[code] = {"auction": auction, "end": end}
    attn = set()
    m = re.search(r'注意累計[^：:]*[：:]\s*<span[^>]*>([^<]+)</span>', html)
    if m:
        attn = set(re.findall(r'(\d{4,6})', m.group(1)))
    return dispo, attn


def load_tags_supplement():
    """讀 twse-disposition 的 stock_info.json,回傳 {code: [中文tags]}。失敗回空。"""
    raw = None
    if os.path.exists(STOCKINFO_LOCAL):
        raw = open(STOCKINFO_LOCAL, encoding="utf-8").read()
    else:
        try:
            raw = fetch(STOCKINFO_URL)
        except Exception:
            return {}
    try:
        info = json.loads(raw)
    except Exception:
        return {}
    out = {}
    for code, v in info.items():
        if code.startswith("_") or not isinstance(v, dict):
            continue
        tags = [TAG_ZH[t] for t in (v.get("tags") or "").split() if t in TAG_ZH]
        if tags:
            out[code] = tags
    return out


def age_days(analysis_date, today):
    try:
        d = datetime.strptime(analysis_date, "%Y-%m-%d").date()
        return (today - d).days
    except ValueError:
        return None


def build_fresh_html(meta, close, price_date, today):
    parts = []
    if close is not None:
        base = meta["analysis_price"]
        pct = (close / base - 1) * 100 if base else 0
        cls = "up" if pct > 0.05 else ("down" if pct < -0.05 else "flat")
        arrow = "▲" if pct > 0.05 else ("▼" if pct < -0.05 else "―")
        dstr = f"{price_date[4:6]}/{price_date[6:8]}" if len(price_date) == 8 else price_date
        parts.append(f'最新收盤 <b>{close:g}</b>({dstr})'
                     f'<span class="{cls}"> {arrow} 較分析價 {pct:+.1f}%</span>')
    else:
        parts.append("最新收盤:查無(來源未回傳)")
    n = age_days(meta["analysis_date"], today)
    if n is not None:
        acls = "age-ok" if n <= 7 else ("age-warn" if n <= 30 else "age-old")
        label = "" if n <= 7 else ("・建議留意時效" if n <= 30 else "・分析已陳舊,建議重跑")
        parts.append(f'<span class="{acls}">資料齡 {n} 天{label}</span>')
    return "|".join(parts)


def build_dispo_badge(code, dispo, attn):
    if code in dispo:
        d = dispo[code]
        end = f"·至 {d['end']}" if d["end"] else ""
        return f'<span class="badge dispo">處置中·{d["auction"]}{end}</span>'
    if code in attn:
        return '<span class="badge attn">注意股累計中</span>'
    return ""


def build_calendar_html(cal_events, stocks, dispo, today):
    events = []
    for e in cal_events:
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        events.append((d, e.get("code", ""), e.get("event", ""), bool(e.get("approx"))))
    # 處置迄日(僅已分析個股)
    for code, d in dispo.items():
        if code in stocks and d.get("end"):
            try:
                m, dd = d["end"].split("/")
                dt = date(today.year, int(m), int(dd))
                if dt < today:  # 跨年
                    dt = date(today.year + 1, int(m), int(dd))
                events.append((dt, code, f"處置期滿(恢復正常撮合,現為{d['auction']})", False))
            except (ValueError, IndexError):
                pass
    # 只保留:未來事件 + 過去 14 天內(逾期=待驗證)
    kept = [(d, code, ev, approx) for d, code, ev, approx in events if (d - today).days >= -14]
    # 同一股票的多個事件合併成一列;整列以該股「最早的未過期事件」排序(全過期則用最近的逾期日)
    by_code = {}
    for d, code, ev, approx in kept:
        by_code.setdefault(code, []).append((d, ev, approx))

    def sort_key(code):
        ds = sorted(by_code[code])
        future = [d for d, _, _ in ds if (d - today).days >= 0]
        return (future[0] if future else ds[-1][0])

    rows = []
    for code in sorted(by_code, key=sort_key):
        name = stocks.get(code, {}).get("name", code)
        href = stocks.get(code, {}).get("file", "#")
        evs = sorted(by_code[code])
        # 每個事件一行小字:日期 + 事件;逾期紅、7 天內琥珀
        lines = []
        for d, ev, approx in evs:
            delta = (d - today).days
            ds = d.strftime("%m/%d") + ("(約)" if approx else "")
            if delta < 0:
                dtag = f'<span class="due">{ds} 已過{-delta}天,待驗證</span>'
            elif delta <= 7:
                dtag = f'<span class="soon">{ds}</span>'
            else:
                dtag = f'<span style="color:var(--muted)">{ds}</span>'
            lines.append(f'<div style="padding:2px 0"><b>{dtag}</b> {ev}</div>')
        # 整列的日期欄:顯示該股最近待辦的日期狀態(取排序鍵那筆)+ 多事件註記
        head_d = sort_key(code)
        hdelta = (head_d - today).days
        head_ds = head_d.strftime("%m/%d")
        cls = "due" if hdelta < 0 else ("soon" if hdelta <= 7 else "")
        head_ds = f'<span class="{cls}">{head_ds}</span>' if cls else head_ds
        more = f'<br><span style="color:var(--muted);font-size:11px">共 {len(evs)} 事件</span>' if len(evs) > 1 else ""
        head_html = head_ds + more
        rows.append(f'<tr><td style="white-space:nowrap;vertical-align:top">{head_html}</td>'
                    f'<td style="vertical-align:top"><a href="{href}" '
                    f'style="color:var(--blue);text-decoration:none">{name} {code}</a></td>'
                    f'<td>{"".join(lines)}</td></tr>')
    if not rows:
        rows.append('<tr><td colspan="3" style="color:var(--muted)">近期無待驗證事件</td></tr>')
    upd = today.strftime("%Y/%m/%d")
    return (f'<div class="cal"><h3>📅 催化劑日曆(自動更新 {upd})</h3>'
            f'<table><tr><th>日期</th><th>個股</th><th>事件</th></tr>{"".join(rows)}</table></div>')


def main():
    today = date.today()
    stocks = json.load(open(STOCKS_JSON, encoding="utf-8"))
    stocks.pop("_comment", None)
    cal = json.load(open(CAL_JSON, encoding="utf-8"))

    twse_date, twse = fetch_twse_closes()
    tpex_date, tpex = fetch_tpex_closes()
    dispo, attn = load_dispo()
    tag_sup = load_tags_supplement()

    s = open(INDEX, encoding="utf-8").read()
    prices = {}

    for code, meta in stocks.items():
        close = (tpex if meta["market"] == "tpex" else twse).get(code)
        pdate = tpex_date if meta["market"] == "tpex" else twse_date
        prices[code] = {"close": close, "date": pdate,
                        "analysis_price": meta["analysis_price"],
                        "analysis_date": meta["analysis_date"]}
        # fresh div
        fresh = build_fresh_html(meta, close, pdate, today)
        s = re.sub(rf'(<div class="fresh" data-code="{code}">).*?(</div>)',
                   lambda m: m.group(1) + fresh + m.group(2), s, count=1, flags=re.S)
        # dispo badge(autodispo span 是 badges 列最後一個元素,以 </span></div> 為右界確保冪等)
        badge = build_dispo_badge(code, dispo, attn)
        s = re.sub(rf'(<span class="autodispo" data-code="{code}">).*?(</span></div>)',
                   lambda m: m.group(1) + badge + m.group(2), s, count=1, flags=re.S)
        # tags supplement (merge, keep order, dedupe)
        if code in tag_sup:
            merged = list(dict.fromkeys(meta["tags"] + tag_sup[code]))
            s = re.sub(rf'(data-code="{code}" data-tags=")[^"]*(")',
                       lambda m: m.group(1) + " ".join(merged) + m.group(2), s, count=1)

    cal_html = build_calendar_html(cal.get("events", []), stocks, dispo, today)
    s = re.sub(r'<!--CALENDAR_START-->.*?<!--CALENDAR_END-->',
               "<!--CALENDAR_START-->\n" + cal_html + "\n<!--CALENDAR_END-->", s, flags=re.S)

    open(INDEX, "w", encoding="utf-8").write(s)
    json.dump({"updated": today.isoformat(), "prices": prices},
              open(PRICES_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    got = sum(1 for v in prices.values() if v["close"] is not None)
    print(f"done: prices {got}/{len(prices)} | dispo hits "
          f"{sum(1 for c in stocks if c in dispo)} | attn hits {sum(1 for c in stocks if c in attn)}")


if __name__ == "__main__":
    main()
