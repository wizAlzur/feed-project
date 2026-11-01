"""Проверка записей десяти сущностей логической модели."""

from datetime import date, datetime
from decimal import Decimal, DecimalException, Inexact, localcontext

from availability import check_unique, find_record, read_quantity, require_fields, require_id
from requirements import calculate_requirements, parse_positive_decimal


def read_date(value, name):
    if not isinstance(value, str):
        raise ValueError(f"{name}: нужна дата в формате YYYY-MM-DD.")
    try:
        result = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"{name}: некорректная дата.") from None
    if result.isoformat() != value:
        raise ValueError(f"{name}: используйте формат YYYY-MM-DD.")
    return result


def read_timestamp(value, name):
    if not isinstance(value, str) or "T" not in value:
        raise ValueError(f"{name}: нужны дата и время ISO 8601, например 2026-10-31T10:00:00.")
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise ValueError(f"{name}: некорректные дата и время.") from None


def task_requirements(data, task_id):
    return [
        {"component_id": row["component_id"],
         "required_quantity_kg": read_quantity(row["required_quantity_kg"], "Позиция задания", 9)}
        for row in data["task_items"] if row["task_id"] == task_id
    ]


def validate_workflow_data(data):
    """Проверить структуру, ключи, связи и согласованность готовых заданий."""
    tables = (
        ("products", ("product_id", "name"), ("product_id",)),
        ("recipe_versions", ("recipe_id", "product_id", "version_number", "approval_status", "valid_from", "valid_to"), ("recipe_id",)),
        ("components", ("component_id", "name"), ("component_id",)),
        ("recipe_composition", ("recipe_id", "component_id", "share_percent"), ("recipe_id", "component_id")),
        ("requests", ("request_id", "product_id", "batch_mass_kg", "due_date", "status", "return_reason"), ("request_id",)),
        ("production_tasks", ("task_id", "request_id", "recipe_id", "created_at", "approved_at", "status"), ("task_id",)),
        ("task_items", ("task_id", "component_id", "required_quantity_kg"), ("task_id", "component_id")),
        ("stock_lots", ("stock_lot_id", "component_id", "quantity_kg", "admission_status"), ("stock_lot_id",)),
        ("reservations", ("reservation_id", "task_id", "created_at", "status"), ("reservation_id",)),
        ("reservation_items", ("reservation_id", "stock_lot_id", "reserved_quantity_kg"), ("reservation_id", "stock_lot_id")),
    )
    require_fields(data, tuple(name for name, _, _ in tables), "Данные процесса")
    for name, fields, key in tables:
        if not isinstance(data[name], list):
            raise ValueError(f"{name}: нужен список записей.")
        for row in data[name]:
            require_fields(row, fields, name)
            for field in key:
                require_id(row[field], f"{name}.{field}")
        check_unique(data[name], key, name)
    check_unique(data["recipe_versions"], ("product_id", "version_number"), "Версии рецептур")
    check_unique(data["production_tasks"], ("request_id",), "Задания")
    check_unique(data["reservations"], ("task_id",), "Резервы")

    links = (
        ("recipe_versions", "product_id", "products"),
        ("recipe_composition", "recipe_id", "recipe_versions"),
        ("recipe_composition", "component_id", "components"),
        ("production_tasks", "request_id", "requests"),
        ("production_tasks", "recipe_id", "recipe_versions"),
        ("task_items", "task_id", "production_tasks"),
        ("task_items", "component_id", "components"),
        ("stock_lots", "component_id", "components"),
        ("reservations", "task_id", "production_tasks"),
        ("reservation_items", "reservation_id", "reservations"),
        ("reservation_items", "stock_lot_id", "stock_lots"),
    )
    for table, field, parent in links:
        for row in data[table]:
            require_id(row[field], f"{table}.{field}")
            if find_record(data[parent], field, row[field]) is None:
                raise ValueError(f"{table}: не найдена связанная запись {field}={row[field]}.")

    for table in ("products", "components"):
        for row in data[table]:
            if not isinstance(row["name"], str) or not row["name"].strip():
                raise ValueError(f"{table}: название не должно быть пустым.")
    for recipe in data["recipe_versions"]:
        require_id(recipe["version_number"], "Номер версии")
        if recipe["approval_status"] not in ("draft", "approved"):
            raise ValueError("Версия рецептуры: неизвестный статус.")
        start = read_date(recipe["valid_from"], "Начало действия рецептуры")
        if recipe["valid_to"] is not None and read_date(recipe["valid_to"], "Конец действия рецептуры") < start:
            raise ValueError("Конец действия рецептуры раньше начала.")
    for row in data["recipe_composition"]:
        parse_positive_decimal(row["share_percent"], "Доля компонента", 4, 100)
    for request in data["requests"]:
        if request["status"] not in ("new", "processing", "returned", "ready"):
            raise ValueError("Заявка: неизвестный статус.")
        reason = request["return_reason"]
        if request["status"] == "returned":
            if not isinstance(reason, str) or not reason.strip():
                raise ValueError("Возвращённая заявка должна содержать причину.")
        elif reason is not None:
            raise ValueError("Причина возврата допустима только для возвращённой заявки.")
        # Содержимое новой/возвращённой заявки проверяет validate_request().
        # Это позволяет вернуть причину ошибки в массе, продукции или сроке.
        if request["status"] in ("processing", "ready"):
            require_id(request["product_id"], "Продукция заявки")
            if find_record(data["products"], "product_id", request["product_id"]) is None:
                raise ValueError("Заявка ссылается на неизвестную продукцию.")
            parse_positive_decimal(request["batch_mass_kg"], "Масса партии", 3, 1_000_000)
            read_date(request["due_date"], "Срок выпуска")
    for task in data["production_tasks"]:
        if task["status"] not in ("draft", "approved", "returned", "ready"):
            raise ValueError("Задание: неизвестный статус.")
        read_timestamp(task["created_at"], "Дата создания задания")
        if task["status"] in ("approved", "ready"):
            read_timestamp(task["approved_at"], "Дата утверждения задания")
        elif task["approved_at"] is not None:
            raise ValueError("Неутверждённое задание не должно содержать approved_at.")
        if not task_requirements(data, task["task_id"]):
            raise ValueError("Задание должно содержать хотя бы одну позицию.")
    for lot in data["stock_lots"]:
        read_quantity(lot["quantity_kg"], "Складской остаток", 3, allow_zero=True)
        if lot["admission_status"] not in ("pending", "admitted", "blocked"):
            raise ValueError("Партия сырья: неизвестный статус допуска.")
    for item in data["reservation_items"]:
        read_quantity(item["reserved_quantity_kg"], "Количество резерва", 9)
    for header in data["reservations"]:
        read_timestamp(header["created_at"], "Дата создания резерва")
        if header["status"] not in ("active", "cancelled"):
            raise ValueError("Резерв: неизвестный статус.")
        if find_record(data["reservation_items"], "reservation_id", header["reservation_id"]) is None:
            raise ValueError("Резерв должен содержать хотя бы одну позицию.")
        task = find_record(data["production_tasks"], "task_id", header["task_id"])
        if header["status"] == "active" and task["status"] != "ready":
            raise ValueError("Действующий резерв должен принадлежать готовому заданию.")
    try:
        with localcontext() as context:
            context.prec = 40
            context.traps[Inexact] = True
            validate_ready_tasks(data)
    except DecimalException:
        raise ValueError("Не удалось точно проверить количества в данных процесса.") from None


def validate_ready_tasks(data):
    for lot in data["stock_lots"]:
        reserved = Decimal("0")
        for item in data["reservation_items"]:
            header = find_record(data["reservations"], "reservation_id", item["reservation_id"])
            if item["stock_lot_id"] == lot["stock_lot_id"] and header["status"] == "active":
                reserved += Decimal(item["reserved_quantity_kg"])
        if reserved > Decimal(lot["quantity_kg"]):
            raise ValueError("Действующие резервы превышают складской остаток.")
    for request in data["requests"]:
        task = find_record(data["production_tasks"], "request_id", request["request_id"])
        if request["status"] == "ready" and (task is None or task["status"] != "ready"):
            raise ValueError("Готовая заявка должна иметь готовое задание.")
    for task in data["production_tasks"]:
        if task["status"] != "ready":
            continue
        request = find_record(data["requests"], "request_id", task["request_id"])
        recipe = find_record(data["recipe_versions"], "recipe_id", task["recipe_id"])
        header = find_record(data["reservations"], "task_id", task["task_id"])
        if request["status"] != "ready" or recipe["product_id"] != request["product_id"]:
            raise ValueError("Готовое задание не соответствует заявке.")
        if header is None or header["status"] != "active":
            raise ValueError("Готовое задание должно иметь действующий резерв.")
        composition = [row for row in data["recipe_composition"] if row["recipe_id"] == recipe["recipe_id"]]
        expected = calculate_requirements(request["batch_mass_kg"], composition)
        needs = task_requirements(data, task["task_id"])
        if {row["component_id"]: row["required_quantity_kg"] for row in needs} != {
            row["component_id"]: row["required_quantity_kg"] for row in expected
        }:
            raise ValueError("Позиции готового задания не соответствуют заявке и рецептуре.")
        totals = {}
        for item in data["reservation_items"]:
            if item["reservation_id"] != header["reservation_id"]:
                continue
            lot = find_record(data["stock_lots"], "stock_lot_id", item["stock_lot_id"])
            if lot["admission_status"] != "admitted":
                raise ValueError("Действующий резерв содержит недопущенное сырьё.")
            component_id = lot["component_id"]
            totals[component_id] = totals.get(component_id, Decimal("0")) + Decimal(item["reserved_quantity_kg"])
        if totals != {row["component_id"]: row["required_quantity_kg"] for row in needs}:
            raise ValueError("Действующий резерв должен точно покрывать все позиции задания.")
