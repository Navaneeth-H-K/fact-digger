"""Period parsing: Indian fiscal years, quarters, halves, cumulative spans, calendar years, as-of dates."""

from datetime import date

import pytest

from fkl.normalize import Period, parse_period, period_key

FY = "fy"


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("FY24", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("FY 24", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("FY2024", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("FY 2023-24", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("2023-24", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("2023/24", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("FY2023/24", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("2023-2024", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("Fiscal 2021", Period(date(2020, 4, 1), date(2021, 3, 31), FY)),
        ("financial year ended March 31, 2024", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("year ended 31 March 2024", Period(date(2023, 4, 1), date(2024, 3, 31), FY)),
        ("Q1 FY25", Period(date(2024, 4, 1), date(2024, 6, 30), "quarter")),
        ("Q1FY25", Period(date(2024, 4, 1), date(2024, 6, 30), "quarter")),
        ("Q4 FY24", Period(date(2024, 1, 1), date(2024, 3, 31), "quarter")),
        ("Q3:2024-25", Period(date(2024, 10, 1), date(2024, 12, 31), "quarter")),
        ("Q2 of FY25", Period(date(2024, 7, 1), date(2024, 9, 30), "quarter")),
        ("H1 FY25", Period(date(2024, 4, 1), date(2024, 9, 30), "half")),
        ("H1:2024-25", Period(date(2024, 4, 1), date(2024, 9, 30), "half")),
        ("9MFY25", Period(date(2024, 4, 1), date(2024, 12, 31), "range")),
        ("April-December 2024", Period(date(2024, 4, 1), date(2024, 12, 31), "range")),
        ("April – December 2024", Period(date(2024, 4, 1), date(2024, 12, 31), "range")),
        ("Apr-Dec 2024", Period(date(2024, 4, 1), date(2024, 12, 31), "range")),
        (
            "nine months ended December 31, 2021",
            Period(date(2021, 4, 1), date(2021, 12, 31), "range"),
        ),
        ("CY2024", Period(date(2024, 1, 1), date(2024, 12, 31), "calendar")),
        ("2024", Period(date(2024, 1, 1), date(2024, 12, 31), "calendar")),
        ("March 2025", Period(date(2025, 3, 1), date(2025, 3, 31), "month")),
        ("September 2024", Period(date(2024, 9, 1), date(2024, 9, 30), "month")),
        ("as on 31 March 2025", Period(date(2025, 3, 31), date(2025, 3, 31), "asof")),
        ("as at March 31, 2025", Period(date(2025, 3, 31), date(2025, 3, 31), "asof")),
        ("end-March 2025", Period(date(2025, 3, 31), date(2025, 3, 31), "asof")),
        ("as of December 31, 2021", Period(date(2021, 12, 31), date(2021, 12, 31), "asof")),
        ("as on 31.03.2025", Period(date(2025, 3, 31), date(2025, 3, 31), "asof")),
        ("2025Q2", Period(date(2025, 4, 1), date(2025, 6, 30), "quarter")),  # IMF calendar quarter
    ],
)
def test_parse_period_with_april_fiscal_year(text: str, expected: Period) -> None:
    assert parse_period(text, fy_start_month=4) == expected


def test_parse_period_respects_january_fiscal_year_start() -> None:
    assert parse_period("FY24", fy_start_month=1) == Period(
        date(2024, 1, 1), date(2024, 12, 31), FY
    )
    assert parse_period("Q2 FY24", fy_start_month=1) == Period(
        date(2024, 4, 1), date(2024, 6, 30), "quarter"
    )


@pytest.mark.parametrize("text", ["", "recent months", "the last three years", "n.a.", "Table 2.1"])
def test_parse_period_returns_none_when_unparseable(text: str) -> None:
    assert parse_period(text, fy_start_month=4) is None


def test_period_key_is_stable_string_and_unknown_for_none() -> None:
    period = Period(date(2023, 4, 1), date(2024, 3, 31), FY)
    assert period_key(period) == "2023-04-01:2024-03-31"
    assert period_key(None) == "unknown"
