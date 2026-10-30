"""Проверки расчёта: python3 test_requirements.py."""

import json
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from main import load_recipe_composition
from requirements import calculate_requirements


def expect_error(function, *args):
    try:
        function(*args)
    except ValueError as error:
        assert str(error), "У ошибки должна быть причина"
    else:
        raise AssertionError("Ожидался отказ из-за некорректных данных")


def make_composition(*shares):
    return [
        {"recipe_id": 1, "component_id": index, "share_percent": share}
        for index, share in enumerate(shares, start=1)
    ]


def test_example():
    result = calculate_requirements("1000", make_composition("60", "25", "15"))
    assert result == [
        {"component_id": 1, "required_quantity_kg": Decimal("600")},
        {"component_id": 2, "required_quantity_kg": Decimal("250")},
        {"component_id": 3, "required_quantity_kg": Decimal("150")},
    ]


def test_exact_small_quantities():
    result = calculate_requirements("0.001", make_composition("0.0001", "99.9999"))
    assert result[0]["required_quantity_kg"] == Decimal("0.000000001")
    assert result[1]["required_quantity_kg"] == Decimal("0.000999999")
    assert sum(item["required_quantity_kg"] for item in result) == Decimal("0.001")


def test_decimal_sum():
    result = calculate_requirements("123.456", make_composition("33.3333", "33.3333", "33.3334"))
    assert sum(item["required_quantity_kg"] for item in result) == Decimal("123.456")


def test_upper_mass_limit():
    result = calculate_requirements("1000000", make_composition("100"))
    assert result[0]["required_quantity_kg"] == Decimal("1000000")


def test_invalid_mass():
    composition = make_composition("100")
    for mass in ("0", "-1", "NaN", "Infinity", "abc", "1000000.001", "0.0001", 1000.0):
        expect_error(calculate_requirements, mass, composition)


def test_invalid_shares():
    for share in ("0", "-1", "101", "NaN", "Infinity", "abc", "0.00001", 25.0):
        expect_error(calculate_requirements, "1000", make_composition(share))


def test_incorrect_sum_and_empty_composition():
    for composition in ([], make_composition("50", "49.9999"), make_composition("50", "50.0001")):
        expect_error(calculate_requirements, "1000", composition)


def test_input_unchanged():
    composition = make_composition("60", "25", "15")
    original = deepcopy(composition)
    calculate_requirements("1000", composition)
    assert composition == original
    composition[-1]["share_percent"] = "14"
    original = deepcopy(composition)
    expect_error(calculate_requirements, "1000", composition)
    assert composition == original


def test_local_precision():
    with localcontext() as context:
        context.prec = 4
        result = calculate_requirements("123.456", make_composition("0.0001", "99.9999"))
        assert result[0]["required_quantity_kg"] == Decimal("0.000123456")
        assert result[1]["required_quantity_kg"] == Decimal("123.455876544")
        assert context.prec == 4


def test_composition_keys():
    cases = [
        make_composition("60", "25", "15"),
        [{"recipe_id": 1, "component_id": 1, "share_percent": "50"}] * 2,
        [
            {"recipe_id": 1, "component_id": 1, "share_percent": "50"},
            {"recipe_id": 2, "component_id": 2, "share_percent": "50"},
        ],
        [{"recipe_id": 1, "component_id": True, "share_percent": "100"}],
        [{"recipe_id": 1, "share_percent": "100"}],
    ]
    with TemporaryDirectory() as directory:
        path = Path(directory) / "composition.json"
        for index, composition in enumerate(cases):
            path.write_text(json.dumps(composition), encoding="utf-8")
            if index == 0:
                assert load_recipe_composition(path) == composition
            else:
                expect_error(load_recipe_composition, path)


def run_tests():
    tests = (
        test_example,
        test_exact_small_quantities,
        test_decimal_sum,
        test_upper_mass_limit,
        test_invalid_mass,
        test_invalid_shares,
        test_incorrect_sum_and_empty_composition,
        test_input_unchanged,
        test_local_precision,
        test_composition_keys,
    )
    for test in tests:
        test()
    print(f"Проверки пройдены: {len(tests)}.")


if __name__ == "__main__":
    run_tests()
