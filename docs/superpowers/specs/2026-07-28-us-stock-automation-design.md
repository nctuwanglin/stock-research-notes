# 美股納入每日自動更新與催化劑日曆 — 設計文件

- 日期:2026-07-28
- 範圍:`~/stock-research-notes` repo 的 `scripts/update_freshness.py`、`index.html`、`data/*.json`、GitHub Actions workflow,以及 `stock-analyzer` skill 的 `SKILL.md` 規則
- 目標:讓美股個股與台股享有同等的每日自動更新(現價偏移/資料齡)與催化劑日曆待辦

## 1. 問題陳述

`stock-research-notes` 已有兩檔美股分析(`MU-micron.html`、`SNDK-sandisk.html`),但:

1. 兩檔**未登記** `data/stocks.json`,`update_freshness.py` 完全不認識它們;卡片沒有 `.fresh` / `.autodispo` 佔位,`.date` 欄硬寫「美股,非本站自動更新範圍」。
2. 兩檔**未登記** `data/calendar.json`,不會出現在催化劑日曆,分析裡指出的驗證點沒有任何追蹤機制。

結果是美股分析一旦發佈就靜止不動,與台股的每日更新體驗落差明顯。

## 2. 現況架構(不改動的部分)

- `update_freshness.py` 每個交易日盤後(workflow cron `20 13 * * 1-5` = 台灣 21:20)執行一次:
  - 抓 TWSE `STOCK_DAY_ALL`(整市 CSV)與 TPEx openapi(整市 JSON)收盤價
  - 就地覆寫 `index.html` 每張卡片的 `<div class="fresh" data-code="...">` 與 `<span class="autodispo" data-code="...">`
  - 由 `data/calendar.json` + 處置迄日重建 `<!--CALENDAR_START-->…<!--CALENDAR_END-->` 標記區
  - 輸出 `data/prices.json`
- `index.html` 已有市場篩選 chips(`data-f="market:tw"` / `market:us`),美股卡片已帶 `data-market="us"`,台股卡片無此屬性(視為台股)。此機制沿用,不改。
- 下游 `~/Claude Code/02. Dashboard/03. 個人績效儀表板/dashlib/related.py` 解析本 repo 的 `index.html`,但其卡片比對條件為 `class="tk">(\d{4,6})\.TW`。美股卡片的 `NASDAQ: MU` 不符合,會被靜默跳過。**本次變更不影響績效儀表板,無需配套修改。**

## 3. 決策紀錄

| 決策 | 選擇 | 理由 |
|---|---|---|
| 美股報價來源 | 免金鑰備援鏈:Yahoo chart `query1` → `query2` → api.nasdaq.com | 零註冊、維持現有 `urllib` 無外部依賴風格。Stooq CSV 已實測失效(改用 JS proof-of-work 挑戰,無 JS 抓不到)。詳細實測結果見 §4.1 |
| 排程時點 | 沿用現有單班 21:20 TW,不加班次 | 美股資料日照實顯示為前一美股交易日。`.fresh` 呈現的是「較分析價偏移 %」這種長期指標,差一個交易日不影響判讀;避免多一個受 GitHub 排程延遲影響的班次 |
| 日曆事件來源 | 手動登記,與台股同規則 | `calendar.json` schema 本來就與市場無關。自動抓財報日的兩條路都不划算:Yahoo `quoteSummary` 實測直接回 429(連住家 IP 都被擋);Nasdaq 財報日曆只能「按日期查當天有誰財報」,要往前掃 60~90 天才能定位單一 ticker,慢且脆弱,而美股確認財報日很少變動 |
| 實作結構 | 在 `update_freshness.py` 原檔新增函式 + market dispatch | 抽 provider 抽象層對三個市場是過度設計;拆獨立腳本會讓兩支程式各寫 `index.html` 一半、regex 互踩 |

## 4. 設計

### 4.1 報價抓取

新增純解析函式(不碰網路,便於測試):

```
parse_yahoo_chart(text: str, us_today: date) -> tuple[str, float] | None
parse_nasdaq_info(text: str, us_today: date) -> tuple[str, float] | None
```

回傳 `(price_date_yyyymmdd, close)`,解析不出可用資料回 `None`。

新增網路協調函式:

```
fetch_us_closes(codes: list[str]) -> dict[str, tuple[str, float]]
```

每檔依序試兩源,任一成功即停,全部失敗則該 code 不出現在回傳 dict 中。

- **來源 A / B(主)** `https://query1.finance.yahoo.com/v8/finance/chart/{code}?range=5d&interval=1d`,失敗改試 `query2.finance.yahoo.com` 同路徑。
  取 `chart.result[0].timestamp[]` 與 `chart.result[0].indicators.quote[0].close[]`,由後往前找第一根符合日期規則且 `close` 非 `null` 的日 K。時間戳為該場次**開盤**時刻(09:30 ET),換算日期需先加 `chart.result[0].meta.gmtoffset` 再取日期部分。
  **不使用 `meta.regularMarketPrice`** — 該欄位在美股盤中會回傳盤中價。
  query1 與 query2 是不同主機,分別重試對 429 速率限制有實質幫助。
- **來源 C(備)** `https://api.nasdaq.com/api/quote/{code}/info?assetclass=stocks`
  取 `data.primaryData.lastSalePrice`(格式如 `"$1,270.50"`,需去除 `$` 與千分位逗號)與 `data.primaryData.lastTradeTimestamp`(格式如 `"Jul 27, 2026 12:41 PM ET"`,取其日期部分)。
  **此端點失敗時回 HTTP 200 但 `data` 為 `null`**,錯誤碼在 `status.bCodeMessage[]`。解析器必須檢查 `data` 非 null,不能只看 HTTP 狀態碼。

**User-Agent:沿用現有 `fetch()` 的 `research-notes-updater`,不要改用瀏覽器樣 UA。** 2026-07-28 實測:帶 Chrome UA 呼叫 Yahoo chart 直接回 `Too Many Requests`,換回原 UA 立即正常。Nasdaq 對兩種 UA 行為一致。

**Nasdaq 覆蓋率有缺口,故只列第三順位。** 實測 MU、NVDA 正常,但 SNDK 連續三次回 `code 3004 "Error while calling vendor"`(非 `1001 Symbol not exists`,屬其 vendor 資料源缺該檔)。Yahoo 兩台主機對 MU、SNDK 皆正常。

### 4.2 日期規則(核心防呆)

**只接受日期嚴格早於「當前美東日期」的資料點。**

`us_today` 以 `zoneinfo.ZoneInfo("America/New_York")` 取得(Python 3.9+ 標準庫,workflow 用 3.11)。

理由:台灣 21:20 = 美東同日 09:20(開盤前),正常情況最後一根完整日 K 就是前一交易日收盤。但 GitHub 排程實測延遲 5~8 小時,執行時點可能落在台灣凌晨 02:20~05:20 = 美東同日 14:20~17:20,也就是美股盤中或剛收盤。此時來源回傳的最後一根日 K 是**未完成的盤中棒**,`lastSalePrice` 也是盤中價。

這條規則讓延遲情境下最壞結果只是「少拿一天」,**永遠不會把盤中價當收盤價寫進站上**。兩個來源共用同一條規則,行為一致。

### 4.3 失敗處理

分兩層,呼應 twse-disposition 的教訓(「run 顯示 success 不代表有寫入資料」):

**第一層 — 站上可見。** 單一檔兩源皆失敗時 `close=None`,`.fresh` 走現有的「最新收盤:查無(來源未回傳)」分支。由於 `.fresh` 是 regex 就地覆寫,**天然不會靜默沿用舊價**。

**第二層 — workflow 亮紅燈。** 若已登記美股 ≥1 檔但成功 0 檔,`prices.json` 增寫 `"errors": ["us_all_failed"]`。這是「機房 IP 被 Yahoo 與 Nasdaq 同時封鎖」的表徵,必須看得見。

**腳本本身不 `sys.exit(1)`**,而是由 workflow 在 **commit + push 之後**新增一個檢查步驟讀 `prices.json` 的 `errors` 決定成敗:

```yaml
- name: Verify US quotes
  run: |
    python3 -c "import json,sys; e=(json.load(open('data/prices.json')).get('errors') or []); print('errors:', e); sys.exit(1 if e else 0)"
```

如此台股當日更新仍會正常寫入並推上線,同時 job 轉紅通知。若腳本直接 exit 1,commit 步驟會被跳過,連台股更新一起丟掉。

台股既有的 WARN-不中斷行為維持原狀,不在本次範圍內調整。

### 4.4 卡片與資料檔

`data/stocks.json` 的 `market` 欄位新增合法值 `"us"`,並回補兩檔:

```json
"MU":   {"name": "美光 Micron", "market": "us", "file": "MU-micron.html",
         "analysis_date": "2026-07-27", "analysis_price": 871.0, "tags": ["記憶體"]},
"SNDK": {"name": "SanDisk", "market": "us", "file": "SNDK-sandisk.html",
         "analysis_date": "2026-07-27", "analysis_price": 1270.0, "tags": ["記憶體"]}
```

`analysis_price` 的語義是**該次分析所採用的基準價**,不是「收盤價」。台股用收盤價只是因為台股分析都在盤後進行。MU `$871` 與 SNDK `$1,270` 是 2026-07-27 美股盤中價,兩份儀表板內文的所有價位推導(情境價、停損、目標價距離)都錨定在這兩個數字,因此照抄以維持一致。

`index.html` 的 MU / SNDK 兩張卡片:

- `.date` 移除「・美股,非本站自動更新範圍」字樣
- `.badges` 列尾補 `<span class="autodispo" data-code="MU"></span>`
- 卡片末(`</a>` 前)補 `<div class="fresh" data-code="MU">現價更新中(每日盤後自動更新)</div>`
- `data-market="us"` 已存在,保留

`build_dispo_badge()` 目前簽章為 `(code, dispo, attn)`,需擴充為 `(code, dispo, attn, market)`,遇 `market == "us"` 直接回空字串(呼叫端已持有 `meta["market"]`)。保留空的 `autodispo` 標籤是刻意的——美股無處置制度,但維持標籤讓 SKILL.md 只需一套卡片規則,且日後若要加美股專屬徽章有現成掛點。

`data/prices.json` 的 `date` 欄位本來就是 per-code,台美各自帶自己的資料日,結構不變(僅頂層新增選用的 `errors` 陣列)。

`load_tags_supplement()` 不需修改:其資料來自 twse-disposition 的 `stock_info.json`,美股代號自然 miss。

### 4.5 催化劑日曆

`build_calendar_html()` 本體幾乎不動:

- `stocks.get(code)` 已能取得美股的 `name` / `file`,連結與名稱自動正確
- 處置迄日附加迴圈只走台股 `dispo` dict,美股不會誤入
- 逾期/7 天內的顏色標記邏輯與市場無關,直接沿用

唯一新增:美股列在個股名稱後附加淡色 `US` 標記,便於在同一張表裡區分市場。

`data/calendar.json` 補登 MU / SNDK 的財報日與驗證點。美股確認財報日用 `approx: false`;若實作時查到的僅為預估日則用 `approx: true`。

### 4.6 SKILL.md 規則更新

`stock-analyzer` skill 位於
`~/Library/Application Support/Claude/local-agent-mode-sessions/skills-plugin/3f4e9c12-2a54-426e-b888-aecca87c29e0/d5b5b310-3531-4d3d-a477-55daa48787b2/skills/stock-analyzer/SKILL.md`

需修改四處,確保**下次分析美股時自動遵守**,不必再手動回補:

1. 「通用規則」的處置/注意股檢查,明確標註**僅台股適用**,美股略過。
2. 「發佈到 GitHub Pages」步驟 2 的卡片規則:美股卡片同樣必放 `.fresh` 與 `.autodispo` 佔位,並帶 `data-market="us"`;移除任何「美股非自動更新範圍」的措辭。
3. 步驟 3 的 `stocks.json` 登記規則:`market: twse|tpex|us`;`analysis_price` 定義改為「該次分析所採用的基準價(美股盤中分析就用當下價),幣別 USD,須與儀表板內文的價位錨點一致」。
4. 步驟 3 的 `calendar.json` 登記規則涵蓋美股:財報日與驗證點同樣要登記,確認日期用 `approx: false`。

## 5. 測試

repo 目前無 `tests/` 目錄。新增 `tests/test_us_quotes.py`,**全部使用存檔 fixture,不打網路**(fixture 置於 `tests/fixtures/`):

| 測試案例 | 預期 |
|---|---|
| Yahoo 正常回應 | 取到最後一根完整日 K 的 close 與其日期 |
| Yahoo 回應含 `close: null` | 跳過該根,往前找下一根有效日 K |
| Yahoo 最後一根為當前美東日期(盤中棒) | 被日期規則排除,取前一根 |
| Yahoo 回應無 `chart.result`(429 / 錯誤頁) | 回 `None` |
| Nasdaq `"$1,270.50"` | 解析為 `1270.5` |
| Nasdaq `lastTradeTimestamp` 為當前美東日期 | 被日期規則排除,回 `None` |
| Nasdaq HTTP 200 但 `data: null`(`code 3004`) | 回 `None`,不可拋例外 |
| 已登記美股但兩源皆失敗 | `prices.json` 出現 `errors: ["us_all_failed"]` |

workflow 比照 twse-disposition,在執行更新腳本**之前**先跑測試。

## 6. 已知風險

**Yahoo 與 Nasdaq 在 GitHub Actions 機房 IP 上能否運作,只能上線後實測。** 兩家都是非官方端點,對資料中心 IP 的封鎖政策不透明;本機(住家 IP)實測 Yahoo chart 兩台主機皆可用,但 Yahoo 的 `quoteSummary` 端點連住家 IP 都已回 429、chart 端點帶瀏覽器 UA 也會被 429,顯示其風控確實在收緊。

**Nasdaq 這層備援不保證覆蓋所有 ticker**(SNDK 實測持續失敗,見 §4.1),因此實際上 SNDK 只有 Yahoo 兩台主機可用。若 Yahoo 全面封鎖機房 IP,SNDK 會最先失去報價,並由 §4.3 第二層告警反映出來。

這正是 4.3 第二層告警存在的理由:若上線後美股報價持續失敗,workflow 會轉紅,屆時再依 §3 決策表的備案改用免費 API 金鑰方案(Finnhub / Twelve Data,金鑰存 GitHub Secret)作為第三順位來源。此備案不在本次實作範圍。

## 7. 驗收標準

1. `python3 scripts/update_freshness.py` 在本機執行後,`index.html` 的 MU / SNDK 卡片出現「最新收盤 $xxx(MM/DD)▲/▼ 較分析價 ±x.x%|資料齡 N 天」。
2. 催化劑日曆表格出現 MU / SNDK 列,名稱帶 `US` 標記,連結指向對應 HTML。
3. `data/prices.json` 含 MU / SNDK 條目且 `close` 非 null、`errors` 不存在或為空。
4. `tests/test_us_quotes.py` 全數通過。
5. 台股 19 檔的 `.fresh`、`.autodispo`、日曆列與變更前一致(無回歸)。
6. 變更推上線後,`https://nctuwanglin.github.io/stock-research-notes/` 可見上述效果。
7. SKILL.md 四處規則已更新。
