"""Проверка обеспеченности партии без изменения запасов и резервов."""

from datetime import datetime
from decimal import Decimal, DecimalException, Inexact, localcontext


def require_id(value, field_name):
    if type(value) is not int or value <= 0:
        raise ValueError(f"{field_name}: нужно положительное целое число.")


def require_fields(record, fields, record_name):
    if not isinstance(record, dict):
        raise ValueError(f"{record_name}: запись должна быть словарём.")
    if len(record) != len(fields):
        raise ValueError(f"{record_name}: нужны только поля {', '.join(fields)}.")
    for field in fields:
        if field not in record:
            raise ValueError(f"{record_name}: отсутствует поле {field}.")


def read_quantity(value, field_name, fractional_digits, allow_zero=False):
    """Прочитать точное количество; двоичные float не принимаются."""
    if not isinstance(value, (str, Decimal)):
        raise ValueError(f"{field_name}: передайте десятичную строку или Decimal.")
    try:
        number = Decimal(value)
    except DecimalException:
        raise ValueError(f"{field_name}: некорректное число.") from None
    if not number.is_finite():
        raise ValueError(f"{field_name}: число должно быть конечным.")
    if number < 0 or number > 1_000_000 or (not allow_zero and number == 0):
        lower = "не меньше нуля" if allow_zero else "больше нуля"
        raise ValueError(f"{field_name}: допустимо значение {lower} и не больше 1 000 000 кг.")
    if number.as_tuple().exponent < -fractional_digits:
        raise ValueError(f"{field_name}: допускается не больше {fractional_digits} дробных знаков.")
    return number


def check_unique(records, fields, record_name):
    """Проверить ключи сравнением с ранее просмотренными ключами."""
    known_keys = []
    for record in records:
        key = tuple(record[field] for field in fields)
        if key in known_keys:
            raise ValueError(f"{record_name}: повтор ключа {fields} = {key}.")
        known_keys.append(key)


def validate_availability_data(requirements, stock_lots, reservations, reservation_items, current_task_id):
    """Проверить формат записей и ключи, вернуть копии с Decimal."""
    for records, name in (
        (requirements, "Потребность"),
        (stock_lots, "Складские партии"),
        (reservations, "Резервы"),
        (reservation_items, "Позиции резервов"),
    ):
        if not isinstance(records, list):
            raise ValueError(f"{name}: нужен список записей.")
    if not requirements:
        raise ValueError("Потребность не должна быть пустой.")
    if current_task_id is not None:
        require_id(current_task_id, "Текущее задание")

    prepared_requirements = []
    for row in requirements:
        require_fields(row, ("component_id", "required_quantity_kg"), "Потребность")
        require_id(row["component_id"], "component_id")
        prepared = row.copy()
        prepared["required_quantity_kg"] = read_quantity(row["required_quantity_kg"], "Потребность", 9)
        prepared_requirements.append(prepared)
    check_unique(prepared_requirements, ("component_id",), "Потребность")

    prepared_lots = []
    for row in stock_lots:
        require_fields(row, ("stock_lot_id", "component_id", "quantity_kg", "admission_status"), "Партия сырья")
        require_id(row["stock_lot_id"], "stock_lot_id")
        require_id(row["component_id"], "component_id")
        if row["admission_status"] not in ("pending", "admitted", "blocked"):
            raise ValueError("Партия сырья: неизвестный статус допуска.")
        prepared = row.copy()
        prepared["quantity_kg"] = read_quantity(row["quantity_kg"], "Складской остаток", 3, allow_zero=True)
        prepared_lots.append(prepared)
    check_unique(prepared_lots, ("stock_lot_id",), "Партии сырья")

    prepared_reservations = []
    for row in reservations:
        require_fields(row, ("reservation_id", "task_id", "created_at", "status"), "Резерв")
        require_id(row["reservation_id"], "reservation_id")
        require_id(row["task_id"], "task_id")
        if row["status"] not in ("active", "cancelled"):
            raise ValueError("Резерв: неизвестный статус.")
        if not isinstance(row["created_at"], str):
            raise ValueError("Резерв: created_at должен быть строкой даты и времени ISO 8601.")
        timestamp = row["created_at"]
        if timestamp.endswith("Z"):
            timestamp = timestamp[:-1] + "+00:00"
        try:
            datetime.fromisoformat(timestamp)
        except ValueError:
            raise ValueError("Резерв: некорректная дата created_at.") from None
        prepared_reservations.append(row.copy())
    check_unique(prepared_reservations, ("reservation_id",), "Резервы")
    check_unique(prepared_reservations, ("task_id",), "Резервы")

    prepared_items = []
    for row in reservation_items:
        require_fields(row, ("reservation_id", "stock_lot_id", "reserved_quantity_kg"), "Позиция резерва")
        require_id(row["reservation_id"], "reservation_id")
        require_id(row["stock_lot_id"], "stock_lot_id")
        prepared = row.copy()
        prepared["reserved_quantity_kg"] = read_quantity(row["reserved_quantity_kg"], "Количество резерва", 9)
        prepared_items.append(prepared)
    check_unique(prepared_items, ("reservation_id", "stock_lot_id"), "Позиции резервов")
    for reservation in prepared_reservations:
        if find_record(prepared_items, "reservation_id", reservation["reservation_id"]) is None:
            raise ValueError(f"Резерв {reservation['reservation_id']}: отсутствуют связанные позиции.")

    return prepared_requirements, prepared_lots, prepared_reservations, prepared_items


def find_record(records, field, identifier):
    for record in records:
        if record[field] == identifier:
            return record
    return None


def create_balances(stock_lots):
    """Создать локальные накопители; складские записи не изменяются."""
    balances = []
    for lot in stock_lots:
        balance = lot.copy()
        balance["reserved_all_kg"] = Decimal("0")
        balance["reserved_other_kg"] = Decimal("0")
        balances.append(balance)
    return balances


def check_availability(requirements, stock_lots, reservations, reservation_items, current_task_id=None):
    """Вернуть обеспеченность и все дефициты, при ошибке данных - ValueError.

    Потребность имеет формат результата calculate_requirements(). Остальные
    списки соответствуют сущностям ER. Для текущего задания его собственный
    действующий резерв исключается из занятого количества, но учитывается
    при проверке целостности складских остатков. Новые резервы не создаются.
    """
    needs, lots, headers, items = validate_availability_data(
        requirements, stock_lots, reservations, reservation_items, current_task_id
    )
    balances = create_balances(lots)

    try:
        with localcontext() as context:
            context.prec = 40
            context.traps[Inexact] = True

            for item in items:
                header = find_record(headers, "reservation_id", item["reservation_id"])
                balance = find_record(balances, "stock_lot_id", item["stock_lot_id"])
                if header is None or balance is None:
                    raise ValueError(
                        f"Позиция резерва {item['reservation_id']}, партия {item['stock_lot_id']}: "
                        "не найден связанный заголовок или складская партия."
                    )
                if header["status"] != "active":
                    continue
                balance["reserved_all_kg"] += item["reserved_quantity_kg"]
                if header["task_id"] != current_task_id:
                    balance["reserved_other_kg"] += item["reserved_quantity_kg"]

            for balance in balances:
                if balance["reserved_all_kg"] > balance["quantity_kg"]:
                    raise ValueError(
                        f"Партия сырья {balance['stock_lot_id']}: "
                        "действующие резервы превышают складской остаток."
                    )
                balance["available_quantity_kg"] = balance["quantity_kg"] - balance["reserved_other_kg"]

            components = []
            can_fulfill = True
            for need in needs:
                available = Decimal("0")
                for balance in balances:
                    if balance["component_id"] == need["component_id"] and balance["admission_status"] == "admitted":
                        available += balance["available_quantity_kg"]
                if available < need["required_quantity_kg"]:
                    shortage = need["required_quantity_kg"] - available
                    can_fulfill = False
                else:
                    shortage = Decimal("0")
                components.append({
                    "component_id": need["component_id"],
                    "required_quantity_kg": need["required_quantity_kg"],
                    "available_quantity_kg": available,
                    "shortage_quantity_kg": shortage,
                })
    except DecimalException:
        raise ValueError("Не удалось выполнить точный расчёт доступного количества сырья.") from None

    return {"can_fulfill": can_fulfill, "components": components}
