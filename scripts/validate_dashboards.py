#!/usr/bin/env python3
"""
發佈前一致性驗證器 — 擋住「同一個數字散落多處、彼此不一致」這類錯誤。

背景:停損價手工維護在 3 個地方(verdictbox vstop / 交易計畫硬停損 / stocks.json.stop),
分析基準價手工維護在 4 個地方(現價卡 / 交叉檢核表頭 / 情境圖▲ / stocks.json.analysis_price),
過去沒有任何交叉校驗,導致同一頁出現兩個出場價、買進階梯整段落在停損之下等問題。

用法:
    python3 scripts/validate_dashboards.py              # 報告模式,永遠 exit 0
    python3 scripts/validate_dashboards.py --strict     # 有 ERROR 就 exit 1(CI 用)
    python3 scripts/validate_dashboards.py --file 2330-tsmc.html
    python3 scripts/validate_dashboards.py --json       # 機器可讀
"""
import argparse, glob, json, os, re, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LADDER_TOL = 1.5      # 情境圖座標容差(百分點)
PCT_TOL    = 1.0      # 百分比重算容差(百分點)


def num(s):
    """'2,470.5' / '$140' / '1950' -> float"""
    if s is None:
        return None
    s = str(s).replace(',', '').replace('$', '').strip()
    m = re.search(r'-?\d+(?:\.\d+)?', s)
    return float(m.group()) if m else None


def load_json(path, default=None):
    try:
        with open(os.path.join(ROOT, path), encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return default if default is not None else {}


# ---------------- 抽取器:把散落各處的同一個數字撈出來 ----------------

def extract(html):
    d = {}

    m = re.search(r'<span class="vstop">\s*([^<]+?)\s*</span>', html)
    d['vstop'] = num(m.group(1)) if m else None

    m = re.search(r'硬停損[^<]*?<b class="red">\s*([^<]+?)\s*</b>', html)
    d['plan_stop'] = num(m.group(1)) if m else None

    m = re.search(r'現價[^<]{0,30}</div>\s*<div class="value[^"]*">\s*([^<]+?)\s*</div>', html)
    d['card_price'] = num(m.group(1)) if m else None

    m = re.search(r'<th>\s*現價\s*([\d,\.\$]+)\s*vs', html)
    d['table_price'] = num(m.group(1)) if m else None

    m = re.search(r'<div class="mark below"[^>]*>.*?<div class="price">\s*([^<]+?)\s*</div>', html, re.S)
    d['ladder_price'] = num(m.group(1)) if m else None

    m = re.search(r'<div class="mark below"[^>]*style="left:\s*([\d.]+)%', html)
    d['ladder_price_left'] = float(m.group(1)) if m else None

    m = re.search(r'刻度\s*([\d,\.]+)\s*[~～]\s*([\d,\.]+)', html)
    d['scale'] = (num(m.group(1)), num(m.group(2))) if m else None

    # 左側計畫三批:第一批 / 第二批 / 第三批
    batches = []
    for label in ('第一批', '第二批', '第三批'):
        mb = re.search(rf'<td>\s*{label}\s*</td>\s*<td[^>]*>\s*([\d,\.\$]+)\s*[~～\-–]\s*([\d,\.\$]+)\s*</td>', html)
        if mb:
            batches.append((label, num(mb.group(1)), num(mb.group(2))))
    d['batches'] = batches

    # 四情境標記(不含現價▲)
    marks = []
    for m in re.finditer(
            r'<div class="mark"[^>]*style="left:\s*([\d.]+)%"[^>]*>(.*?)(?=<div class="mark|</div>\s*<table|\Z)',
            html, re.S):
        left, blk = float(m.group(1)), m.group(2)
        pm = re.search(r'<div class="price[^"]*"[^>]*>\s*([^<]+?)\s*</div>', blk)
        lm = re.search(r'<div class="lbl">\s*([^<]+?)\s*</div>', blk)
        if not pm:
            continue
        nums = [num(x) for x in re.findall(r'[\d,]+(?:\.\d+)?', pm.group(1))]
        nums = [n for n in nums if n is not None]
        if not nums:
            continue
        marks.append({'left': left, 'label': (lm.group(1) if lm else '?'),
                      'text': pm.group(1), 'mid': sum(nums) / len(nums)})
    d['marks'] = marks
    return d


# ---------------- 檢查規則 ----------------

def check_file(fname, html, meta, close):
    """回傳 [(level, rule, message), ...]"""
    out = []
    E = lambda r, m: out.append(('ERROR', r, m))
    W = lambda r, m: out.append(('WARN', r, m))

    d = extract(html)
    js_stop   = num(meta.get('stop'))
    js_target = num(meta.get('target'))
    js_price  = num(meta.get('analysis_price'))
    rating    = meta.get('rating')

    # R1 停損三處一致
    stops = {'verdictbox': d['vstop'], '交易計畫': d['plan_stop'], 'stocks.json': js_stop}
    present = {k: v for k, v in stops.items() if v is not None}
    if len(set(present.values())) > 1:
        E('R1-停損不一致', '停損價三處不同:' + '、'.join(f'{k}={v:g}' for k, v in present.items()))

    # R2 基準價四處一致
    prices = {'現價卡': d['card_price'], '交叉檢核表頭': d['table_price'],
              '情境圖': d['ladder_price'], 'stocks.json': js_price}
    present = {k: v for k, v in prices.items() if v is not None}
    if len(set(present.values())) > 1:
        E('R2-基準價不一致', '分析基準價多處不同:' + '、'.join(f'{k}={v:g}' for k, v in present.items()))

    # R3 交易計畫邏輯:批次不得低於停損、須嚴格遞減不重疊
    stop = d['plan_stop'] if d['plan_stop'] is not None else js_stop
    if stop is not None and d['batches']:
        below = [b for b in d['batches'] if b[1] < stop]
        if below:
            allb = len(below) == len(d['batches'])
            msg = '、'.join(f'{b[0]} {b[1]:g}~{b[2]:g}' for b in below)
            if allb:
                E('R3-批次全低於停損', f'全部 {len(below)} 批都在硬停損 {stop:g} 之下,整份左側計畫無法執行:{msg}')
            else:
                E('R3-批次低於停損', f'批次下限低於硬停損 {stop:g}(該區間永遠不會成交):{msg}')
    for i in range(len(d['batches']) - 1):
        (l1, lo1, hi1), (l2, lo2, hi2) = d['batches'][i], d['batches'][i + 1]
        if hi2 >= lo1:
            E('R3-批次重疊', f'{l1} {lo1:g}~{hi1:g} 與 {l2} {lo2:g}~{hi2:g} 重疊或順序反轉')

    # R4 停損 vs 最新收盤
    if stop is not None and close is not None and stop >= close:
        E('R4-停損高於現價', f'硬停損 {stop:g} ≥ 最新收盤 {close:g},計畫已失效卻仍列出買進階梯'
                          + (f'(評等仍為 {rating})' if rating == 'buy' else ''))

    # R5 評等門檻(對應目標價校準規則:偏多≥+10%、觀望≤-10%)
    if js_target and js_price:
        up = (js_target / js_price - 1) * 100
        if rating == 'buy' and up < 10:
            E('R5-評等門檻', f'評等 buy 但上檔僅 {up:+.1f}%(需 ≥+10%)')
        elif rating == 'avoid' and up > -10:
            E('R5-評等門檻', f'評等 avoid 但上檔為 {up:+.1f}%(需 ≤-10%)')
        elif rating == 'hold' and not (-10 <= up <= 15):
            W('R5-評等門檻', f'評等 hold 但上檔為 {up:+.1f}%(中性帶為 -10%~+15%)')

    # R6 「折價」用詞須用 (1 - 現價/目標),不可填上漲空間
    if js_target and js_price:
        up   = (js_target / js_price - 1) * 100
        disc = (1 - js_price / js_target) * 100
        for m in re.finditer(r'折價\s*([\d.]+)\s*%', html):
            v = float(m.group(1))
            if abs(v - up) < PCT_TOL and abs(v - disc) > PCT_TOL:
                E('R6-折價用詞', f'寫「折價 {v}%」但該數字其實是上漲空間;真折價為 {disc:.1f}%')
                break

    # R7 情境圖座標須與刻度公式相符
    if d['scale']:
        lo, hi = d['scale']
        if hi and lo is not None and hi != lo:
            allm = list(d['marks'])
            if d['ladder_price'] is not None and d['ladder_price_left'] is not None:
                allm.append({'left': d['ladder_price_left'], 'label': '現價▲',
                             'text': f"{d['ladder_price']:g}", 'mid': d['ladder_price']})
            for mk in allm:
                exp = 4 + (mk['mid'] - lo) / (hi - lo) * 92
                if abs(mk['left'] - exp) > LADDER_TOL:
                    lvl = E if mk['label'] == '現價▲' else W
                    lvl('R7-情境圖座標',
                        f"{mk['label']}({mk['text']}) 位置 {mk['left']}% 應為 {exp:.1f}%(差 {mk['left']-exp:+.1f}pp)")

    # R8 佔位符不得殘留
    for ph in ('目標價載入中', '籌碼資料更新中', '現價更新中'):
        if ph in html:
            E('R8-佔位符殘留', f'發佈版仍含未填充的佔位符「{ph}」')
    if re.search(r'<span class="dispostat" data-code="[^"]+"></span>', html):
        E('R8-佔位符殘留', '`.dispostat` 為空(處置/注意股狀態未填)')

    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--strict', action='store_true', help='有 ERROR 就 exit 1(CI 用)')
    ap.add_argument('--file', help='只檢查單一檔案')
    ap.add_argument('--json', action='store_true', help='輸出 JSON')
    args = ap.parse_args()

    stocks = {k: v for k, v in load_json('data/stocks.json').items() if isinstance(v, dict)}
    by_file = {v['file']: (k, v) for k, v in stocks.items() if 'file' in v}
    praw = load_json('data/prices.json')
    pmap = praw.get('prices', praw) if isinstance(praw, dict) else {}

    files = [args.file] if args.file else sorted(
        os.path.basename(f) for f in glob.glob(os.path.join(ROOT, '*.html'))
        if os.path.basename(f) != 'index.html')

    report, n_err, n_warn = {}, 0, 0
    for fname in files:
        path = os.path.join(ROOT, fname)
        if not os.path.exists(path):
            print(f'找不到檔案:{fname}', file=sys.stderr)
            continue
        code, meta = by_file.get(fname, (None, {}))
        pe = pmap.get(code) if code else None
        close = num(pe.get('close')) if isinstance(pe, dict) else None
        with open(path, encoding='utf-8') as f:
            html = f.read()
        issues = check_file(fname, html, meta, close)
        if issues:
            report[fname] = issues
            n_err += sum(1 for i in issues if i[0] == 'ERROR')
            n_warn += sum(1 for i in issues if i[0] == 'WARN')

    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        for fname, issues in report.items():
            code, _ = by_file.get(fname, (None, {}))
            print(f'\n── {fname}  [{code}]')
            for lvl, rule, msg in issues:
                print(f'   {"✗" if lvl=="ERROR" else "!"} {lvl:5} {rule:18} {msg}')
        clean = len(files) - len(report)
        print(f'\n{"="*74}')
        print(f'檢查 {len(files)} 份:乾淨 {clean}、有問題 {len(report)}  |  ERROR {n_err}、WARN {n_warn}')
        by_rule = {}
        for issues in report.values():
            for lvl, rule, _ in issues:
                by_rule.setdefault(rule, [0, 0])[0 if lvl == 'ERROR' else 1] += 1
        if by_rule:
            print(f'{"="*74}\n按規則統計:')
            for rule, (e, w) in sorted(by_rule.items(), key=lambda x: -(x[1][0] + x[1][1])):
                print(f'   {rule:22} ERROR {e:3}  WARN {w:3}')

    sys.exit(1 if (args.strict and n_err) else 0)


if __name__ == '__main__':
    main()
