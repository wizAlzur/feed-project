"""Подготовка производственного задания по утверждённой рецептуре."""

from copy import deepcopy
from decimal import Decimal, DecimalException, Inexact, localcontext

from availability import check_availability, find_record, require_fields, require_id
from requirements import calculate_requirements, parse_positive_decimal
from workflow_data import read_date, read_timestamp, task_requirements, validate_workflow_data


def validate_request(request, products, processing_date):
    """Вернуть все ошибки обязательных полей заявки."""
    processing_date = read_date(processing_date, "Дата обработки")
    errors = []
    if not isinstance(request, dict):
        return ["Заявка должна быть словарём."]
    try:
        require_id(request.get("product_id"), "Продукция")
        if find_record(products, "product_id", request["product_id"]) is None:
            errors.append("Указанная продукция отсутствует в справочнике.")
    except ValueError as error:
        errors.append(str(error))
    try:
        parse_positive_decimal(request.get("batch_mass_kg"), "Масса партии", 3, 1_000_000)
    except ValueError as error:
        errors.append(str(error))
    try:
        if read_date(request.get("due_date"), "Срок выпуска") < processing_date:
            errors.append("Срок выпуска раньше даты обработки заявки.")
    except ValueError as error:
        errors.append(str(error))
    return errors


def select_recipe(recipe_versions, product_id, processing_date):
    """Выбрать единственную утверждённую версию, действующую на дату обработки."""
    processing_date = read_date(processing_date, "Дата обработки")
    candidates = []
    for recipe in recipe_versions:
        if recipe["product_id"] != product_id or recipe["approval_status"] != "approved":
            continue
        start = read_date(recipe["valid_from"], "Начало действия рецептуры")
        end = None if recipe["valid_to"] is None else read_date(recipe["valid_to"], "Конец действия рецептуры")
        if start <= processing_date and (end is None or processing_date <= end):
            candidates.append(recipe)
    if not candidates:
        raise ValueError("Нет действующей утверждённой рецептуры для указанной продукции.")
    if len(candidates) > 1:
        raise ValueError("Найдено несколько действующих утверждённых рецептур; требуется уточнить сроки их действия.")
    return candidates[0].copy()


def format_return_reason(errors=None, availability=None):
    """Собрать причины возврата, включая каждый обнаруженный дефицит."""
    reasons = list(errors or [])
    if availability is not None:
        for item in availability["components"]:
            if item["shortage_quantity_kg"] > 0:
                quantity = format(item["shortage_quantity_kg"], "f")
                if "." in quantity:
                    quantity = quantity.rstrip("0").rstrip(".")
                reasons.append(f"Компонент {item['component_id']}: не хватает {quantity} кг.")
    return " ".join(reasons)


def next_id(records, field):
    return max((row[field] for row in records), default=0) + 1


def create_production_task(data, request_id, recipe_id, requirements, created_at):
    """Создать/пересчитать черновик и его позиции в копии данных."""
    read_timestamp(created_at, "Дата создания задания")
    result = deepcopy(data)
    request = find_record(result["requests"], "request_id", request_id)
    if request is None:
        raise ValueError("Заявка не найдена.")
    task = find_record(result["production_tasks"], "request_id", request_id)
    if task is not None:
        header = find_record(result["reservations"], "task_id", task["task_id"])
        if header is not None and header["status"] == "active":
            raise ValueError("Изменять задание с действующим резервом нельзя; сначала требуется отдельная отмена резерва.")
        task.update(recipe_id=recipe_id, approved_at=None, status="draft")
    else:
        task = {"task_id": next_id(result["production_tasks"], "task_id"),
                "request_id": request_id, "recipe_id": recipe_id,
                "created_at": created_at, "approved_at": None, "status": "draft"}
        result["production_tasks"].append(task)
    result["task_items"] = [row for row in result["task_items"] if row["task_id"] != task["task_id"]]
    for row in requirements:
        result["task_items"].append({"task_id": task["task_id"], **row})
    request.update(status="processing", return_reason=None)
    validate_workflow_data(result)
    return result


def reserve_materials(data, task_id, reserved_at):
    """Повторно проверить запасы и создать полный резерв в копии данных.

    При дефиците возвращает исходное состояние и reserved=False. Распределение
    по складским партиям выполняется в порядке записей входного файла.
    """
    require_id(task_id, "Номер задания")
    validate_workflow_data(data)
    read_timestamp(reserved_at, "Дата создания резерва")
    task = find_record(data["production_tasks"], "task_id", task_id)
    if task is None or task["status"] != "approved" or task["approved_at"] is None:
        raise ValueError("Перед резервированием задание должно быть явно утверждено.")
    header = find_record(data["reservations"], "task_id", task_id)
    if header is not None and header["status"] == "active":
        raise ValueError("У задания уже есть действующий резерв.")
    needs = task_requirements(data, task_id)
    availability = check_availability(
        needs, data["stock_lots"], data["reservations"], data["reservation_items"], task_id
    )
    if not availability["can_fulfill"]:
        return {"data": deepcopy(data), "availability": availability, "reserved": False}

    positions = []
    try:
        with localcontext() as context:
            context.prec = 40
            context.traps[Inexact] = True
            free_lots = []
            for lot in data["stock_lots"]:
                free = Decimal(lot["quantity_kg"])
                for item in data["reservation_items"]:
                    reservation = find_record(data["reservations"], "reservation_id", item["reservation_id"])
                    if item["stock_lot_id"] == lot["stock_lot_id"] and reservation["status"] == "active":
                        free -= Decimal(item["reserved_quantity_kg"])
                free_lots.append({**lot, "free": free})
            for need in needs:
                remaining = need["required_quantity_kg"]
                for lot in free_lots:
                    if lot["component_id"] != need["component_id"] or lot["admission_status"] != "admitted":
                        continue
                    quantity = min(remaining, lot["free"])
                    if quantity > 0:
                        positions.append({"stock_lot_id": lot["stock_lot_id"], "reserved_quantity_kg": quantity})
                        lot["free"] -= quantity
                        remaining -= quantity
                    if remaining == 0:
                        break
                if remaining != 0:
                    raise ValueError("Не удалось сформировать полный резерв.")
    except DecimalException:
        raise ValueError("Не удалось точно рассчитать позиции резерва.") from None

    result = deepcopy(data)
    header = find_record(result["reservations"], "task_id", task_id)
    if header is None:
        header = {"reservation_id": next_id(result["reservations"], "reservation_id"),
                  "task_id": task_id, "created_at": reserved_at, "status": "active"}
        result["reservations"].append(header)
    else:
        header.update(created_at=reserved_at, status="active")
    reservation_id = header["reservation_id"]
    result["reservation_items"] = [row for row in result["reservation_items"] if row["reservation_id"] != reservation_id]
    result["reservation_items"].extend({"reservation_id": reservation_id, **row} for row in positions)
    task = find_record(result["production_tasks"], "task_id", task_id)
    task["status"] = "ready"
    find_record(result["requests"], "request_id", task["request_id"]).update(status="ready", return_reason=None)
    validate_workflow_data(result)
    return {"data": result, "availability": availability, "reserved": True}


def return_request(data, request_id, reason):
    request = find_record(data["requests"], "request_id", request_id)
    request.update(status="returned", return_reason=reason)
    task = find_record(data["production_tasks"], "request_id", request_id)
    if task is not None:
        task.update(status="returned", approved_at=None)


def process_request(data, request_id, processing_at, approve=False, fresh_stock=None):
    """Пройти процесс BPMN. Исходные данные не изменяются даже при отказе.

    fresh_stock - необязательный новый снимок трёх складских таблиц для
    повторной проверки после утверждения. Это последовательная демонстрация
    в памяти; параллельная запись несколькими процессами не поддерживается.
    """
    require_id(request_id, "Номер заявки")
    if type(approve) is not bool:
        raise ValueError("Признак утверждения должен быть bool.")
    timestamp = read_timestamp(processing_at, "Дата и время обработки")
    processing_date = timestamp.date().isoformat()
    validate_workflow_data(data)
    result = deepcopy(data)
    request = find_record(result["requests"], "request_id", request_id)
    if request is None:
        raise ValueError("Заявка не найдена.")
    task = find_record(result["production_tasks"], "request_id", request_id)
    if request["status"] == "ready":
        if fresh_stock is not None:
            raise ValueError("Для готовой заявки складской снимок не заменяется; требуется отдельная обработка изменений.")
        availability = check_availability(
            task_requirements(result, task["task_id"]), result["stock_lots"],
            result["reservations"], result["reservation_items"], task["task_id"]
        )
        return {"data": result, "outcome": "already_ready", "availability": availability}
    if fresh_stock is not None:
        require_fields(fresh_stock, ("stock_lots", "reservations", "reservation_items"), "Новый складской снимок")
        if not approve:
            raise ValueError("Новый складской снимок используется только после явного утверждения задания.")

    errors = validate_request(request, result["products"], processing_date)
    if errors:
        return_request(result, request_id, format_return_reason(errors))
        return {"data": result, "outcome": "returned", "availability": None}
    try:
        recipe = select_recipe(result["recipe_versions"], request["product_id"], processing_date)
        composition = [row for row in result["recipe_composition"] if row["recipe_id"] == recipe["recipe_id"]]
        needs = calculate_requirements(request["batch_mass_kg"], composition)
    except ValueError as error:
        return_request(result, request_id, format_return_reason([str(error)]))
        return {"data": result, "outcome": "returned", "availability": None}
    availability = check_availability(
        needs, result["stock_lots"], result["reservations"], result["reservation_items"],
        None if task is None else task["task_id"]
    )
    if not availability["can_fulfill"]:
        return_request(result, request_id, format_return_reason(availability=availability))
        return {"data": result, "outcome": "returned", "availability": availability}

    result = create_production_task(result, request_id, recipe["recipe_id"], needs, processing_at)
    task = find_record(result["production_tasks"], "request_id", request_id)
    if not approve:
        return {"data": result, "outcome": "draft", "availability": availability}
    task.update(status="approved", approved_at=processing_at)
    if fresh_stock is not None:
        for table in ("stock_lots", "reservations", "reservation_items"):
            result[table] = deepcopy(fresh_stock[table])
    reservation = reserve_materials(result, task["task_id"], processing_at)
    if not reservation["reserved"]:
        return_request(result, request_id, format_return_reason(availability=reservation["availability"]))
        return {"data": result, "outcome": "returned", "availability": reservation["availability"]}
    return {"data": reservation["data"], "outcome": "ready", "availability": reservation["availability"]}
