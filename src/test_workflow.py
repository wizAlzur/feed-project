"""Проверки полного процесса: python3 test_workflow.py."""

import json
import subprocess
import sys
from copy import deepcopy
from decimal import Decimal, localcontext
from pathlib import Path
from tempfile import TemporaryDirectory

from main import save_workflow_data
from workflow import create_production_task, process_request, reserve_materials, select_recipe
from workflow_data import validate_workflow_data


def example():
    path = Path(__file__).parent / "examples" / "workflow.json"
    return json.loads(path.read_text(encoding="utf-8"))


def run(data, approve=False, fresh_stock=None, request_id=1):
    return process_request(data, request_id, "2026-10-31T10:00:00", approve, fresh_stock)


def expect_error(function, *args, contains=""):
    try:
        function(*args)
    except ValueError as error:
        assert str(error) and contains in str(error), str(error)
    else:
        raise AssertionError("Ожидалась ошибка данных")


def assert_reserve_totals(data, task_id):
    header = next(row for row in data["reservations"] if row["task_id"] == task_id)
    totals = {}
    for item in data["reservation_items"]:
        if item["reservation_id"] != header["reservation_id"]:
            continue
        lot = next(row for row in data["stock_lots"] if row["stock_lot_id"] == item["stock_lot_id"])
        assert lot["admission_status"] == "admitted"
        component = lot["component_id"]
        totals[component] = totals.get(component, Decimal("0")) + Decimal(item["reserved_quantity_kg"])
    expected = {row["component_id"]: Decimal(row["required_quantity_kg"])
                for row in data["task_items"] if row["task_id"] == task_id}
    assert totals == expected


def test_success_and_complete_reserve():
    source = example()
    original = deepcopy(source)
    result = run(source, True)
    data = result["data"]
    assert result["outcome"] == "ready"
    assert data["requests"][0]["status"] == "ready"
    assert data["requests"][0]["return_reason"] is None
    task = data["production_tasks"][0]
    assert task["recipe_id"] == 2 and task["status"] == "ready"
    assert task["approved_at"] == "2026-10-31T10:00:00"
    assert len(data["reservations"]) == 1
    assert [(row["stock_lot_id"], row["reserved_quantity_kg"]) for row in data["reservation_items"]] == [
        (101, Decimal("400")), (102, Decimal("200")), (201, Decimal("250")), (301, Decimal("150"))]
    assert_reserve_totals(data, task["task_id"])
    assert data["stock_lots"] == original["stock_lots"]
    assert source == original


def test_draft_requires_explicit_approval():
    draft = run(example())["data"]
    assert draft["requests"][0]["status"] == "processing"
    task = draft["production_tasks"][0]
    assert task["status"] == "draft" and task["approved_at"] is None
    assert draft["reservations"] == [] and draft["reservation_items"] == []
    expect_error(reserve_materials, draft, task["task_id"], "2026-10-31T10:00:00", contains="утверждено")
    ready = run(draft, True)
    assert ready["outcome"] == "ready"
    assert len(ready["data"]["production_tasks"]) == 1


def test_invalid_request_collects_reasons():
    data = example()
    data["requests"][0].update(product_id=99, batch_mass_kg="-1", due_date="2026-10-30")
    original = deepcopy(data)
    result = run(data, True)
    reason = result["data"]["requests"][0]["return_reason"]
    assert result["outcome"] == "returned"
    assert "продукция" in reason and "Масса" in reason and "Срок" in reason
    assert not result["data"]["production_tasks"] and not result["data"]["reservations"]
    assert data == original


def test_empty_request_values():
    data = example()
    data["requests"][0].update(product_id=None, batch_mass_kg=None, due_date=None)
    result = run(data)
    assert result["outcome"] == "returned"
    assert result["data"]["requests"][0]["return_reason"].count(".") >= 3


def test_due_date_boundary():
    data = example()
    data["requests"][0]["due_date"] = "2026-10-31"
    assert run(data, True)["outcome"] == "ready"


def test_recipe_dates_approval_and_product():
    recipes = example()["recipe_versions"]
    assert select_recipe(recipes, 1, "2026-10-30")["recipe_id"] == 1
    assert select_recipe(recipes, 1, "2026-10-31")["recipe_id"] == 2
    assert select_recipe(recipes, 1, "2027-01-01")["recipe_id"] == 2
    expect_error(select_recipe, recipes, 2, "2026-10-31", contains="Нет")
    expect_error(select_recipe, recipes, 1, "2025-12-31", contains="Нет")
    recipes[1]["approval_status"] = "draft"
    expect_error(select_recipe, recipes, 1, "2026-10-31", contains="Нет")


def test_missing_and_overlapping_recipe():
    for overlap in (False, True):
        data = example()
        data["recipe_versions"][1]["approval_status"] = "draft"
        if overlap:
            data["recipe_versions"][0]["valid_to"] = None
            data["recipe_versions"][2]["approval_status"] = "approved"
        result = run(data, True)
        assert result["outcome"] == "returned"
        reason = result["data"]["requests"][0]["return_reason"]
        assert ("несколько" if overlap else "Нет") in reason
        assert not result["data"]["production_tasks"]


def test_invalid_composition_sum_and_empty():
    for empty in (False, True):
        data = example()
        if empty:
            data["recipe_composition"] = [row for row in data["recipe_composition"] if row["recipe_id"] != 2]
        else:
            data["recipe_composition"][3]["share_percent"] = "59"
        result = run(data, True)
        assert result["outcome"] == "returned"
        assert not result["data"]["reservations"]


def test_first_check_reports_all_deficits():
    data = example()
    data["stock_lots"][0]["admission_status"] = "pending"
    data["stock_lots"][1]["admission_status"] = "blocked"
    data["stock_lots"][3]["quantity_kg"] = "200"
    result = run(data, True)
    assert result["outcome"] == "returned"
    assert "600 кг" in result["data"]["requests"][0]["return_reason"]
    assert "50 кг" in result["data"]["requests"][0]["return_reason"]
    assert not result["data"]["production_tasks"] and not result["data"]["reservation_items"]


def test_second_check_returns_without_partial_reserve():
    data = example()
    fresh = json.loads((Path(__file__).parent / "examples" / "stock_after_approval.json").read_text(encoding="utf-8"))
    original, original_fresh = deepcopy(data), deepcopy(fresh)
    result = run(data, True, fresh)
    changed = result["data"]
    assert result["outcome"] == "returned"
    assert "100 кг" in changed["requests"][0]["return_reason"]
    assert changed["production_tasks"][0]["status"] == "returned"
    assert changed["production_tasks"][0]["approved_at"] is None
    assert changed["reservations"] == [] and changed["reservation_items"] == []
    assert changed["stock_lots"] == fresh["stock_lots"]
    assert data == original and fresh == original_fresh
    fresh["stock_lots"][1]["quantity_kg"] = "300"
    assert run(data, True, fresh)["outcome"] == "ready"


def test_failed_reserve_keeps_existing_reserves():
    data = run(example(), True)["data"]
    data["requests"].append({"request_id": 2, "product_id": 1, "batch_mass_kg": "100",
                             "due_date": "2026-11-03", "status": "new", "return_reason": None})
    result = run(data, True, request_id=2)
    assert result["outcome"] == "returned"  # Третий компонент занят целиком.
    assert result["data"]["reservations"] == data["reservations"]
    assert result["data"]["reservation_items"] == data["reservation_items"]
    assert result["data"]["requests"][0]["status"] == "ready"


def test_other_reserves_and_zero_free_lots():
    data = example()
    for lot in data["stock_lots"]:
        if lot["component_id"] == 3:
            lot["quantity_kg"] = "300"
    data = run(data, True)["data"]
    data["requests"].append({"request_id": 2, "product_id": 1, "batch_mass_kg": "100",
                             "due_date": "2026-11-03", "status": "new", "return_reason": None})
    result = run(data, True, request_id=2)
    assert result["outcome"] == "ready"
    assert len(result["data"]["reservations"]) == 2
    second = result["data"]["reservations"][1]["reservation_id"]
    positions = [row for row in result["data"]["reservation_items"] if row["reservation_id"] == second]
    assert not any(row["stock_lot_id"] in (101, 103) for row in positions)
    assert_reserve_totals(result["data"], 2)
    assert result["data"]["stock_lots"] == data["stock_lots"]


def test_rerun_does_not_duplicate_or_change_reserve():
    ready = run(example(), True)["data"]
    for approve in (False, True):
        result = run(ready, approve)
        assert result["outcome"] == "already_ready"
        assert result["data"] == ready
        assert result["data"] is not ready
    task = ready["production_tasks"][0]
    expect_error(create_production_task, ready, 1, 2, [], "2026-10-31T10:00:00", contains="отмена")


def test_ready_request_changes_rejected():
    for change in ("mass", "items", "reserve"):
        data = run(example(), True)["data"]
        if change == "mass":
            data["requests"][0]["batch_mass_kg"] = "500"
        elif change == "items":
            data["task_items"][0]["required_quantity_kg"] = "500"
        else:
            data["reservation_items"][0]["reserved_quantity_kg"] = "300"
        original = deepcopy(data)
        expect_error(run, data, True)
        assert data == original


def test_returned_request_recalculated_and_reapproved():
    data = run(example())["data"]
    data["stock_lots"][4]["quantity_kg"] = "100"
    returned = run(data, True)["data"]
    assert returned["production_tasks"][0]["status"] == "returned"
    returned["requests"][0]["batch_mass_kg"] = "500"
    draft = run(returned)["data"]
    assert draft["requests"][0]["return_reason"] is None
    assert draft["production_tasks"][0]["approved_at"] is None
    assert [row["required_quantity_kg"] for row in draft["task_items"]] == [Decimal("300"), Decimal("125"), Decimal("75")]
    assert draft["reservations"] == []
    ready = run(draft, True)["data"]
    assert len(ready["production_tasks"]) == 1
    assert_reserve_totals(ready, 1)


def test_cancelled_header_reused():
    data = run(example(), True)["data"]
    data["reservations"][0]["status"] = "cancelled"
    data["production_tasks"][0].update(status="returned", approved_at=None)
    data["requests"][0].update(status="returned", return_reason="Резерв отменён вне данного процесса.", batch_mass_kg="500")
    result = run(data, True)["data"]
    assert len(result["reservations"]) == 1 and result["reservations"][0]["reservation_id"] == 1
    assert result["reservations"][0]["status"] == "active"
    assert_reserve_totals(result, 1)
    assert len(result["reservation_items"]) == 3


def test_exact_micro_quantities_and_local_context():
    data = example()
    data["requests"][0]["batch_mass_kg"] = "0.001"
    data["recipe_composition"] = [row for row in data["recipe_composition"] if row["recipe_id"] != 2]
    data["recipe_composition"].extend([
        {"recipe_id": 2, "component_id": 1, "share_percent": "0.0001"},
        {"recipe_id": 2, "component_id": 2, "share_percent": "99.9999"}])
    with localcontext() as context:
        context.prec = 4
        result = run(data, True)["data"]
        assert result["reservation_items"][0]["reserved_quantity_kg"] == Decimal("0.000000001")
        assert result["reservation_items"][1]["reserved_quantity_kg"] == Decimal("0.000999999")
        assert context.prec == 4


def test_data_keys_links_and_states():
    data = run(example(), True)["data"]
    for table in data:
        bad = deepcopy(data)
        bad[table].append(bad[table][0].copy())
        expect_error(validate_workflow_data, bad, contains="повтор ключа")
    cases = (
        ("recipe_versions", "product_id", 99), ("recipe_composition", "component_id", 99),
        ("production_tasks", "request_id", 99), ("task_items", "task_id", 99),
        ("stock_lots", "component_id", 99), ("reservations", "task_id", 99),
        ("reservation_items", "stock_lot_id", 99), ("production_tasks", "status", "unknown"),
        ("production_tasks", "approved_at", None), ("recipe_versions", "valid_to", "2020-01-01"),
    )
    for table, field, value in cases:
        bad = deepcopy(data)
        bad[table][0][field] = value
        expect_error(validate_workflow_data, bad)
    for table in data:
        bad = deepcopy(data)
        bad[table][0]["unknown"] = "field"
        expect_error(validate_workflow_data, bad)


def test_overreserve_and_blocked_active_reserve_rejected():
    for blocked in (False, True):
        data = run(example(), True)["data"]
        if blocked:
            data["stock_lots"][0]["admission_status"] = "blocked"
        else:
            data["stock_lots"][0]["quantity_kg"] = "399"
        expect_error(run, data, True)


def test_invalid_process_parameters():
    data = example()
    for request_id in (True, 0, -1, "1", 99):
        expect_error(process_request, data, request_id, "2026-10-31T10:00:00")
    for value in (None, "вчера", "2026-10-31", "2026-02-30T10:00:00"):
        expect_error(process_request, data, 1, value)
    expect_error(process_request, data, 1, "2026-10-31T10:00:00", "yes")
    fresh = {name: data[name] for name in ("stock_lots", "reservations", "reservation_items")}
    expect_error(run, data, False, fresh)
    bad = deepcopy(fresh)
    bad["stock_lots"][0]["component_id"] = 99
    original = deepcopy(data)
    expect_error(run, data, True, bad)
    assert data == original
    assert process_request(data, 1, "2026-10-31T10:00:00Z")["outcome"] == "draft"


def test_json_round_trip():
    ready = run(example(), True)["data"]
    with TemporaryDirectory() as directory:
        path = Path(directory) / "result.json"
        save_workflow_data(path, ready)
        reloaded = json.loads(path.read_text(encoding="utf-8"))
        assert run(reloaded)["outcome"] == "already_ready"
        assert len(list(Path(directory).iterdir())) == 1


def test_cli_and_output_protection():
    script = Path(__file__).parent / "main.py"
    with TemporaryDirectory() as directory:
        source = Path(directory) / "input.json"
        output = Path(directory) / "result.json"
        original = json.dumps(example(), ensure_ascii=False)
        source.write_text(original, encoding="utf-8")
        command = [sys.executable, str(script), "--data-file", str(source), "--request-id", "1",
                   "--processing-at", "2026-10-31T10:00:00"]
        success = subprocess.run(command + ["--approve", "--output", str(output)], capture_output=True, text=True)
        assert success.returncode == 0, success.stderr
        assert "сырьё зарезервировано" in success.stdout and not success.stderr
        assert run(json.loads(output.read_text(encoding="utf-8")))["outcome"] == "already_ready"
        assert source.read_text(encoding="utf-8") == original
        protected = subprocess.run(command + ["--output", str(source)], capture_output=True, text=True)
        assert protected.returncode == 2 and not protected.stdout
        assert source.read_text(encoding="utf-8") == original
        source.write_text("{", encoding="utf-8")
        saved = output.read_bytes()
        invalid = subprocess.run(command + ["--output", str(output)], capture_output=True, text=True)
        assert invalid.returncode == 1 and not invalid.stdout and invalid.stderr.startswith("Ошибка:")
        assert output.read_bytes() == saved


def run_tests():
    tests = (test_success_and_complete_reserve, test_draft_requires_explicit_approval,
             test_invalid_request_collects_reasons, test_empty_request_values, test_due_date_boundary,
             test_recipe_dates_approval_and_product, test_missing_and_overlapping_recipe,
             test_invalid_composition_sum_and_empty, test_first_check_reports_all_deficits,
             test_second_check_returns_without_partial_reserve, test_failed_reserve_keeps_existing_reserves,
             test_other_reserves_and_zero_free_lots, test_rerun_does_not_duplicate_or_change_reserve,
             test_ready_request_changes_rejected, test_returned_request_recalculated_and_reapproved,
             test_cancelled_header_reused, test_exact_micro_quantities_and_local_context,
             test_data_keys_links_and_states, test_overreserve_and_blocked_active_reserve_rejected,
             test_invalid_process_parameters, test_json_round_trip, test_cli_and_output_protection)
    for test in tests:
        test()
    print(f"Проверки полного процесса пройдены: {len(tests)}.")


if __name__ == "__main__":
    run_tests()
