# 美股每日自動更新與催化劑日曆 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 讓 `stock-research-notes` 站上的美股個股(MU、SNDK)與台股一樣,每個交易日自動更新現價偏移/資料齡,並出現在催化劑日曆。

**Architecture:** 在既有的 `scripts/update_freshness.py` 內新增美股報價來源(Yahoo chart `query1` → `query2` → Nasdaq 三段備援鏈)與 `market == "us"` 分派,不新增腳本、不新增外部套件。`.fresh` / `.autodispo` / 催化劑日曆的組裝邏輯完全與台股共用。核心防呆是「只接受日期嚴格早於當前美東日期的資料點」,避免 GitHub 排程延遲落到美股盤中時把盤中價當成收盤價。

**Tech Stack:** Python 3.11 標準庫(`urllib`、`json`、`re`、`csv`、`zoneinfo`)、`unittest`、GitHub Actions。**不得引入任何第三方套件**——現有腳本刻意零依賴。

**設計文件:** `docs/superpowers/specs/2026-07-28-us-stock-automation-design.md`

## Global Constraints

- 工作目錄一律為 `~/stock-research-notes`(repo `nctuwanglin/stock-research-notes`)。**此 repo 路徑不得移動**,GitHub Pages、績效儀表板交叉引用、自動化都指向它。
- 零第三方依賴,只用 Python 3.11 標準庫。
- 測試框架用 `unittest`(非 pytest),沿用 `~/twse-disposition/tests/test_parsers.py` 的既有慣例:`sys.path.insert(0, Path(__file__).parent.parent / "scripts")` 後直接 import。執行指令 `python3 -m unittest discover -s tests -q`。
- HTTP User-Agent 一律沿用 `fetch()` 現有的 `Mozilla/5.0 (research-notes-updater)`。**不得改用瀏覽器樣 UA**——2026-07-28 實測帶 Chrome UA 呼叫 Yahoo chart 直接回 `Too Many Requests`,換回原 UA 立即正常。
- 所有測試使用存檔 fixture,**不得在測試中發出網路請求**。
- 站內顏色慣例:**綠漲紅跌**(與台股站一致,勿用美股慣例)。
- `scripts/update_freshness.py` 只能改動 `index.html` 的標記區與佔位元素(`.fresh`、`.autodispo`、`data-tags`、`<!--CALENDAR_START-->…<!--CALENDAR_END-->`),**不得動任何分析本文**。
- 每個 Task 結束都要 commit(**Task 6 除外**——它改的 SKILL.md 位於 repo 之外的 Claude skills 目錄,無法納入版本控制,改以 `grep` 驗證取代 commit)。commit message 用英文,結尾加:
  ```
  Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
  ```

## 現況參考(實作前必讀)

`scripts/update_freshness.py` 目前 307 行,關鍵位置:

| 行號 | 內容 |
|---|---|
| 12-19 | imports(`from datetime import date, datetime`) |
| 27-33 | 資料源常數 |
| 41-54 | `fetch(url, timeout=30, retries=3)` |
| 57-77 | `fetch_twse_closes()` → `(resp_date, {code: close})` |
| 80-101 | `fetch_tpex_closes()` → `(resp_date, {code: close})` |
| 162-179 | `build_fresh_html(meta, close, price_date, today)` |
| 182-189 | `build_dispo_badge(code, dispo, attn)` |
| 192-256 | `build_calendar_html(cal_events, stocks, dispo, today)` |
| 259-306 | `main()` |

`index.html` 第 105-115 行是 MU 與 SNDK 兩張卡片,已帶 `data-market="us"`(供既有的市場篩選 chips 使用),但**沒有** `.fresh` 與 `.autodispo` 佔位。

---

### Task 1: 美股報價解析函式(純函式 + 單元測試)

建立不碰網路的解析層與日期規則。這是整個功能的正確性核心,先用 TDD 鎖死。

**Files:**
- Create: `tests/__init__.py`(空檔)
- Create: `tests/fixtures/yahoo_mu.json`
- Create: `tests/fixtures/yahoo_null_close.json`
- Create: `tests/fixtures/yahoo_ratelimited.txt`
- Create: `tests/fixtures/nasdaq_mu.json`
- Create: `tests/fixtures/nasdaq_comma.json`
- Create: `tests/fixtures/nasdaq_error.json`
- Create: `tests/test_us_quotes.py`
- Modify: `scripts/update_freshness.py`(imports 區 12-19、常數區 27-33,並在 `fetch()` 之後新增三個函式)

**Interfaces:**
- Consumes: 無(本 plan 的第一個 Task)
- Produces:
  - `us_today() -> datetime.date` — 當前美東日期
  - `parse_yahoo_chart(text: str, ref_date: datetime.date) -> tuple[str, float] | None` — 回 `(yyyymmdd, close)`
  - `parse_nasdaq_info(text: str, ref_date: datetime.date) -> tuple[str, float] | None` — 回 `(yyyymmdd, close)`
  - 常數 `YAHOO_CHART`(含 `{host}` 與 `{code}` 兩個 format 欄位)、`NASDAQ_INFO`(含 `{code}`)

- [ ] **Step 1: 建立 fixture 檔**

`tests/__init__.py` 建立為空檔案。

`tests/fixtures/yahoo_mu.json`(取自 2026-07-28 真實回應,裁剪保留必要欄位。最後一根 `1785159000` 是美東 2026-07-27 進行中的盤中棒,close 870.19 為盤中價):

```json
{"chart":{"result":[{"meta":{"currency":"USD","symbol":"MU","exchangeTimezoneName":"America/New_York","gmtoffset":-14400},"timestamp":[1784813400,1784899800,1785159000],"indicators":{"quote":[{"close":[990.2100219726562,920.9500122070312,870.1900024414062]}]}}],"error":null}}
```

`tests/fixtures/yahoo_null_close.json`(中間那根 `close` 為 `null`,模擬來源缺值):

```json
{"chart":{"result":[{"meta":{"currency":"USD","symbol":"MU","exchangeTimezoneName":"America/New_York","gmtoffset":-14400},"timestamp":[1784813400,1784899800,1785159000],"indicators":{"quote":[{"close":[990.2100219726562,null,870.1900024414062]}]}}],"error":null}}
```

`tests/fixtures/yahoo_ratelimited.txt`(Yahoo 被限流時的真實回應,**不是 JSON**,單行純文字):

```
Too Many Requests
```

`tests/fixtures/nasdaq_mu.json`(取自真實回應,時間戳為美東 2026-07-27 盤中):

```json
{"data":{"symbol":"MU","primaryData":{"lastSalePrice":"$874.19","lastTradeTimestamp":"Jul 27, 2026 12:51 PM ET"}},"message":null,"status":{"rCode":200,"bCodeMessage":null}}
```

`tests/fixtures/nasdaq_comma.json`(千分位逗號 + 已收盤的前一交易日):

```json
{"data":{"symbol":"SNDK","primaryData":{"lastSalePrice":"$1,270.50","lastTradeTimestamp":"Jul 24, 2026 4:00 PM ET"}},"message":null,"status":{"rCode":200,"bCodeMessage":null}}
```

`tests/fixtures/nasdaq_error.json`(取自 SNDK 真實回應。**HTTP 狀態是 200,但 `data` 為 `null`**):

```json
{"data":null,"message":null,"status":{"rCode":200,"bCodeMessage":[{"code":3004,"errorMessage":"Error while calling vendor"}]}}
```

- [ ] **Step 2: 寫失敗測試**

建立 `tests/test_us_quotes.py`:

```python
# -*- coding: utf-8 -*-
"""
美股報價解析固定測資。
Yahoo/Nasdaq 改格式或改風控時這裡會先紅,避免靜默解析失敗
(歷史教訓:twse-disposition 曾解析 0 筆照樣回報 success)。
執行:python3 -m unittest discover -s tests -q
"""
import sys
import unittest
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from update_freshness import (          # noqa: E402
    parse_yahoo_chart, parse_nasdaq_info,
)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


class TestYahooChart(unittest.TestCase):
    def test_takes_last_completed_bar(self):
        """最後一根是當日進行中的盤中棒,必須被日期規則排除,取前一根收盤。"""
        got = parse_yahoo_chart(load("yahoo_mu.json"), date(2026, 7, 27))
        self.assertEqual(got, ("20260724", 920.9500122070312))

    def test_skips_null_close(self):
        """close 為 null 的日 K 要跳過,繼續往前找。"""
        got = parse_yahoo_chart(load("yahoo_null_close.json"), date(2026, 7, 27))
        self.assertEqual(got, ("20260723", 990.2100219726562))

    def test_accepts_yesterday_when_ref_moves_on(self):
        """美東日期前進到 7/28 後,7/27 那根就成為可用的完整收盤。"""
        got = parse_yahoo_chart(load("yahoo_mu.json"), date(2026, 7, 28))
        self.assertEqual(got, ("20260727", 870.1900024414062))

    def test_ratelimited_body_returns_none(self):
        """被 429 時回的是純文字而非 JSON,必須回 None 而不是拋例外。"""
        self.assertIsNone(
            parse_yahoo_chart(load("yahoo_ratelimited.txt"), date(2026, 7, 27)))

    def test_all_bars_too_recent_returns_none(self):
        got = parse_yahoo_chart(load("yahoo_mu.json"), date(2026, 7, 23))
        self.assertIsNone(got)


class TestNasdaqInfo(unittest.TestCase):
    def test_rejects_same_day_intraday(self):
        """lastTradeTimestamp 為當前美東日期 = 盤中價,必須拒收。"""
        self.assertIsNone(
            parse_nasdaq_info(load("nasdaq_mu.json"), date(2026, 7, 27)))

    def test_accepts_completed_session(self):
        got = parse_nasdaq_info(load("nasdaq_mu.json"), date(2026, 7, 28))
        self.assertEqual(got, ("20260727", 874.19))

    def test_strips_dollar_and_comma(self):
        got = parse_nasdaq_info(load("nasdaq_comma.json"), date(2026, 7, 27))
        self.assertEqual(got, ("20260724", 1270.5))

    def test_null_data_returns_none(self):
        """HTTP 200 但 data 為 null(code 3004),必須回 None 而不是拋例外。"""
        self.assertIsNone(
            parse_nasdaq_info(load("nasdaq_error.json"), date(2026, 7, 28)))


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: 執行測試確認失敗**

```bash
cd ~/stock-research-notes && python3 -m unittest discover -s tests -q
```

Expected: `ImportError: cannot import name 'parse_yahoo_chart' from 'update_freshness'`

- [ ] **Step 4: 實作解析函式**

修改 `scripts/update_freshness.py`。

第 12-19 行的 import 區塊,把 `from datetime import date, datetime` 改為:

```python
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo
```

第 27-33 行的常數區塊末尾(`STOCKINFO_URL` 那行之後)新增:

```python
US_EASTERN = ZoneInfo("America/New_York")
YAHOO_CHART = "https://{host}.finance.yahoo.com/v8/finance/chart/{code}?range=5d&interval=1d"
NASDAQ_INFO = "https://api.nasdaq.com/api/quote/{code}/info?assetclass=stocks"
NASDAQ_TS = re.compile(r"([A-Z][a-z]{2})\s+(\d{1,2}),\s*(\d{4})")
MONTHS = {m: i + 1 for i, m in enumerate(
    ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
     "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"])}
```

在 `fetch()` 函式(第 54 行結束)之後、`fetch_twse_closes()` 之前,新增:

```python
def us_today():
    """當前美東日期。日期規則的基準:只收嚴格早於此日期的資料點。"""
    return datetime.now(US_EASTERN).date()


def parse_yahoo_chart(text, ref_date):
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
    except (ValueError, KeyError, IndexError, TypeError):
        return None
    for t, c in zip(reversed(ts), reversed(closes)):
        if c is None:
            continue
        d = datetime.fromtimestamp(t + off, tz=timezone.utc).date()
        if d < ref_date:
            return d.strftime("%Y%m%d"), float(c)
    return None


def parse_nasdaq_info(text, ref_date):
    """Nasdaq quote info JSON → (yyyymmdd, close);無可用資料回 None。

    此端點失敗時回 HTTP 200 但 data 為 null(錯誤碼在 status.bCodeMessage),
    所以必須檢查 data 非 null,不能只看 HTTP 狀態碼。
    """
    try:
        d = json.loads(text).get("data")
        pdata = (d or {}).get("primaryData") or {}
        raw = (pdata.get("lastSalePrice") or "").replace("$", "").replace(",", "").strip()
        stamp = pdata.get("lastTradeTimestamp") or ""
        close = float(raw)
    except (ValueError, AttributeError, TypeError):
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
```

- [ ] **Step 5: 執行測試確認通過**

```bash
cd ~/stock-research-notes && python3 -m unittest discover -s tests -q
```

Expected: `Ran 9 tests` + `OK`

- [ ] **Step 6: Commit**

```bash
cd ~/stock-research-notes
git add tests scripts/update_freshness.py
git commit -m "$(cat <<'EOF'
Add US quote parsers with intraday-bar guard

Yahoo chart and Nasdaq quote responses are parsed into (date, close).
Only bars strictly older than the current US Eastern date are accepted,
so a delayed scheduled run landing mid-session cannot record an
intraday price as a close.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 2: 美股抓取協調與 main() 整合

把解析層接上網路與 `main()`,讓 `.fresh` 真的拿到美股收盤價,並產出 §4.3 第二層告警所需的 `errors` 欄位。

**Files:**
- Modify: `scripts/update_freshness.py`(新增 `fetch_us_closes()`;改 `main()` 259-306 行)

**Interfaces:**
- Consumes: `us_today()`、`parse_yahoo_chart()`、`parse_nasdaq_info()`、常數 `YAHOO_CHART` / `NASDAQ_INFO`(Task 1)
- Produces:
  - `fetch_us_closes(codes: list[str]) -> dict[str, tuple[str, float]]` — 全鏈失敗的 code 不出現在回傳 dict
  - `data/prices.json` 頂層新增 `errors: list[str]` 欄位(正常為 `[]`)

- [ ] **Step 1: 實作 `fetch_us_closes()`**

在 `parse_nasdaq_info()` 之後新增:

```python
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
                got = parser(fetch(url, retries=2), ref)
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
```

- [ ] **Step 2: 改 `main()` 的抓取與分派**

`main()` 第 265-267 行目前是:

```python
    twse_date, twse = fetch_twse_closes()
    tpex_date, tpex = fetch_tpex_closes()
    dispo, attn = load_dispo()
```

改為:

```python
    twse_date, twse = fetch_twse_closes()
    tpex_date, tpex = fetch_tpex_closes()
    us_codes = [c for c, m in stocks.items() if m["market"] == "us"]
    us = fetch_us_closes(us_codes) if us_codes else {}
    dispo, attn = load_dispo()
```

第 273-275 行目前是:

```python
    for code, meta in stocks.items():
        close = (tpex if meta["market"] == "tpex" else twse).get(code)
        pdate = tpex_date if meta["market"] == "tpex" else twse_date
```

改為:

```python
    for code, meta in stocks.items():
        if meta["market"] == "us":
            got = us.get(code)
            close, pdate = (got[1], got[0]) if got else (None, "")
        else:
            close = (tpex if meta["market"] == "tpex" else twse).get(code)
            pdate = tpex_date if meta["market"] == "tpex" else twse_date
```

- [ ] **Step 3: 改 `main()` 的輸出與告警**

第 298-302 行目前是:

```python
    json.dump({"updated": today.isoformat(), "prices": prices},
              open(PRICES_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    got = sum(1 for v in prices.values() if v["close"] is not None)
    print(f"done: prices {got}/{len(prices)} | dispo hits "
          f"{sum(1 for c in stocks if c in dispo)} | attn hits {sum(1 for c in stocks if c in attn)}")
```

改為:

```python
    # 已登記美股但一檔都沒抓到 = 兩家來源同時封鎖(很可能是機房 IP),必須讓 workflow 轉紅。
    # 刻意不在此 sys.exit(1):那會讓 workflow 跳過 commit 步驟,連當天台股更新一起丟掉。
    # 由 workflow 在 push 之後讀這個欄位決定成敗。
    errors = ["us_all_failed"] if (us_codes and not us) else []
    json.dump({"updated": today.isoformat(), "errors": errors, "prices": prices},
              open(PRICES_JSON, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    got = sum(1 for v in prices.values() if v["close"] is not None)
    print(f"done: prices {got}/{len(prices)} | us {len(us)}/{len(us_codes)} | dispo hits "
          f"{sum(1 for c in stocks if c in dispo)} | attn hits {sum(1 for c in stocks if c in attn)}"
          + (f" | ERRORS {errors}" if errors else ""))
```

- [ ] **Step 4: 驗證既有測試仍通過,且腳本可執行**

```bash
cd ~/stock-research-notes && python3 -m unittest discover -s tests -q
```

Expected: `Ran 9 tests` + `OK`

```bash
cd ~/stock-research-notes && python3 scripts/update_freshness.py
```

Expected: 印出 `done: prices 19/19 | us 0/0 | dispo hits ... | attn hits ...`
(此時 `stocks.json` 尚未登記美股,`us_codes` 為空是正確的;`errors` 不會觸發。)

- [ ] **Step 5: 確認未污染 index.html**

```bash
cd ~/stock-research-notes && git diff --stat
```

Expected: 只有 `data/prices.json` 有變動(多出 `errors` 欄位),`index.html` 若有變動只能是 `.fresh` 的日期/偏移數字。**若 diff 觸及任何分析本文,立刻 `git checkout index.html` 並回報。**

- [ ] **Step 6: Commit**

```bash
cd ~/stock-research-notes
git add scripts/update_freshness.py data/prices.json
git commit -m "$(cat <<'EOF'
Fetch US closes with a three-hop no-key fallback chain

Yahoo query1 -> query2 -> Nasdaq, per ticker. Records us_all_failed in
prices.json instead of exiting non-zero, so a US-source outage still lets
the day's Taiwan update commit and push; the workflow gates on the field
afterwards.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 3: 卡片改造與個股登記

讓 MU / SNDK 真正進入自動更新範圍。

**Files:**
- Modify: `scripts/update_freshness.py`(`build_dispo_badge` 182-189 行、其 `main()` 內的呼叫點 284 行)
- Modify: `data/stocks.json`
- Modify: `index.html`(105-115 行的兩張美股卡片)

**Interfaces:**
- Consumes: `fetch_us_closes()`(Task 2)
- Produces: `build_dispo_badge(code, dispo, attn, market) -> str`(新增第四個參數 `market`)

- [ ] **Step 1: `build_dispo_badge()` 加 market 參數**

第 182-189 行目前是:

```python
def build_dispo_badge(code, dispo, attn):
    if code in dispo:
```

改為:

```python
def build_dispo_badge(code, dispo, attn, market):
    # 美股無處置/注意股制度,徽章恆空。保留空標籤讓 SKILL.md 只需一套卡片規則,
    # 且日後若要加美股專屬徽章有現成掛點。
    if market == "us":
        return ""
    if code in dispo:
```

`main()` 第 284 行目前是:

```python
        badge = build_dispo_badge(code, dispo, attn)
```

改為:

```python
        badge = build_dispo_badge(code, dispo, attn, meta["market"])
```

- [ ] **Step 2: 登記 `data/stocks.json`**

把 `_comment` 那行的 `market=twse|tpex` 改成 `market=twse|tpex|us`,並補上說明。整段 `_comment` 改為:

```json
  "_comment": "研究筆記個股註冊表。發佈新分析時必須登記/更新:market=twse|tpex|us,analysis_date/analysis_price=該次分析所採用的基準價(台股盤後分析用收盤價;美股盤中分析就用當下價,幣別 USD),須與儀表板內文的價位錨點一致。tags=中文產業分類(update_freshness.py 會用 twse-disposition 的 stock_info.json tags 補充,美股無此來源)",
```

在 `_comment` 之後、`"2337"` 之前插入兩筆:

```json
  "MU": {"name": "美光 Micron", "market": "us", "file": "MU-micron.html", "analysis_date": "2026-07-27", "analysis_price": 871.0, "tags": ["記憶體"]},
  "SNDK": {"name": "SanDisk", "market": "us", "file": "SNDK-sandisk.html", "analysis_date": "2026-07-27", "analysis_price": 1270.0, "tags": ["記憶體"]},
```

`analysis_price` 的 871.0 / 1270.0 是 2026-07-27 美股盤中價,兩份儀表板內文的情境價、停損、目標價距離全部錨定這兩個數字,**不得改成當日收盤價**,否則站上顯示的偏移 % 會與分析本文脫節。

- [ ] **Step 3: 改 MU 卡片**

`index.html` 第 106 行,把:

```html
<span class="date">資料 2026/07/27(美股盤中)|製表 2026/07/27・美股,非本站自動更新範圍</span>
```

改為:

```html
<span class="date">資料 2026/07/27(美股盤中)|製表 2026/07/27</span>
```

第 107 行,把結尾的 `</div>` 前補上 `autodispo` 佔位——整行改為:

```html
  <div class="badges"><span class="badge amber">估值:合理偏低(CXMT疑慮下的錯殺可能性)</span><span class="badge green">結論:逢回布局,獲利動能三雄最強</span><span class="autodispo" data-code="MU"></span></div>
```

第 108 行的 `.oneliner` **不動**。在第 108 行之後、第 109 行的 `</a>` 之前,插入一行:

```html
  <div class="fresh" data-code="MU">現價更新中(每日盤後自動更新)</div>
```

- [ ] **Step 4: 改 SNDK 卡片**

第 112 行,把:

```html
<span class="date">資料 2026/07/27(美股盤中)|製表 2026/07/27・美股,非本站自動更新範圍</span>
```

改為:

```html
<span class="date">資料 2026/07/27(美股盤中)|製表 2026/07/27</span>
```

第 113 行改為:

```html
  <div class="badges"><span class="badge red">估值:高度不確定(暴漲後首次重挫)</span><span class="badge amber">結論:僅適合投機倉位,嚴設停損</span><span class="autodispo" data-code="SNDK"></span></div>
```

第 114 行的 `.oneliner` **不動**。在第 114 行之後、第 115 行的 `</a>` 之前,插入一行:

```html
  <div class="fresh" data-code="SNDK">現價更新中(每日盤後自動更新)</div>
```

- [ ] **Step 5: 執行腳本並驗證**

```bash
cd ~/stock-research-notes && python3 -m unittest discover -s tests -q && python3 scripts/update_freshness.py
```

Expected: 測試 `OK`,腳本印出 `done: prices 21/21 | us 2/2 | ...`(無 `ERRORS`)

```bash
cd ~/stock-research-notes && grep -A1 'class="fresh" data-code="MU"' index.html && grep -A1 'class="fresh" data-code="SNDK"' index.html
```

Expected: 兩行都含「最新收盤 <b>數字</b>(MM/DD)」與「較分析價 ±x.x%」與「資料齡 N 天」,**不是**「現價更新中」也**不是**「查無」。

```bash
cd ~/stock-research-notes && python3 -c "
import json; d=json.load(open('data/prices.json'))
print('errors:', d['errors'])
for k in ('MU','SNDK'): print(k, d['prices'][k])
"
```

Expected: `errors: []`,兩檔的 `close` 非 `null` 且 `date` 為 8 位數字串。

- [ ] **Step 6: 確認台股 19 檔無回歸**

```bash
cd ~/stock-research-notes && git diff index.html | grep '^[-+]' | grep -c 'oneliner\|badge amber\|badge green\|badge red'
```

Expected: `2` — 只有 Task 3 Step 3/4 手動改的兩張美股 badges 列。若數字更大,代表腳本動到了分析本文,立刻回報。

- [ ] **Step 7: Commit**

```bash
cd ~/stock-research-notes
git add scripts/update_freshness.py data/stocks.json data/prices.json index.html
git commit -m "$(cat <<'EOF'
Bring MU and SNDK into the daily freshness update

Registers both US tickers in stocks.json, adds the .fresh and .autodispo
placeholders to their cards, and drops the "not auto-updated" note.
build_dispo_badge now takes market and returns empty for US, which has no
disposition regime.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 4: 催化劑日曆納入美股

**Files:**
- Modify: `scripts/update_freshness.py`(`build_calendar_html` 192-256 行,僅個股欄的 `<a>` 那段)
- Modify: `data/calendar.json`

**Interfaces:**
- Consumes: `data/stocks.json` 的美股條目(Task 3)
- Produces: 無新函式;`build_calendar_html()` 簽章不變

- [ ] **Step 1: 個股欄加 US 標記**

`build_calendar_html()` 第 248-251 行目前是:

```python
        rows.append(f'<tr><td style="white-space:nowrap;vertical-align:top">{head_html}</td>'
                    f'<td style="vertical-align:top"><a href="{href}" '
                    f'style="color:var(--blue);text-decoration:none">{name} {code}</a></td>'
                    f'<td>{"".join(lines)}</td></tr>')
```

改為:

```python
        mk = (' <span style="color:var(--muted);font-size:11px">US</span>'
              if stocks.get(code, {}).get("market") == "us" else "")
        rows.append(f'<tr><td style="white-space:nowrap;vertical-align:top">{head_html}</td>'
                    f'<td style="vertical-align:top"><a href="{href}" '
                    f'style="color:var(--blue);text-decoration:none">{name} {code}</a>{mk}</td>'
                    f'<td>{"".join(lines)}</td></tr>')
```

`mk` 放在 `</a>` 之外,標記本身不成為連結的一部分。

- [ ] **Step 2: 查證 MU / SNDK 的財報日**

用 WebSearch 查兩檔的下次財報日:

- `Micron FQ4 2026 earnings date`
- `SanDisk FQ4 2026 earnings date`

判定規則:
- 查到公司官方 IR 公告的**確認日期** → `"approx": false`
- 只查到分析機構/財經站的**預估日期** → `"approx": true`
- **完全查無 → 不要編造日期。** 改用該股儀表板內文已寫明的驗證點,搭配該公司歷史財報月份推估的月中日期,`"approx": true`,並在 `event` 文字開頭標明「(日期推估)」。

- [ ] **Step 3: 登記 `data/calendar.json`**

在 `events` 陣列**最前面**插入兩檔的事件。沿用檔內既有格式(參考已存在的 `2317` 那筆確認日期事件寫法):

```json
    {"date": "YYYY-MM-DD", "code": "MU", "event": "<Step 2 查到的財報期別>財報:FQ4 財測營收 $50B±1B、EPS $31±1 是否兌現,以及 CXMT 上市後管理層對 HBM 領先地位的說法", "approx": <Step 2 判定>},
    {"date": "YYYY-MM-DD", "code": "SNDK", "event": "<Step 2 查到的財報期別>財報:QoQ 高速成長是否延續(上季 QoQ+97%),以及 420 億美元 AI 供應協議的入帳節奏", "approx": <Step 2 判定>},
```

`date` 與 `approx` 由 Step 2 的查證結果填入,`event` 文字照抄上面(這兩段驗證點取自兩份儀表板的結論段,已確認可用)。

同時把 `_comment` 中的說明補上美股:整段改為:

```json
  "_comment": "催化劑日曆。發佈新分析時把該股的驗證點事件登記進 events;approx=true 表示日期為推估。台股/美股共用同一份,code 直接用該市場代號。處置迄日由 update_freshness.py 動態附加,不寫在這裡。",
```

- [ ] **Step 4: 執行並驗證日曆**

```bash
cd ~/stock-research-notes && python3 scripts/update_freshness.py && python3 - <<'EOF'
import re
s = open("index.html", encoding="utf-8").read()
cal = re.search(r"<!--CALENDAR_START-->(.*?)<!--CALENDAR_END-->", s, re.S).group(1)
for code in ("MU", "SNDK"):
    row = re.search(rf'<a href="[^"]*"[^>]*>[^<]*{code}</a>(.*?)</td>', cal)
    print(code, "在日曆:", bool(row), "| US 標記:", bool(row and "US" in row.group(1)))
EOF
```

Expected: 兩檔皆 `在日曆: True | US 標記: True`

- [ ] **Step 5: 確認台股日曆列無回歸**

```bash
cd ~/stock-research-notes && python3 -c "
import re
s=open('index.html',encoding='utf-8').read()
cal=re.search(r'<!--CALENDAR_START-->(.*?)<!--CALENDAR_END-->',s,re.S).group(1)
print('日曆列數:', cal.count('<tr>')-1)
print('誤掛 US 標記的台股列:', len(re.findall(r'>\d{4}</a> <span[^>]*>US</span>', cal)))
"
```

Expected: 日曆列數 ≥ 台股原有列數 + 2;誤掛數為 `0`

- [ ] **Step 6: Commit**

```bash
cd ~/stock-research-notes
git add scripts/update_freshness.py data/calendar.json index.html
git commit -m "$(cat <<'EOF'
Add MU and SNDK to the catalyst calendar

calendar.json was already market-agnostic; registering the tickers is
enough to make them appear. Calendar rows now carry a muted US marker so
the two markets are distinguishable in one table.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 5: Workflow 加測試步驟與美股告警

**Files:**
- Modify: `.github/workflows/update.yml`

**Interfaces:**
- Consumes: `data/prices.json` 的 `errors` 欄位(Task 2)、`tests/`(Task 1)
- Produces: 無

- [ ] **Step 1: 加測試步驟**

在「Set up Python」與「Run freshness update script」之間插入(比照 `~/twse-disposition/.github/workflows/update.yml` 第 30-31 行的既有慣例):

```yaml
      - name: Run parser tests
        run: python -m unittest discover -s tests -q
```

- [ ] **Step 2: 加美股告警步驟**

在最末的「Commit and push」步驟**之後**,新增:

```yaml
      - name: Verify US quotes
        run: |
          python3 -c "
          import json, sys
          e = json.load(open('data/prices.json')).get('errors') or []
          print('errors:', e)
          sys.exit(1 if e else 0)
          "
```

必須放在 push 之後:若放在前面且失敗,後續步驟會被跳過,連當天台股更新一起丟掉。

- [ ] **Step 3: 本機驗證 YAML 合法**

```bash
cd ~/stock-research-notes && python3 - <<'EOF'
import re
s = open(".github/workflows/update.yml", encoding="utf-8").read()
for k in ("Run parser tests", "Verify US quotes"):
    print(k, "->", k in s)
print("Verify 在 Commit 之後:",
      s.index("Verify US quotes") > s.index("Commit and push"))
EOF
```

Expected: 兩個步驟皆 `True`,且 `Verify 在 Commit 之後: True`

- [ ] **Step 4: Commit**

```bash
cd ~/stock-research-notes
git add .github/workflows/update.yml
git commit -m "$(cat <<'EOF'
Gate the workflow on parser tests and US quote availability

Tests run before the update script. The US check runs after push so a
source outage turns the job red without discarding the day's Taiwan
update.

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)"
```

---

### Task 6: SKILL.md 規則更新

讓**下次分析美股時自動遵守**,不必再手動回補。

**Files:**
- Modify: `~/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/3f4e9c12-2a54-426e-b888-aecca87c29e0/d5b5b310-3531-4d3d-a477-55daa48787b2/skills/stock-analyzer/SKILL.md`

**Interfaces:**
- Consumes: Task 3、Task 4 建立的 `stocks.json` / `calendar.json` 慣例
- Produces: 無

注意此檔**不在 git repo 內**,無法 commit;修改後在本 Task 的最後以 `grep` 驗證即可。

- [ ] **Step 1: 通用規則標明處置檢查僅台股**

第 22 行目前是:

```markdown
- **處置/注意股檢查(台股必做)**:分析任何台股前,先讀本機 `~/twse-disposition/index.html`(不存在改抓 https://nctuwanglin.github.io/twse-disposition/)查該代號——
```

改為:

```markdown
- **處置/注意股檢查(僅台股,美股略過)**:分析任何台股前,先讀本機 `~/twse-disposition/index.html`(不存在改抓 https://nctuwanglin.github.io/twse-disposition/)查該代號——美股無此制度,直接跳過本項。
```

- [ ] **Step 2: 卡片規則涵蓋美股**

第 102 行目前是:

```markdown
     - `<a class="item" href="..." data-code="代號" data-tags="中文產業(如 記憶體/封測/晶圓代工/IC設計/散熱/電源/電子代工/航空/設備)" data-concl="buy|watch">`(偏多結論=buy、觀望=watch,供篩選 chips 使用)
```

改為:

```markdown
     - `<a class="item" href="..." data-code="代號" data-tags="中文產業(如 記憶體/封測/晶圓代工/IC設計/散熱/電源/電子代工/航空/設備)" data-concl="buy|watch">`(偏多結論=buy、觀望=watch,供篩選 chips 使用);**美股另外加 `data-market="us"`**(台股不加,預設即台股)
```

第 104 行的 `.badges` 規則、第 106 行的 `.fresh` 規則目前只描述台股情境。在第 106 行之後新增一行:

```markdown
     - **美股與台股用同一套卡片規則**:`.autodispo` 與 `.fresh` 佔位美股同樣必放(美股的 autodispo 會由腳本填空字串,`.fresh` 會正常填入收盤偏移)。`.date` 欄**不得**寫「非本站自動更新範圍」之類字樣——美股已納入每日自動更新。
```

- [ ] **Step 3: `stocks.json` 登記規則涵蓋美股**

第 108 行目前是:

```markdown
   - `data/stocks.json`:新增/更新該股 `{name, market: twse|tpex, file, analysis_date, analysis_price, tags}`——analysis_price 用本次分析的基準收盤價;重分析時必須更新 analysis_date/analysis_price。
```

改為:

```markdown
   - `data/stocks.json`:新增/更新該股 `{name, market: twse|tpex|us, file, analysis_date, analysis_price, tags}`——`analysis_price` 是**該次分析所採用的基準價**,必須與儀表板內文的情境價/停損/目標價距離錨定同一個數字:台股盤後分析用收盤價,美股若在盤中分析就用當下價(幣別 USD),不要換成收盤價。重分析時必須更新 analysis_date/analysis_price。
```

- [ ] **Step 4: `calendar.json` 登記規則涵蓋美股**

第 109 行目前是:

```markdown
   - `data/calendar.json`:把本次分析指定的「驗證點」寫成事件 `{date, code, event, approx}`(日期不確定用推估日+approx:true);同時**刪除該股已過期/已驗證的舊事件**。
```

改為:

```markdown
   - `data/calendar.json`:把本次分析指定的「驗證點」寫成事件 `{date, code, event, approx}`(日期不確定用推估日+approx:true);同時**刪除該股已過期/已驗證的舊事件**。台股/美股共用同一份,`code` 直接用該市場代號;美股財報日多為公司官方公告的確認日,查得到就用 `approx:false`。
```

- [ ] **Step 5: 驗證修改**

```bash
SK="$HOME/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/3f4e9c12-2a54-426e-b888-aecca87c29e0/d5b5b310-3531-4d3d-a477-55daa48787b2/skills/stock-analyzer/SKILL.md"
grep -c '僅台股,美股略過' "$SK"
grep -c 'twse|tpex|us' "$SK"
grep -c 'data-market="us"' "$SK"
grep -c '美股與台股用同一套卡片規則' "$SK"
grep -c '非本站自動更新範圍' "$SK"
```

Expected: 前四個都是 `1`;最後一個是 `1`(只出現在新加的禁止規則裡,不是舊描述)。

---

### Task 7: 端到端驗收與上線

**Files:**
- Modify: 無(僅執行與驗證)

**Interfaces:**
- Consumes: Task 1-6 全部
- Produces: 線上生效的 https://nctuwanglin.github.io/stock-research-notes/

- [ ] **Step 1: 全套測試 + 乾淨重跑**

```bash
cd ~/stock-research-notes && python3 -m unittest discover -s tests -q && python3 scripts/update_freshness.py
```

Expected: `Ran 9 tests` `OK`;腳本印出 `done: prices 21/21 | us 2/2 | ...` 且**不含** `ERRORS`

- [ ] **Step 2: 冪等性檢查**

連跑兩次,第二次不應再產生 HTML 差異(除非跨了交易日):

```bash
cd ~/stock-research-notes && python3 scripts/update_freshness.py >/dev/null && git add -A && git stash && python3 scripts/update_freshness.py >/dev/null && git diff --stat index.html && git stash pop
```

Expected: `git diff --stat index.html` 無輸出或僅有數值變動。**若出現重複巢狀的 `<span class="autodispo">` 或 `<div class="fresh">`,代表 regex 覆寫不冪等,必須修正後才能繼續。**

- [ ] **Step 3: 對照 spec §7 驗收標準逐條確認**

```bash
cd ~/stock-research-notes && python3 - <<'EOF'
import json, re
s = open("index.html", encoding="utf-8").read()
p = json.load(open("data/prices.json"))
cal = re.search(r"<!--CALENDAR_START-->(.*?)<!--CALENDAR_END-->", s, re.S).group(1)
ok = True
for code in ("MU", "SNDK"):
    fresh = re.search(rf'<div class="fresh" data-code="{code}">(.*?)</div>', s, re.S)
    hit = bool(fresh and "較分析價" in fresh.group(1))
    incal = code in cal
    price = p["prices"].get(code, {}).get("close")
    print(f"{code}: fresh={hit} calendar={incal} close={price}")
    ok &= hit and incal and price is not None
print("errors:", p.get("errors"))
print("台股卡片數:", len(re.findall(r'class="fresh" data-code="\d{4}"', s)))
print("ALL OK" if ok and not p.get("errors") else "FAILED")
EOF
```

Expected: 兩檔皆 `fresh=True calendar=True close=<數字>`,`errors: []`,台股卡片數 `19`,最後一行 `ALL OK`

- [ ] **Step 4: Push**

```bash
cd ~/stock-research-notes && git add -A && git status --short && git commit -m "$(cat <<'EOF'
Run the US-enabled update and publish

Co-Authored-By: Claude Opus 5 <noreply@anthropic.com>
EOF
)" && git push
```

若 `git status --short` 顯示無變更,略過 commit 直接 `git push`。

- [ ] **Step 5: 驗證上線**

輪詢至回 200(每 15 秒一次,最多約 3 分鐘):

```bash
for i in $(seq 1 12); do
  code=$(curl -s -o /dev/null -w "%{http_code}" https://nctuwanglin.github.io/stock-research-notes/)
  echo "try $i: $code"
  [ "$code" = "200" ] && break
  sleep 15
done
```

再確認線上內容真的含美股更新:

```bash
curl -s https://nctuwanglin.github.io/stock-research-notes/ | grep -o 'data-code="MU">[^<]*' | head -2
```

Expected: 含「最新收盤」與「較分析價」字樣,不是「現價更新中」。

逾時就觸發重建後再驗證,不用先問(比照既有慣例):

```bash
gh api repos/nctuwanglin/stock-research-notes/pages/builds -X POST
```

- [ ] **Step 6: 手動觸發 workflow 實測機房 IP**

這是 spec §6 標記的唯一未知風險——Yahoo / Nasdaq 在 GitHub Actions 機房 IP 上能否運作,只能實測:

```bash
cd ~/stock-research-notes && gh workflow run update.yml && sleep 45 && gh run list --workflow=update.yml --limit 1
```

等執行完成後,**必須看實際 log 輸出,不能只看 conclusion 欄位**(歷史教訓:`success` 不代表有寫入資料):

```bash
cd ~/stock-research-notes && gh run view "$(gh run list --workflow=update.yml --limit 1 --json databaseId -q '.[0].databaseId')" --log | grep -E "done:|WARN us|errors:"
```

Expected: 出現 `us 2/2` 與 `errors: []`。

**若出現 `us 0/2` 或 `errors: ['us_all_failed']`**,代表機房 IP 被兩家來源同時封鎖。這是 spec §6 預期的情境,不是實作錯誤——回報使用者,並依 spec §3 決策表的備案評估改用免費 API 金鑰方案(Finnhub / Twelve Data,金鑰存 GitHub Secret)作為第三順位來源。**不要自行註冊帳號或改用需付費的服務。**

**若出現 `us 1/2`**(通常會是 SNDK 失敗,因為它只有 Yahoo 兩台主機可用,Nasdaq 對它持續回 3004),回報使用者實際失敗的是哪一檔與 `WARN us` 的訊息。
