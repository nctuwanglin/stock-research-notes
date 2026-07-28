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
