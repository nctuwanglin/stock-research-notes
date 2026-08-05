# -*- coding: utf-8 -*-
"""
三大法人籌碼解析固定測資。
TWSE/TPEx 改欄位順序或改 stat 大小寫時這裡會先紅。
(歷史教訓:TPEx 的 stat 是小寫 'ok',第一版誤寫成 'OK' 導致整個上櫃靜默歸零,
 腳本照樣 exit 0,只有靠人工比對才發現。)
執行:python3 -m unittest discover -s tests -q
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from update_chips import (          # noqa: E402
    parse_twse_chips, parse_tpex_chips, build_chips_html, roc_str, _lots, collect,
)
from datetime import date          # noqa: E402
from unittest.mock import patch    # noqa: E402

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


class TestTwseChips(unittest.TestCase):
    def test_known_values(self):
        """鴻海 2026/08/04:外資 +2,131 張、投信 -520、自營 -1,104、合計 +506。"""
        got = parse_twse_chips(load("twse_t86.json"))
        self.assertEqual(got["2317"],
                         {"foreign": 2131, "trust": -520, "dealer": -1104, "total": 506})

    def test_foreign_includes_foreign_dealer(self):
        """外資須為「外陸資 + 外資自營商」兩欄之和,不可只取其一。"""
        got = parse_twse_chips(load("twse_t86.json"))
        self.assertEqual(got["2454"]["foreign"], -601)

    def test_components_sum_to_total(self):
        """三者相加必須等於官方合計欄,否則代表取錯欄位。"""
        for code, r in parse_twse_chips(load("twse_t86.json")).items():
            self.assertLessEqual(
                abs(r["foreign"] + r["trust"] + r["dealer"] - r["total"]), 1,
                f"{code} 各項加總與合計不符:{r}")

    def test_skips_non_four_digit_codes(self):
        """ETF/權證等非四位數代號要濾掉,避免污染追蹤清單。"""
        got = parse_twse_chips(load("twse_t86.json"))
        self.assertNotIn("00981A", got)
        self.assertIn("3008", got)

    def test_holiday_returns_empty(self):
        """非交易日 stat 非 OK,必須回空 dict 讓呼叫端跳過該日。"""
        self.assertEqual(parse_twse_chips(load("twse_t86_holiday.json")), {})

    def test_garbage_returns_empty_not_raise(self):
        """被風控擋掉時回的是 HTML,必須回空而不是拋例外。"""
        self.assertEqual(parse_twse_chips("<html>Forbidden</html>"), {})


class TestTpexChips(unittest.TestCase):
    def test_known_values(self):
        """雙鴻 2026/08/04:外資 +58 張、投信 -1、自營 -15、合計 +42。"""
        got = parse_tpex_chips(load("tpex_3insti.json"))
        self.assertEqual(got["3324"],
                         {"foreign": 58, "trust": -1, "dealer": -15, "total": 42})

    def test_lowercase_stat_accepted(self):
        """回歸測試:TPEx 的 stat 是小寫 'ok',不可因大小寫比對失敗而整批歸零。"""
        raw = json.loads(load("tpex_3insti.json"))
        self.assertEqual(raw["stat"], "ok")          # 測資本身確實是小寫
        self.assertTrue(parse_tpex_chips(load("tpex_3insti.json")))

    def test_uppercase_stat_also_accepted(self):
        """若 TPEx 哪天改成大寫也不能掛,兩種都要吃。"""
        raw = json.loads(load("tpex_3insti.json"))
        raw["stat"] = "OK"
        self.assertTrue(parse_tpex_chips(json.dumps(raw)))

    def test_components_sum_to_total(self):
        for code, r in parse_tpex_chips(load("tpex_3insti.json")).items():
            self.assertLessEqual(
                abs(r["foreign"] + r["trust"] + r["dealer"] - r["total"]), 1,
                f"{code} 各項加總與合計不符:{r}")

    def test_skips_six_digit_etf(self):
        got = parse_tpex_chips(load("tpex_3insti.json"))
        self.assertNotIn("006201", got)
        self.assertIn("8027", got)


class TestCollect(unittest.TestCase):
    """collect() 的視窗推進行為。"""

    def _cached_10(self):
        """模擬已回補完成的 10 個交易日(2026/07/22~08/04)。"""
        ymds = ["20260722", "20260723", "20260724", "20260727", "20260728",
                "20260729", "20260730", "20260731", "20260803", "20260804"]
        return {y: {"2317": {"foreign": 1, "trust": 1, "dealer": 1, "total": 3}}
                for y in ymds}

    def test_still_fetches_new_day_when_cache_full(self):
        """
        回歸測試:快取已滿 10 天時,仍必須嘗試抓最新一天。
        第一版在此直接 break,導致視窗永遠凍結在首次回補的區間(CI 實測踩到)。
        """
        called = []

        def fake_fetch_day(d):
            called.append(d)
            return ({"2317": {"foreign": 9, "trust": 0, "dealer": 0, "total": 9}}, {})

        with patch("update_chips.fetch_day", fake_fetch_day):
            days = collect(["2317"], [], self._cached_10(),
                           need=10, today=date(2026, 8, 5))
        self.assertEqual(called, [date(2026, 8, 5)], "應只抓尚未快取的 8/5")
        self.assertIn("20260805", days)
        self.assertEqual(days["20260805"]["2317"]["foreign"], 9)

    def test_does_not_refetch_cached_days(self):
        """已快取的日期不可重抓,否則每天都會打 10 次 API。"""
        called = []

        def fake_fetch_day(d):
            called.append(d)
            return ({}, {})          # 假裝 8/5 尚未公布

        with patch("update_chips.fetch_day", fake_fetch_day):
            collect(["2317"], [], self._cached_10(), need=10, today=date(2026, 8, 5))
        self.assertEqual(called, [date(2026, 8, 5)])

    def test_no_negative_cache_so_unpublished_day_retries(self):
        """
        當日盤後資料未公布時抓不到,不可寫入負快取,
        否則會把真正的交易日永久誤標成非交易日。
        """
        with patch("update_chips.fetch_day", lambda d: ({}, {})):
            days = collect(["2317"], [], self._cached_10(),
                           need=10, today=date(2026, 8, 5))
        self.assertNotIn("20260805", days)

    def test_skips_weekends_without_api_calls(self):
        """週末不打 API。8/8 是週六、8/9 週日。"""
        called = []

        def fake_fetch_day(d):
            called.append(d)
            return ({}, {})

        with patch("update_chips.fetch_day", fake_fetch_day):
            collect(["2317"], [], self._cached_10(), need=10, today=date(2026, 8, 9))
        self.assertNotIn(date(2026, 8, 8), called)
        self.assertNotIn(date(2026, 8, 9), called)


class TestHelpers(unittest.TestCase):
    def test_roc_str(self):
        self.assertEqual(roc_str(date(2026, 8, 4)), "115/08/04")

    def test_lots_rounds_to_nearest(self):
        self.assertEqual(_lots(2130602), 2131)
        self.assertEqual(_lots(-1104101), -1104)


class TestRender(unittest.TestCase):
    def _days(self, n=10, code="2317"):
        return {f"202607{10 + i:02d}": {code: {"foreign": 100 * (i + 1), "trust": -10,
                                               "dealer": 5, "total": 100 * (i + 1) - 5}}
                for i in range(n)}

    def test_renders_two_tables(self):
        html = build_chips_html("2317", self._days())
        self.assertEqual(html.count("<table"), 2)
        self.assertIn("三大法人合計", html)

    def test_missing_code_says_chawu(self):
        """查無資料時要明說查無,不可顯示 0 讓人誤以為當天沒人交易。"""
        html = build_chips_html("9999", self._days())
        self.assertIn("查無", html)
        self.assertNotIn("<table", html)

    def test_caps_at_window(self):
        """即使 chips.json 存了更多天,呈現仍只取最近 10 個交易日。"""
        html = build_chips_html("2317", self._days(n=20))
        self.assertEqual(html.count('<tr><td>0'), 10)

    def test_uses_all_available_when_fewer_than_window(self):
        """回補未滿 10 日時照樣要能算,且分母須反映實際天數。"""
        html = build_chips_html("2317", self._days(n=4))
        self.assertIn("/4</td>", html)
        self.assertIn("共 4 個交易日", html)


if __name__ == "__main__":
    unittest.main()
