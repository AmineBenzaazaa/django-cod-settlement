from decimal import Decimal

import pytest

from apps.settlement.services.csv_parser import (
    MAX_REPORTED_ERRORS,
    CsvValidationError,
    parse_remittance_csv,
    sha256_of,
)


def parse(text: str):
    return parse_remittance_csv(text.encode("utf-8"))


def test_parses_valid_file():
    lines = parse("tracking_number,amount_collected,fee\nspx-1,249.00,25\nSPX-2,0,10.50\n")
    assert [ln.tracking_number for ln in lines] == ["SPX-1", "SPX-2"]  # normalized upper-case
    assert lines[0].amount_collected == Decimal("249.00")
    assert lines[1].fee == Decimal("10.50")
    assert lines[1].row_number == 2


def test_header_is_flexible_and_bom_is_stripped():
    content = "﻿Fee, Amount Collected ,Tracking Number,extra\n5,50,ABC,x\n".encode()
    (line,) = parse_remittance_csv(content)
    assert line.tracking_number == "ABC"
    assert line.amount_collected == Decimal("50.00")


def test_blank_lines_are_ignored():
    lines = parse("tracking_number,amount_collected,fee\nA,1,0\n,,\n\nB,2,0\n")
    assert len(lines) == 2


@pytest.mark.parametrize(
    ("content", "message"),
    [
        ("", "File is empty."),
        ("tracking_number,fee\nA,1\n", "Missing required column(s): amount_collected."),
        ("tracking_number,amount_collected,fee\n", "File contains no data rows."),
    ],
)
def test_structural_errors(content, message):
    with pytest.raises(CsvValidationError) as exc:
        parse(content)
    assert exc.value.errors == [message]


def test_reports_every_bad_row():
    content = (
        "tracking_number,amount_collected,fee\n"
        "A,10.001,0\n"  # too many decimals
        ",5,0\n"  # empty tracking
        "B,-3,abc\n"  # negative + not a number
        "C,1,0\n"
        "C,1,0\n"  # duplicate
    )
    with pytest.raises(CsvValidationError) as exc:
        parse(content)
    errors = exc.value.errors
    assert len(errors) == 5
    assert errors[0].startswith("Row 1: amount_collected")
    assert "Row 2: tracking_number is empty" in errors
    assert any("duplicate tracking_number C" in e for e in errors)


def test_error_list_is_capped():
    rows = "\n".join(f"T{i},bad,0" for i in range(200))
    with pytest.raises(CsvValidationError) as exc:
        parse(f"tracking_number,amount_collected,fee\n{rows}\n")
    assert len(exc.value.errors) == MAX_REPORTED_ERRORS


def test_row_limit(monkeypatch):
    monkeypatch.setattr("apps.settlement.services.csv_parser.MAX_ROWS", 2)
    with pytest.raises(CsvValidationError, match="more than 2 rows"):
        parse("tracking_number,amount_collected,fee\nA,1,0\nB,1,0\nC,1,0\n")


def test_rejects_non_utf8():
    with pytest.raises(CsvValidationError, match="UTF-8"):
        parse_remittance_csv("tracking_number,amount_collected,fee\né,1,0\n".encode("latin-1"))


def test_sha256_is_content_based():
    assert sha256_of(b"abc") == sha256_of(b"abc")
    assert sha256_of(b"abc") != sha256_of(b"abd")
