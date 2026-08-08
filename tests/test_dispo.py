# -*- coding: utf-8 -*-
"""
處置/注意股徽章同步固定測資。

盯兩件事:
1. 個股頁的兩個佔位符必須「可重複執行而不累加」——h1 的 autodispo 內含 <span>,
   若右界沒鎖 </span></h1> 會置換到錯位置(instflow 踩過同一個坑)。
2. 名單日期抓不到時要寫「查無」,不能靜默省略或猜今天。

執行:python3 -m unittest discover -s tests -q
"""
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from update_freshness import (          # noqa: E402
    build_dispo_badge, build_dispo_note, load_dispo,
)

DISPO = {"2337": {"auction": "5 分鐘撮合", "end": "08/20"},
         "9999": {"auction": "20 分鐘撮合", "end": ""}}
ATTN = {"8027"}


def page(code, h1_inner="", sub_inner=""):
    return (f'<h1>某股 ({code}) <span class="badge amber">估值:合理</span> '
            f'<span class="autodispo" data-code="{code}">{h1_inner}</span></h1>\n'
            f'<div class="sub">產業|資料截至 X|'
            f'<span class="dispostat" data-code="{code}">{sub_inner}</span></div>')


def fill(html, code, badge, note):
    """複製 fill_dashboard_dispo 的兩條 regex,單獨驗證其置換行為。"""
    html, n1 = re.subn(rf'(<span class="autodispo" data-code="{code}">).*?(</span></h1>)',
                       lambda m: m.group(1) + badge + m.group(2), html, count=1, flags=re.S)
    html, n2 = re.subn(rf'(<span class="dispostat" data-code="{code}">)[^<]*(</span>)',
                       lambda m: m.group(1) + note + m.group(2), html, count=1)
    return html, n1, n2


class TestDispoBadge(unittest.TestCase):
    def test_disposition_with_end_date(self):
        got = build_dispo_badge("2337", DISPO, ATTN, "twse")
        self.assertEqual(got, '<span class="badge dispo">處置中·5 分鐘撮合·至 08/20</span>')

    def test_disposition_without_end_date(self):
        """迄日抓不到時不能生出「·至 」這種斷尾。"""
        got = build_dispo_badge("9999", DISPO, ATTN, "twse")
        self.assertEqual(got, '<span class="badge dispo">處置中·20 分鐘撮合</span>')

    def test_attention_only(self):
        self.assertEqual(build_dispo_badge("8027", DISPO, ATTN, "tpex"),
                         '<span class="badge attn">注意股累計中</span>')

    def test_clean_stock_is_empty(self):
        self.assertEqual(build_dispo_badge("2330", DISPO, ATTN, "twse"), "")

    def test_us_always_empty(self):
        """美股無此制度,即使誤入名單也不掛徽章。"""
        self.assertEqual(build_dispo_badge("2337", DISPO, ATTN, "us"), "")


class TestDispoNote(unittest.TestCase):
    def test_clean_stock_states_not_listed(self):
        self.assertEqual(build_dispo_note("2330", DISPO, ATTN, "2026/08/07"),
                         "未列入處置股/注意股名單(對照處置股儀表板 2026/08/07 資料)")

    def test_disposition_note(self):
        self.assertEqual(build_dispo_note("2337", DISPO, ATTN, "2026/08/07"),
                         "處置中:5 分鐘撮合,至 08/20(對照處置股儀表板 2026/08/07 資料)")

    def test_missing_date_says_unknown(self):
        """名單日期抓不到就明講查無,不可猜今天的日期。"""
        got = build_dispo_note("2330", DISPO, ATTN, "")
        self.assertIn("處置股名單日期查無", got)
        self.assertNotIn("對照處置股儀表板", got)


class TestPlaceholderRewrite(unittest.TestCase):
    def test_fills_both_placeholders(self):
        html, n1, n2 = fill(page("2337"), "2337",
                            build_dispo_badge("2337", DISPO, ATTN, "twse"),
                            build_dispo_note("2337", DISPO, ATTN, "2026/08/07"))
        self.assertEqual((n1, n2), (1, 1))
        self.assertIn('badge dispo', html)
        self.assertIn('處置中:5 分鐘撮合,至 08/20', html)

    def test_idempotent_across_runs(self):
        """跑第二次結果必須完全相同——徽章含 </span>,右界沒鎖好就會愈疊愈多。"""
        badge = build_dispo_badge("2337", DISPO, ATTN, "twse")
        note = build_dispo_note("2337", DISPO, ATTN, "2026/08/07")
        once, _, _ = fill(page("2337"), "2337", badge, note)
        twice, n1, n2 = fill(once, "2337", badge, note)
        self.assertEqual(once, twice)
        self.assertEqual((n1, n2), (1, 1))
        self.assertEqual(once.count('class="badge dispo"'), 1)

    def test_status_can_clear_when_delisted_from_watchlist(self):
        """處置期滿後徽章要能被清空,而不是永遠留著。"""
        filled, _, _ = fill(page("2337"), "2337",
                            build_dispo_badge("2337", DISPO, ATTN, "twse"),
                            build_dispo_note("2337", DISPO, ATTN, "2026/08/07"))
        cleared, n1, n2 = fill(filled, "2337",
                               build_dispo_badge("2337", {}, set(), "twse"),
                               build_dispo_note("2337", {}, set(), "2026/08/20"))
        self.assertEqual((n1, n2), (1, 1))
        self.assertNotIn('badge dispo', cleared)
        self.assertIn('未列入處置股/注意股名單', cleared)

    def test_h1_other_badges_survive(self):
        """置換不得吃掉 h1 裡原有的估值/結論徽章。"""
        filled, _, _ = fill(page("2337"), "2337",
                            build_dispo_badge("2337", DISPO, ATTN, "twse"),
                            build_dispo_note("2337", DISPO, ATTN, "2026/08/07"))
        self.assertIn('<span class="badge amber">估值:合理</span>', filled)


class TestLoadDispoDate(unittest.TestCase):
    def test_returns_three_tuple(self):
        """簽章改為三元組;來源缺失時仍須回 (空, 空, 空字串) 而非拋例外。"""
        got = load_dispo()
        self.assertEqual(len(got), 3)
        self.assertIsInstance(got[0], dict)
        self.assertIsInstance(got[1], set)
        self.assertIsInstance(got[2], str)


if __name__ == "__main__":
    unittest.main()
