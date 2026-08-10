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
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

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

US_EASTERN = ZoneInfo("America/New_York")
YAHOO_CHART = "https://{host}.finance.yahoo.com/v8/finance/chart/{code}?range=5d&interval=1d"
NASDAQ_INFO = "https://api.nasdaq.com/api/quote/{code}/info?assetclass=stocks"
NASDAQ_TS = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}

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


def us_today():
    """當前美東日期。日期規則的基準:只收嚴格早於此日期的資料點。"""
    return datetime.now(US_EASTERN).date()


def parse_yahoo_chart(text, ref_date, code):
    """Yahoo chart JSON → (yyyymmdd, close);無可用資料回 None。

    只接受日期嚴格早於 ref_date(當前美東日)的日 K,避開未完成的盤中棒——
    GitHub 排程延遲 5~8 小時時,執行時點可能落在美股盤中。
    timestamp 是場次「開盤」時刻(09:30 ET),需先加 gmtoffset 才能還原美東日期。
    刻意不用 meta.regularMarketPrice:該欄位盤中會回傳盤中價。
    """
    try:
        r = json.loads(text)["chart"]["result"][0]
        ts = r["timestamp"]
        closes = r["indicators"]["quote"][0]["close"]
        off = r["meta"]["gmtoffset"]
        symbol = r["meta"]["symbol"] or ""
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    if symbol.upper() != code.upper():
        return None
    if not isinstance(ts, list) or not isinstance(closes, list) or len(ts) != len(closes):
        return None
    for t, c in zip(reversed(ts), reversed(closes)):
        if c is None:
            continue
        d = datetime.fromtimestamp(t + off, tz=timezone.utc).date()
        if d < ref_date:
            return d.strftime("%Y%m%d"), float(c)
    return None


def parse_nasdaq_info(text, ref_date, code):
    """Nasdaq quote info JSON → (yyyymmdd, close);無可用資料回 None。

    此端點失敗時回 HTTP 200 但 data 為 null(錯誤碼在 status.bCodeMessage),
    所以必須檢查 data 非 null,不能只看 HTTP 狀態碼。
    """
    try:
        d = json.loads(text).get("data")
        symbol = (d or {}).get("symbol") or ""
        pdata = (d or {}).get("primaryData") or {}
        raw = (pdata.get("lastSalePrice") or "").replace("$", "").replace(",", "").strip()
        stamp = pdata.get("lastTradeTimestamp") or ""
        close = float(raw)
    except (ValueError, AttributeError, TypeError):
        return None
    if symbol.upper() != code.upper():
        return None
    m = NASDAQ_TS.search(stamp)
    if not m or m.group(1) not in MONTHS:
        return None
    try:
        dt = date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
    except ValueError:
        return None
    if dt >= ref_date:
        return None
    return dt.strftime("%Y%m%d"), close


def fetch_us_closes(codes):
    """美股收盤價 {code: (yyyymmdd, close)}。個股三段備援全失敗即不出現在結果中。

    順序:Yahoo query1 → Yahoo query2 → Nasdaq。
    query1/query2 是不同主機,分別重試對 429 速率限制有實質幫助;
    Nasdaq 排第三是因為覆蓋率有缺口(實測 SNDK 持續回 code 3004)。
    """
    ref = us_today()
    out = {}
    for code in codes:
        for url, parser in (
            (YAHOO_CHART.format(host="query1", code=code), parse_yahoo_chart),
            (YAHOO_CHART.format(host="query2", code=code), parse_yahoo_chart),
            (NASDAQ_INFO.format(code=code), parse_nasdaq_info),
        ):
            host = url.split("/")[2]
            try:
                got = parser(fetch(url, retries=2), ref, code)
            except Exception as e:
                print(f"WARN us {code} via {host}: {e}", file=sys.stderr)
                continue
            if got:
                out[code] = got
                break
            print(f"WARN us {code} via {host}: no usable bar", file=sys.stderr)
        else:
            print(f"WARN us {code}: all sources failed", file=sys.stderr)
    return out


def us_errors(us_codes, us):
    """已登記美股 vs 實際抓到的美股 → errors token 清單。

    全滅(已登記至少一檔,但一檔都沒抓到)回傳 us_all_failed;部分缺漏回傳
    us_missing:CODE1,CODE2。兩者字串不同純粹是為了讓 log 一眼區分「整批來源
    掛掉」與「單一個股缺漏」,workflow 本身只判斷這份清單是否非空,不比對字串內容。
    """
    missing = [c for c in us_codes if c not in us]
    if missing and not us:
        return ["us_all_failed"]
    if missing:
        return [f"us_missing:{','.join(missing)}"]
    return []


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
    """
    回傳 (處置 {code:{auction,end}}, 注意 set(codes), 名單資料日期 str)。
    本機優先,線上備援,都失敗回空。資料日期供個股頁 sub 行標註對照基準,
    沒抓到就回空字串讓下游寫「日期查無」,不要猜。
    """
    html = ""
    if os.path.exists(DISPO_LOCAL):
        html = open(DISPO_LOCAL, encoding="utf-8").read()
    else:
        try:
            html = fetch(DISPO_URL)
        except Exception as e:
            print(f"WARN dispo fetch failed: {e}", file=sys.stderr)
            return {}, set(), ""
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
    md = re.search(r'自動更新\s*([\d/]+)', html)
    return dispo, attn, (md.group(1) if md else "")


def build_dispo_note(code, dispo, attn, dispo_date):
    """
    個股頁 sub 行的處置狀態句(純文字,無標籤)。
    先前各儀表板把這句寫死、日期各不相同且會過期,改由本函式每日重寫。
    """
    ref = f"(對照處置股儀表板 {dispo_date} 資料)" if dispo_date else "(處置股名單日期查無)"
    if code in dispo:
        d = dispo[code]
        end = f",至 {d['end']}" if d["end"] else ""
        return f"處置中:{d['auction']}{end}{ref}"
    if code in attn:
        return f"注意股累計中{ref}"
    return f"未列入處置股/注意股名單{ref}"


def fill_dashboard_dispo(stocks, dispo, attn, dispo_date):
    """
    把處置徽章與狀態句同步到各個股頁。
    h1 的 autodispo 以 `</span></h1>` 為右界、sub 的 dispostat 內容限純文字,
    兩者都能重複執行而不累加(與 index.html 的 autodispo 同一套作法)。
    """
    filled, missing = 0, []
    for code, meta in stocks.items():
        if meta.get("market") == "us":     # 美股無處置/注意股制度
            continue
        path = os.path.join(BASE, meta["file"])
        if not os.path.exists(path):
            missing.append(meta["file"])
            continue
        s = open(path, encoding="utf-8").read()
        orig = s
        badge = build_dispo_badge(code, dispo, attn, meta["market"])
        s, n1 = re.subn(
            rf'(<span class="autodispo" data-code="{re.escape(code)}">).*?(</span></h1>)',
            lambda m: m.group(1) + badge + m.group(2), s, count=1, flags=re.S)
        note = build_dispo_note(code, dispo, attn, dispo_date)
        s, n2 = re.subn(
            rf'(<span class="dispostat" data-code="{re.escape(code)}">)[^<]*(</span>)',
            lambda m: m.group(1) + note + m.group(2), s, count=1)
        if not (n1 and n2):
            missing.append(f"{meta['file']}(缺佔位符 h1={n1} sub={n2})")
            continue
        if s != orig:
            open(path, "w", encoding="utf-8").write(s)
        filled += 1
    return filled, missing


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
        prefix = "$" if meta.get("market") == "us" else ""
        parts.append(f'最新收盤 <b>{prefix}{close:g}</b>({dstr})'
                     f'<span class="{cls}"> {arrow} 較分析價 {pct:+.1f}%</span>')
    else:
        parts.append("最新收盤:查無(來源未回傳)")
    n = age_days(meta["analysis_date"], today)
    if n is not None:
        acls = "age-ok" if n <= 7 else ("age-warn" if n <= 30 else "age-old")
        label = "" if n <= 7 else ("・建議留意時效" if n <= 30 else "・分析已陳舊,建議重跑")
        parts.append(f'<span class="{acls}">資料齡 {n} 天{label}</span>')
    return "|".join(parts)


RATING_LABEL = {"buy": ("偏多", "green"), "hold": ("中性", "amber"), "avoid": ("觀望", "red")}


def build_verdict_html(meta, close):
    """
    結論卡的動態列:用「最新收盤」而非分析價重算距目標價空間,避免數字隨股價漂移而過期。
    評等徽章沿用 stocks.json 的 rating(分析當下的判斷),不隨股價自動翻面——
    評等是研究結論,只有重跑分析才該改;會變的只有「還剩多少空間」。
    """
    tgt = meta.get("target")
    if tgt is None:
        return "目標價未登記"
    prefix = "$" if meta.get("market") == "us" else ""
    ref = close if close is not None else meta["analysis_price"]
    tag = "現價" if close is not None else "分析價"
    up = (tgt / ref - 1) * 100
    cls = "up" if up > 0 else ("down" if up < 0 else "flat")
    return (f'{tag} <b>{prefix}{ref:g}</b> → 目標價 <b>{prefix}{tgt:g}</b>'
            f'<span class="{cls}"> ({up:+.1f}%)</span>')


def fill_dashboard_verdict(stocks, prices):
    """把結論卡的動態列同步到各個股頁(與 index.html 用同一個 .verdict 佔位符)。"""
    filled, missing = 0, []
    for code, meta in stocks.items():
        path = os.path.join(BASE, meta["file"])
        if not os.path.exists(path):
            continue
        s = open(path, encoding="utf-8").read()
        orig = s
        html = build_verdict_html(meta, (prices.get(code) or {}).get("close"))
        s, n = re.subn(rf'(<div class="verdict" data-code="{re.escape(code)}">).*?(</div>)',
                       lambda m: m.group(1) + html + m.group(2), s, count=1, flags=re.S)
        if not n:
            missing.append(meta["file"])
            continue
        if s != orig:
            open(path, "w", encoding="utf-8").write(s)
        filled += 1
    return filled, missing


def build_dispo_badge(code, dispo, attn, market):
    # 美股無處置/注意股制度,徽章恆空。保留空標籤讓 SKILL.md 只需一套卡片規則,
    # 且日後若要加美股專屬徽章有現成掛點。
    if market == "us":
        return ""
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
        mk = (' <span style="color:var(--muted);font-size:11px">US</span>'
              if stocks.get(code, {}).get("market") == "us" else "")
        rows.append(f'<tr><td style="white-space:nowrap;vertical-align:top">{head_html}</td>'
                    f'<td style="vertical-align:top"><a href="{href}" '
                    f'style="color:var(--blue);text-decoration:none">{name} {code}</a>{mk}</td>'
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
    bad = {c: m.get("market") for c, m in stocks.items() if m.get("market") not in ("twse", "tpex", "us")}
    if bad:
        sys.exit(f"unknown market values: {bad}")
    cal = json.load(open(CAL_JSON, encoding="utf-8"))

    twse_date, twse = fetch_twse_closes()
    tpex_date, tpex = fetch_tpex_closes()
    us_codes = [c for c, m in stocks.items() if m["market"] == "us"]
    us = fetch_us_closes(us_codes) if us_codes else {}
    dispo, attn, dispo_date = load_dispo()
    tag_sup = load_tags_supplement()

    s = open(INDEX, encoding="utf-8").read()
    prices = {}

    for code, meta in stocks.items():
        if meta["market"] == "us":
            got = us.get(code)
            close, pdate = (got[1], got[0]) if got else (None, "")
        else:
            close = (tpex if meta["market"] == "tpex" else twse).get(code)
            pdate = tpex_date if meta["market"] == "tpex" else twse_date
        prices[code] = {"close": close, "date": pdate,
                        "analysis_price": meta["analysis_price"],
                        "analysis_date": meta["analysis_date"]}
        # fresh div
        fresh = build_fresh_html(meta, close, pdate, today)
        s = re.sub(rf'(<div class="fresh" data-code="{re.escape(code)}">).*?(</div>)',
                   lambda m: m.group(1) + fresh + m.group(2), s, count=1, flags=re.S)
        # verdict 動態列(index 卡片版)
        vd = build_verdict_html(meta, close)
        s = re.sub(rf'(<div class="verdict" data-code="{re.escape(code)}">).*?(</div>)',
                   lambda m: m.group(1) + vd + m.group(2), s, count=1, flags=re.S)
        # dispo badge(autodispo span 是 badges 列最後一個元素,以 </span></div> 為右界確保冪等)
        badge = build_dispo_badge(code, dispo, attn, meta["market"])
        s = re.sub(rf'(<span class="autodispo" data-code="{re.escape(code)}">).*?(</span></div>)',
                   lambda m: m.group(1) + badge + m.group(2), s, count=1, flags=re.S)
        # tags = stocks.json + 處置股庫補充(merge, keep order, dedupe)。
        # 無論該股有無補充標籤都重寫,確保 stocks.json 是唯一真相來源
        # (先前只在有補充時才寫,導致 json 加了標籤卻沒同步到卡片)。
        merged = list(dict.fromkeys(meta["tags"] + tag_sup.get(code, [])))
        s = re.sub(rf'(data-code="{re.escape(code)}" data-tags=")[^"]*(")',
                   lambda m: m.group(1) + " ".join(merged) + m.group(2), s, count=1)
        # data-concl 由 rating 驅動,避免篩選條件與結論卡評等各說各話
        s = re.sub(rf'(data-code="{re.escape(code)}" data-tags="[^"]*" data-concl=")[^"]*(")',
                   lambda m: m.group(1) + meta["rating"] + m.group(2), s, count=1)

    cal_html = build_calendar_html(cal.get("events", []), stocks, dispo, today)
    s = re.sub(r'<!--CALENDAR_START-->.*?<!--CALENDAR_END-->',
               "<!--CALENDAR_START-->\n" + cal_html + "\n<!--CALENDAR_END-->", s, flags=re.S)

    open(INDEX, "w", encoding="utf-8").write(s)

    dash_filled, dash_missing = fill_dashboard_dispo(stocks, dispo, attn, dispo_date)
    vd_filled, vd_missing = fill_dashboard_verdict(stocks, prices)
    # 任何已登記美股缺漏(全滅或部分)都必須讓 workflow 轉紅,見 us_errors()。
    # 刻意不在此 sys.exit(1):那會讓 workflow 跳過 commit 步驟,連當天台股更新一起丟掉。
    # 由 workflow 在 push 之後讀這個欄位決定成敗。
    errors = us_errors(us_codes, us)
    json.dump({"updated": today.isoformat(), "errors": errors, "prices": prices},
              open(PRICES_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    got = sum(1 for v in prices.values() if v["close"] is not None)
    print(f"done: prices {got}/{len(prices)} | us {len(us)}/{len(us_codes)} | dispo hits "
          f"{sum(1 for c in stocks if c in dispo)} | attn hits {sum(1 for c in stocks if c in attn)}"
          f" | 個股頁徽章 {dash_filled} 份(名單日期 {dispo_date or '查無'})"
          f" | 結論卡 {vd_filled}/{len(stocks)} 份"
          + (f" | 結論卡缺佔位 {vd_missing}" if vd_missing else "")
          + (f" | 個股頁未處理 {dash_missing}" if dash_missing else "")
          + (f" | ERRORS {errors}" if errors else ""))


if __name__ == "__main__":
    main()
