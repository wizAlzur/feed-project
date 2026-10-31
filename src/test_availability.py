"""Проверки складских запасов и резервов: python3 test_availability.py."""

from copy import deepcopy
from decimal import Decimal, localcontext

from availability import check_availability
from main import format_quantity
from requirements import calculate_requirements


def expect_error(*args, contains=""):
    try:
        check_availability(*args)
    except ValueError as error:
        assert str(error) and contains in str(error), str(error)
    else:
        raise AssertionError("Ожидался отказ из-за некорректных данных")


def lot(identifier, component, quantity, status="admitted"):
    return {"stock_lot_id": identifier, "component_id": component,
            "quantity_kg": quantity, "admission_status": status}


def header(identifier, task, status="active"):
    return {"reservation_id": identifier, "task_id": task,
            "created_at": "2026-10-30T10:00:00", "status": status}


def item(reservation, stock_lot, quantity):
    return {"reservation_id": reservation, "stock_lot_id": stock_lot,
            "reserved_quantity_kg": quantity}


def need(component, quantity):
    return {"component_id": component, "required_quantity_kg": quantity}


def sample_data():
    return (
        [need(1, "100"), need(2, "40")],
        [lot(101, 1, "90"), lot(102, 1, "60"), lot(103, 1, "100", "blocked"),
         lot(201, 2, "40"), lot(202, 2, "100", "pending")],
        [header(11, 21), header(12, 22), header(13, 23, "cancelled")],
        [item(11, 101, "30"), item(12, 102, "40"), item(13, 101, "50")],
    )


def test_reserves_and_admission():
    result = check_availability(*sample_data())
    assert result == {"can_fulfill": False, "components": [
        {"component_id": 1, "required_quantity_kg": Decimal("100"),
         "available_quantity_kg": Decimal("80"), "shortage_quantity_kg": Decimal("20")},
        {"component_id": 2, "required_quantity_kg": Decimal("40"),
         "available_quantity_kg": Decimal("40"), "shortage_quantity_kg": Decimal("0")},
    ]}


def test_own_reserve_excluded():
    result = check_availability(*sample_data(), current_task_id=21)
    assert result["can_fulfill"] is True
    assert result["components"][0]["available_quantity_kg"] == Decimal("110")


def test_all_deficits_collected():
    needs, lots, headers, items = sample_data()
    needs[1]["required_quantity_kg"] = "100"
    result = check_availability(needs, lots, headers, items)
    assert [row["shortage_quantity_kg"] for row in result["components"]] == [Decimal("20"), Decimal("60")]


def test_success_does_not_reset_failure():
    needs, lots, headers, items = sample_data()
    needs.insert(0, need(3, "10")); lots.append(lot(301, 3, "10"))
    result = check_availability(needs, lots, headers, items)
    assert result["can_fulfill"] is False
    assert [row["shortage_quantity_kg"] for row in result["components"]] == [Decimal("0"), Decimal("20"), Decimal("0")]


def test_empty_stock_and_missing_component():
    result = check_availability([need(1, "25"), need(2, "10")], [], [], [])
    assert result["can_fulfill"] is False
    assert [row["shortage_quantity_kg"] for row in result["components"]] == [Decimal("25"), Decimal("10")]
    result = check_availability([need(2, "10")], [lot(101, 1, "100")], [], [])
    assert result["components"][0]["available_quantity_kg"] == Decimal("0")


def test_zero_stock():
    result = check_availability([need(1, "1")], [lot(101, 1, "0")], [], [])
    assert result["components"][0]["shortage_quantity_kg"] == Decimal("1")


def test_exact_small_quantities():
    result = check_availability([need(1, "0.000000001")], [lot(101, 1, "0.001")],
                                [header(11, 21)], [item(11, 101, "0.000999999")])
    assert result["can_fulfill"] is True
    assert result["components"][0]["available_quantity_kg"] == Decimal("0.000000001")
    assert format_quantity(result["components"][0]["available_quantity_kg"]) == "0.000000001"


def test_integration_with_requirements():
    composition = [{"recipe_id": 1, "component_id": 1, "share_percent": "0.0001"},
                   {"recipe_id": 1, "component_id": 2, "share_percent": "99.9999"}]
    needs = calculate_requirements("0.001", composition)
    result = check_availability(needs, [lot(101, 1, "0.001"), lot(201, 2, "0.001")], [], [])
    assert result["can_fulfill"] is True
    assert result["components"][0]["required_quantity_kg"] == Decimal("0.000000001")


def test_local_precision():
    with localcontext() as context:
        context.prec = 4
        result = check_availability([need(1, "0.000000001")], [lot(101, 1, "0.001")],
                                    [header(11, 21)], [item(11, 101, "0.000999999")])
        assert result["can_fulfill"] is True
        assert result["components"][0]["available_quantity_kg"] == Decimal("0.000000001")
        assert context.prec == 4


def test_over_reservation_including_own():
    expect_error([need(1, "1")], [lot(101, 1, "90")], [header(11, 21), header(12, 22)],
                 [item(11, 101, "70"), item(12, 101, "40")], 21, contains="превышают")


def test_over_reservation_of_unused_blocked_lot():
    expect_error([need(1, "1")], [lot(101, 1, "10"), lot(901, 9, "1", "blocked")],
                 [header(11, 21)], [item(11, 901, "2")], contains="превышают")


def test_cancelled_reserve_ignored():
    result = check_availability([need(1, "90")], [lot(101, 1, "90")],
                                [header(11, 21, "cancelled")], [item(11, 101, "500")])
    assert result["can_fulfill"] is True


def test_missing_links():
    for status in ("active", "cancelled"):
        expect_error([need(1, "1")], [lot(101, 1, "10")], [header(11, 21, status)],
                     [item(99, 101, "1")], contains="связ")
        expect_error([need(1, "1")], [lot(101, 1, "10")], [header(11, 21, status)],
                     [item(11, 999, "1")], contains="связ")


def test_empty_reservation_rejected():
    for status in ("active", "cancelled"):
        expect_error([need(1, "1")], [lot(101, 1, "10")],
                     [header(11, 21, status)], [], contains="позиции")


def test_available_sum_above_one_lot_limit():
    result = check_availability([need(1, "1000000")],
                                [lot(101, 1, "1000000"), lot(102, 1, "1000000")], [], [])
    assert result["can_fulfill"] is True
    assert result["components"][0]["available_quantity_kg"] == Decimal("2000000")


def test_duplicate_keys():
    for table in range(4):
        data = list(sample_data())
        data[table].append(data[table][0].copy())
        expect_error(*data, contains="повтор ключа")
    needs, lots, headers, items = sample_data()
    headers.append(header(99, 21, "cancelled"))
    expect_error(needs, lots, headers, items, contains="повтор ключа")


def test_invalid_quantities():
    for value in ("NaN", "Infinity", "-1", "abc", "1000000.001", "0.0000000001", 1.0):
        data = list(sample_data()); data[0][0]["required_quantity_kg"] = value
        expect_error(*data)
        data = list(sample_data()); data[3][0]["reserved_quantity_kg"] = value
        expect_error(*data)
    for value in ("NaN", "Infinity", "-1", "abc", "1000000.001", "0.0001", 1.0):
        data = list(sample_data()); data[1][0]["quantity_kg"] = value
        expect_error(*data)
    for table, field in ((0,"required_quantity_kg"),(3,"reserved_quantity_kg")):
        data = list(sample_data()); data[table][0][field] = "0"
        expect_error(*data)


def test_invalid_ids():
    for table, field in ((0,"component_id"),(1,"stock_lot_id"),(1,"component_id"),
                         (2,"reservation_id"),(2,"task_id"),(3,"reservation_id"),(3,"stock_lot_id")):
        for value in (True, 0, -1, "1"):
            data = list(sample_data()); data[table][0][field] = value
            expect_error(*data)
    for value in (True, 0, -1, "21"):
        expect_error(*sample_data(), value)


def test_invalid_record_format():
    for table in range(4):
        data = list(sample_data()); data[table][0] = "ошибка"
        expect_error(*data)
        data = list(sample_data()); data[table][0].pop(next(iter(data[table][0])))
        expect_error(*data)
        data = list(sample_data()); data[table][0]["unknown"] = "ошибка"
        expect_error(*data)


def test_invalid_containers():
    for table in range(4):
        data = list(sample_data()); data[table] = None
        expect_error(*data)
    expect_error([], [], [], [])


def test_invalid_status_and_date():
    for table, field, value in ((1,"admission_status","unknown"),(2,"status","unknown"),
                                 (2,"created_at","вчера"),(2,"created_at",None)):
        data = list(sample_data()); data[table][0][field] = value
        expect_error(*data)
    needs, lots, headers, items = sample_data()
    headers[0]["created_at"] = "2026-10-30T10:00:00Z"
    assert check_availability(needs, lots, headers, items)["can_fulfill"] is False


def test_input_unchanged():
    data = sample_data(); original = deepcopy(data)
    check_availability(*data)
    assert data == original
    check_availability(*data, current_task_id=21)
    assert data == original
    data[3][0]["reserved_quantity_kg"] = "91"; original = deepcopy(data)
    expect_error(*data)
    assert data == original


def run_tests():
    tests = (test_reserves_and_admission, test_own_reserve_excluded, test_all_deficits_collected,
             test_success_does_not_reset_failure, test_empty_stock_and_missing_component, test_zero_stock,
             test_exact_small_quantities, test_integration_with_requirements, test_local_precision,
             test_over_reservation_including_own, test_over_reservation_of_unused_blocked_lot,
             test_cancelled_reserve_ignored, test_missing_links, test_empty_reservation_rejected,
             test_available_sum_above_one_lot_limit, test_duplicate_keys, test_invalid_quantities,
             test_invalid_ids, test_invalid_record_format, test_invalid_containers,
             test_invalid_status_and_date, test_input_unchanged)
    for test in tests:
        test()
    print(f"Проверки обеспеченности пройдены: {len(tests)}.")


if __name__ == "__main__":
    run_tests()
