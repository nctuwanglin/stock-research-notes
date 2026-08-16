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
import ssl
import subprocess
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


def _fetch_via_curl(url, timeout):
    """回退路徑,僅在 urllib 的 SSL 驗證失敗時使用。curl 走 macOS 系統信任庫,
    仍會驗證憑證鏈(不是關閉驗證),只是能接受 Python/OpenSSL 較嚴格判定為
    「缺 Subject Key Identifier」而拒絕、但瀏覽器與 curl 均接受的憑證。"""
    r = subprocess.run(
        ["curl", "-sS", "-A", "Mozilla/5.0 (research-notes-updater)",
         "--max-time", str(timeout), url],
        capture_output=True, timeout=timeout + 5)
    if r.returncode != 0:
        raise RuntimeError(f"curl exit {r.returncode}: {r.stderr.decode(errors='replace')[:200]}")
    return r.stdout.decode("utf-8", errors="replace")


def fetch(url, timeout=30, retries=3):
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={
                "User-Agent": "Mozilla/5.0 (research-notes-updater)",
                "Accept": "text/csv,application/json,*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.URLError as e:
            last = e
            if isinstance(e.reason, ssl.SSLCertVerificationError):
                # 已知個案:某些 TWSE CDN 端點的憑證鏈缺 Subject Key Identifier,
                # OpenSSL 嚴格拒絕但系統信任庫(curl 走的路徑)接受,直接改走 curl
                # 而非放寬 Python 的驗證設定,以免真的中間人攻擊也被靜默放行。
                try:
                    return _fetch_via_curl(url, timeout)
                except Exception as ce:
                    last = ce
            import time
            time.sleep(2 * (i + 1))
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


def _cal_routine_type(ev_text):
    """常態(全體適用)事件分類。個股專屬催化劑回傳 None。

    月營收/法說會是全台上市櫃普遍的制度性揭露,同一天常有數十檔同時觸發,
    逐檔列出只會灌爆版面且無差異化資訊,故聚合成單列。
    """
    if "營收" in ev_text:
        return "月營收公告"
    if "法說" in ev_text or "財報" in ev_text:
        return "法說會/財報"
    return None


def _cal_datetag(d, today, approx):
    """日期標籤:逾期紅、7 天內琥珀、其餘灰。"""
    delta = (d - today).days
    ds = d.strftime("%m/%d") + ("(約)" if approx else "")
    if delta < 0:
        return f'<span class="due">{ds}</span>'
    if delta <= 7:
        return f'<span class="soon">{ds}</span>'
    return f'<span style="color:var(--muted)">{ds}</span>'


def build_calendar_html(cal_events, stocks, dispo, today):
    events = []
    for e in cal_events:
        try:
            d = datetime.strptime(e["date"], "%Y-%m-%d").date()
        except ValueError:
            continue
        events.append((d, e.get("code", ""), e.get("event", ""), bool(e.get("approx"))))
    # 處置迄日(僅已分析個股)
    # 已在 calendar.json 手動登記處置事件的 (代號, 日期) 不重複產生
    manual_dispo = {(d, code) for d, code, ev, _ in events if "處置" in ev}
    for code, dd in dispo.items():
        if code in stocks and dd.get("end"):
            try:
                m, day = dd["end"].split("/")
                dt = date(today.year, int(m), int(day))
                # 僅在明顯跨年時(相差逾半年)才推到明年;
                # 近期已期滿的處置就是結束了,不可誤推成一年後
                if (today - dt).days > 180:
                    dt = date(today.year + 1, int(m), int(day))
                if dt < today:      # 已期滿 → 不再列入
                    continue
                if (dt, code) in manual_dispo:
                    continue
                events.append((dt, code, f"處置期滿(恢復正常撮合,現為{dd['auction']})", False))
            except (ValueError, IndexError):
                pass
    # 只保留:未來事件 + 過去 14 天內(逾期=待驗證)
    kept = [x for x in events if (x[0] - today).days >= -14]
    upd = today.strftime("%Y/%m/%d")
    if not kept:
        return (f'<div class="cal"><h3>📅 催化劑日曆(自動更新 {upd})</h3>'
                f'<div style="color:var(--muted);font-size:12.5px">近期無待驗證事件</div></div>')

    # ── 事件分層 ────────────────────────────────────────────────
    # 同日同類型且 >=3 檔的常態事件 → 聚合成一列(可展開);其餘照個股列出
    routine, solo = {}, []
    for d, code, ev, approx in kept:
        t = _cal_routine_type(ev)
        if t:
            routine.setdefault((d, t), []).append((code, ev, approx))
        else:
            solo.append((d, code, ev, approx))
    aggs = []
    for (d, t), items in routine.items():
        if len(items) >= 3:
            aggs.append((d, t, items))
        else:
            for code, ev, approx in items:
                solo.append((d, code, ev, approx))

    # ── 每日事件計數(供月曆格子畫圓點)────────────────────────
    daymap = {}
    for d, code, ev, approx in solo:
        e = daymap.setdefault(d, {"solo": 0, "agg": 0, "due": False})
        e["solo"] += 1
        if (d - today).days < 0:
            e["due"] = True
    for d, t, items in aggs:
        e = daymap.setdefault(d, {"solo": 0, "agg": 0, "due": False})
        e["agg"] += 1
        if (d - today).days < 0:
            e["due"] = True

    def stock_link(code):
        name = stocks.get(code, {}).get("name", code)
        href = stocks.get(code, {}).get("file", "")
        mk = " US" if stocks.get(code, {}).get("market") == "us" else ""
        if not href:
            return f"{name} {code}{mk}"
        return f'<a href="{href}">{name} {code}{mk}</a>'

    # ── 逐月產生:月曆網格 + 該月事件清單 ──────────────────────
    months = sorted({(d.year, d.month) for d in daymap})
    cur = (today.year, today.month)
    if cur not in months:
        months.append(cur)
        months.sort()
    dows = ["日", "一", "二", "三", "四", "五", "六"]
    panels = []
    for y, m in months:
        first = date(y, m, 1)
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        ndays = (nxt - first).days
        lead = (first.weekday() + 1) % 7  # 週日起算
        cells = ['<div class="dow">%s</div>' % w for w in dows]
        cells += ['<div class="calcell pad"></div>'] * lead
        for dd in range(1, ndays + 1):
            d = date(y, m, dd)
            info = daymap.get(d)
            klass = "calcell"
            dots = ""
            if info:
                klass += " has"
                bits = ""
                if info["solo"]:
                    bits += '<i class="%s"></i>' % ("p" if info["due"] else "k")
                if info["agg"]:
                    bits += '<i class="r"></i>'
                dots = f'<span class="caldots">{bits}</span>'
            if d == today:
                klass += " today"
            attr = f' data-d="{d.isoformat()}"' if info else ""
            cells.append(f'<div class="{klass}"{attr}><span>{dd}</span>{dots}</div>')
        grid = f'<div class="calgrid">{"".join(cells)}</div>'

        rows = []
        month_items = [(d, "solo", code, ev, approx) for d, code, ev, approx in solo
                       if (d.year, d.month) == (y, m)]
        month_items += [(d, "agg", t, items, None) for d, t, items in aggs
                        if (d.year, d.month) == (y, m)]
        for it in sorted(month_items, key=lambda x: (x[0], x[1])):
            d = it[0]
            if it[1] == "solo":
                _, _, code, ev, approx = it
                overdue = ((d - today).days < 0)
                tail = f' <span class="due">已過{(today - d).days}天,待驗證</span>' if overdue else ""
                rows.append(
                    f'<div class="calrow" data-d="{d.isoformat()}">'
                    f'<span class="caldate">{_cal_datetag(d, today, approx)}</span>'
                    f'<span class="calbody">{stock_link(code)} — {ev}{tail}</span></div>')
            else:
                _, _, t, items, _ = it
                approx_any = any(a for _, _, a in items)
                tags = "".join(stock_link(c) for c, _, _ in
                               sorted(items, key=lambda x: x[0]))
                rows.append(
                    f'<details class="calrow calagg" data-d="{d.isoformat()}">'
                    f'<summary><span class="caldate">{_cal_datetag(d, today, approx_any)}</span>'
                    f'<span class="calbody"><span class="calcaret">▸</span> {t}'
                    f' · <b>{len(items)}</b> 檔追蹤中</span></summary>'
                    f'<div class="caltags">{tags}</div></details>')
        empty = '<div style="color:var(--muted);font-size:12.5px">本月無事件</div>'
        listing = "".join(rows) or empty
        hidden = "" if (y, m) == cur else " hidden"
        panels.append(
            f'<div class="calpanel" data-m="{y}-{m:02d}"{hidden}>{grid}'
            f'<div class="callist">{listing}</div></div>')

    n_solo, n_agg_stocks = len(solo), sum(len(i) for _, _, i in aggs)
    js = (
        "(function(){var c=document.getElementById('calwrap');if(!c)return;"
        "var ps=c.querySelectorAll('.calpanel'),i=0;"
        "ps.forEach(function(p,k){if(!p.hasAttribute('hidden'))i=k;});"
        "function show(k){i=Math.max(0,Math.min(ps.length-1,k));"
        "ps.forEach(function(p,j){p.hidden=(j!==i);});"
        "var m=ps[i].dataset.m.split('-');"
        "c.querySelector('.calmon').textContent=m[0]+'年'+parseInt(m[1],10)+'月';"
        "c.querySelector('[data-nav=prev]').disabled=(i===0);"
        "c.querySelector('[data-nav=next]').disabled=(i===ps.length-1);}"
        "c.querySelector('[data-nav=prev]').onclick=function(){show(i-1);};"
        "c.querySelector('[data-nav=next]').onclick=function(){show(i+1);};"
        "var tb=c.querySelector('[data-nav=today]');if(tb)tb.onclick=function(){"
        "ps.forEach(function(p,j){if(p.dataset.m==='" + f"{cur[0]}-{cur[1]:02d}" + "')show(j);});};"
        "c.addEventListener('click',function(e){var cell=e.target.closest('.calcell.has');"
        "if(!cell)return;var d=cell.dataset.d;"
        "c.querySelectorAll('.calcell.sel,.calrow.sel').forEach(function(x){x.classList.remove('sel');});"
        "cell.classList.add('sel');"
        "var first=null;c.querySelectorAll('.calrow[data-d=\"'+d+'\"]').forEach(function(r){"
        "r.classList.add('sel');if(!first)first=r;});"
        "if(first){if(first.tagName==='DETAILS')first.open=true;"
        "first.scrollIntoView({block:'nearest',behavior:'smooth'});}});"
        "show(i);})();")

    legend = ('<span class="callegend">'
              '<span><i style="background:var(--amber)"></i>個股催化劑</span>'
              '<span><i style="background:var(--muted);opacity:.75"></i>常態(月營收/法說)</span>'
              '</span>')
    head = (f'<div class="calhead">'
            f'<button class="calnav" data-nav="prev">‹</button>'
            f'<span class="calmon">{cur[0]}年{cur[1]}月</span>'
            f'<button class="calnav" data-nav="next">›</button>'
            f'<button class="calnav" data-nav="today">今日</button>{legend}</div>')
    foot = (f'<div class="calmore">共 {n_solo} 筆個股專屬催化劑;'
            f'月營收/法說會等常態事件已聚合為 {len(aggs)} 列(涵蓋 {n_agg_stocks} 檔次),點擊展開。'
            f'點月曆日期可跳至當日事件。</div>')
    return (f'<div class="cal" id="calwrap"><h3>📅 催化劑日曆(自動更新 {upd})</h3>'
            f'{head}{"".join(panels)}{foot}</div><script>{js}</script>')


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
