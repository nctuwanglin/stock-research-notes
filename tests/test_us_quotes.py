# -*- coding: utf-8 -*-
"""
美股報價解析固定測資。
Yahoo/Nasdaq 改格式或改風控時這裡會先紅,避免靜默解析失敗
(歷史教訓:twse-disposition 曾解析 0 筆照樣回報 success)。
執行:python3 -m unittest discover -s tests -q
"""
import contextlib
import io
import sys
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).parent.parent / "scripts"))
from update_freshness import (          # noqa: E402
    parse_yahoo_chart, parse_nasdaq_info, fetch_us_closes, us_errors,
)

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


class TestYahooChart(unittest.TestCase):
    def test_takes_last_completed_bar(self):
        """最後一根是當日進行中的盤中棒,必須被日期規則排除,取前一根收盤。"""
        got = parse_yahoo_chart(load("yahoo_mu.json"), date(2026, 7, 27), "MU")
        self.assertEqual(got, ("20260724", 920.9500122070312))

    def test_skips_null_close(self):
        """close 為 null 的日 K 要跳過,繼續往前找。"""
        got = parse_yahoo_chart(load("yahoo_null_close.json"), date(2026, 7, 27), "MU")
        self.assertEqual(got, ("20260723", 990.2100219726562))

    def test_accepts_yesterday_when_ref_moves_on(self):
        """美東日期前進到 7/28 後,7/27 那根就成為可用的完整收盤。"""
        got = parse_yahoo_chart(load("yahoo_mu.json"), date(2026, 7, 28), "MU")
        self.assertEqual(got, ("20260727", 870.1900024414062))

    def test_ratelimited_body_returns_none(self):
        """被 429 時回的是純文字而非 JSON,必須回 None 而不是拋例外。"""
        self.assertIsNone(
            parse_yahoo_chart(load("yahoo_ratelimited.txt"), date(2026, 7, 27), "MU"))

    def test_all_bars_too_recent_returns_none(self):
        got = parse_yahoo_chart(load("yahoo_mu.json"), date(2026, 7, 23), "MU")
        self.assertIsNone(got)

    def test_mismatched_symbol_returns_none(self):
        """回傳的 meta.symbol 與請求的代號不符(重新分配/拼錯)必須拒收。"""
        got = parse_yahoo_chart(load("yahoo_wrong_symbol.json"), date(2026, 7, 27), "MU")
        self.assertIsNone(got)

    def test_null_symbol_returns_none(self):
        """meta.symbol 為 JSON null 時不能讓 .upper() 拋例外,必須回 None。"""
        got = parse_yahoo_chart(load("yahoo_null_symbol.json"), date(2026, 7, 27), "MU")
        self.assertIsNone(got)

    def test_length_mismatch_returns_none(self):
        """timestamp 3 筆但 close 只有 2 筆,絕不能硬配對出一個(日期,收盤價),必須回 None。"""
        got = parse_yahoo_chart(load("yahoo_length_mismatch.json"), date(2026, 7, 27), "MU")
        self.assertIsNone(got)


class TestNasdaqInfo(unittest.TestCase):
    def test_rejects_same_day_intraday(self):
        """lastTradeTimestamp 為當前美東日期 = 盤中價,必須拒收。"""
        self.assertIsNone(
            parse_nasdaq_info(load("nasdaq_mu.json"), date(2026, 7, 27), "MU"))

    def test_accepts_completed_session(self):
        got = parse_nasdaq_info(load("nasdaq_mu.json"), date(2026, 7, 28), "MU")
        self.assertEqual(got, ("20260727", 874.19))

    def test_strips_dollar_and_comma(self):
        got = parse_nasdaq_info(load("nasdaq_comma.json"), date(2026, 7, 27), "SNDK")
        self.assertEqual(got, ("20260724", 1270.5))

    def test_null_data_returns_none(self):
        """HTTP 200 但 data 為 null(code 3004),必須回 None 而不是拋例外。"""
        self.assertIsNone(
            parse_nasdaq_info(load("nasdaq_error.json"), date(2026, 7, 28), "SNDK"))

    def test_mismatched_symbol_returns_none(self):
        """回傳的 data.symbol 與請求的代號不符,必須拒收。"""
        got = parse_nasdaq_info(load("nasdaq_wrong_symbol.json"), date(2026, 7, 28), "MU")
        self.assertIsNone(got)


class TestUsErrors(unittest.TestCase):
    def test_all_fetched_no_errors(self):
        self.assertEqual(us_errors(["MU", "SNDK"], {"MU": ("20260727", 900.0),
                                                      "SNDK": ("20260727", 1270.0)}), [])

    def test_all_missing_is_total_outage(self):
        self.assertEqual(us_errors(["MU", "SNDK"], {}), ["us_all_failed"])

    def test_some_missing_names_them(self):
        got = us_errors(["MU", "SNDK"], {"MU": ("20260727", 900.0)})
        self.assertEqual(got, ["us_missing:SNDK"])


class TestFetchUsCloses(unittest.TestCase):
    """固定 us_today() 回傳值,讓測資日期規則的結果不受實際執行時間影響。

    每個案例都故意觸發至少一段備援失敗,fetch_us_closes 會照設計印 WARN 到
    stderr(供 CI log 除錯用)——測試裡吞掉它,保持測試輸出乾淨,不代表production
    行為改變。
    """

    @staticmethod
    def hosts_requested(mock_fetch):
        return [c.args[0].split("/")[2] for c in mock_fetch.call_args_list]

    @patch("update_freshness.us_today", return_value=date(2026, 7, 27))
    @patch("update_freshness.fetch")
    def test_first_hop_fails_second_succeeds(self, mock_fetch, mock_today):
        mock_fetch.side_effect = [Exception("boom"), load("yahoo_mu.json")]
        with contextlib.redirect_stderr(io.StringIO()):
            got = fetch_us_closes(["MU"])
        self.assertEqual(got, {"MU": ("20260724", 920.9500122070312)})
        self.assertEqual(self.hosts_requested(mock_fetch),
                          ["query1.finance.yahoo.com", "query2.finance.yahoo.com"])

    @patch("update_freshness.us_today", return_value=date(2026, 7, 28))
    @patch("update_freshness.fetch")
    def test_first_hop_fails_second_no_usable_bar_third_succeeds(self, mock_fetch, mock_today):
        mock_fetch.side_effect = [
            Exception("boom"),
            load("yahoo_ratelimited.txt"),
            load("nasdaq_mu.json"),
        ]
        with contextlib.redirect_stderr(io.StringIO()):
            got = fetch_us_closes(["MU"])
        self.assertEqual(got, {"MU": ("20260727", 874.19)})
        self.assertEqual(self.hosts_requested(mock_fetch),
                          ["query1.finance.yahoo.com", "query2.finance.yahoo.com",
                           "api.nasdaq.com"])

    @patch("update_freshness.us_today", return_value=date(2026, 7, 27))
    @patch("update_freshness.fetch")
    def test_all_hops_fail_code_absent(self, mock_fetch, mock_today):
        mock_fetch.side_effect = [Exception("a"), Exception("b"), Exception("c")]
        with contextlib.redirect_stderr(io.StringIO()):
            got = fetch_us_closes(["MU"])
        self.assertEqual(got, {})
        self.assertEqual(self.hosts_requested(mock_fetch),
                          ["query1.finance.yahoo.com", "query2.finance.yahoo.com",
                           "api.nasdaq.com"])


if __name__ == "__main__":
    unittest.main()
