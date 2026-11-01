"""Расчёт потребности и проверка обеспеченности сырьём."""

import argparse
import json
import os
import sys
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile

from availability import check_availability
from requirements import calculate_requirements
from workflow import process_request
from workflow_data import task_requirements


def load_recipe_composition(path):
    """Прочитать состав одной версии и проверить его составной ключ."""
    with Path(path).open(encoding="utf-8") as source:
        composition = json.load(source)

    if not isinstance(composition, list) or not composition:
        raise ValueError("Состав рецептуры должен быть непустым списком.")

    component_ids = set()
    selected_recipe_id = None
    for item in composition:
        if not isinstance(item, dict):
            raise ValueError("Каждая строка состава должна быть объектом JSON.")
        for key in ("recipe_id", "component_id"):
            if type(item.get(key)) is not int or item[key] <= 0:
                raise ValueError(f"Поле {key} должно содержать положительное целое число.")
        if selected_recipe_id is None:
            selected_recipe_id = item["recipe_id"]
        if item["recipe_id"] != selected_recipe_id:
            raise ValueError("В файле должен быть состав одной версии рецептуры.")
        if item["component_id"] in component_ids:
            raise ValueError("Компонент не может повторяться в одной версии рецептуры.")
        component_ids.add(item["component_id"])

    return composition


def load_stock_data(path):
    with Path(path).open(encoding="utf-8") as source:
        data = json.load(source)
    fields = ("stock_lots", "reservations", "reservation_items")
    if not isinstance(data, dict) or any(field not in data for field in fields):
        raise ValueError("В складском файле нужны stock_lots, reservations и reservation_items.")
    return data


def format_quantity(quantity):
    text = format(quantity, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def encode_decimal(value):
    if isinstance(value, Decimal):
        return format_quantity(value)
    raise TypeError(f"Нельзя записать в JSON: {type(value).__name__}")


def save_workflow_data(path, data):
    """Заменить выходной JSON только после полной записи временного файла."""
    path = Path(path)
    content = json.dumps(data, ensure_ascii=False, indent=2, default=encode_decimal) + "\n"
    temporary = None
    try:
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                prefix="." + path.name, suffix=".tmp", delete=False) as target:
            temporary = Path(target.name)
            target.write(content)
        os.replace(temporary, path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def run_workflow(args):
    with Path(args.data_file).open(encoding="utf-8") as source:
        data = json.load(source)
    fresh_stock = None
    if args.stock_update_file is not None:
        fresh_stock = load_stock_data(args.stock_update_file)
    result = process_request(data, args.request_id, args.processing_at, args.approve, fresh_stock)
    if args.output is not None:
        save_workflow_data(args.output, result["data"])
    request = next(row for row in result["data"]["requests"] if row["request_id"] == args.request_id)
    outcomes = {"draft": "Задание сформировано, ожидается утверждение технологом.",
                "ready": "Задание готово к передаче в производство, сырьё зарезервировано.",
                "already_ready": "Задание уже подготовлено, существующий резерв сохранён.",
                "returned": "Заявка возвращена на доработку."}
    print(f"Заявка {request['request_id']}: {outcomes[result['outcome']]}")
    print(f"Статус заявки: {request['status']}")
    if request["return_reason"] is not None:
        print("Причина: " + request["return_reason"])
    task = next((row for row in result["data"]["production_tasks"] if row["request_id"] == args.request_id), None)
    if task is not None:
        print(f"Задание {task['task_id']}: рецептура {task['recipe_id']}, статус {task['status']}")
        print(f"Продукция: {request['product_id']}; масса партии: {request['batch_mass_kg']} кг; срок: {request['due_date']}")
        for item in task_requirements(result["data"], task["task_id"]):
            print(f"Компонент {item['component_id']}: {format_quantity(item['required_quantity_kg'])} кг")
    if result["outcome"] in ("ready", "already_ready"):
        header = next(row for row in result["data"]["reservations"] if row["task_id"] == task["task_id"])
        print(f"Резерв {header['reservation_id']}:")
        for item in result["data"]["reservation_items"]:
            if item["reservation_id"] == header["reservation_id"]:
                print(f"Партия сырья {item['stock_lot_id']}: {format_quantity(Decimal(item['reserved_quantity_kg']))} кг")
    if args.output is not None:
        print(f"Результат сохранён: {args.output}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Расчёт сырья для партии комбикорма.")
    parser.add_argument("batch_mass_kg", nargs="?", help="масса партии в кг, например 1000 или 125.500")
    parser.add_argument("composition_file", nargs="?", help="JSON с составом выбранной рецептуры")
    parser.add_argument("--stock-file", help="JSON со складскими партиями и резервами")
    parser.add_argument("--task-id", type=int, help="ID текущего задания при повторной проверке")
    parser.add_argument("--data-file", help="JSON со всеми сущностями процесса")
    parser.add_argument("--request-id", type=int, help="ID обрабатываемой заявки")
    parser.add_argument("--processing-at", help="дата и время обработки ISO 8601")
    parser.add_argument("--approve", action="store_true", help="явное подтверждение утверждения технологом")
    parser.add_argument("--stock-update-file", help="новый складской снимок для повторной проверки после утверждения")
    parser.add_argument("--output", help="отдельный JSON для сохранения результата обработки")
    args = parser.parse_args()
    if args.data_file is not None:
        if any(value is not None for value in (args.batch_mass_kg, args.composition_file, args.stock_file, args.task_id)):
            parser.error("Режим --data-file используется отдельно от расчёта по файлу состава.")
        if args.request_id is None or args.processing_at is None:
            parser.error("Для --data-file нужны --request-id и --processing-at.")
        if args.stock_update_file is not None and not args.approve:
            parser.error("--stock-update-file используется вместе с --approve.")
        if args.output is not None:
            inputs = (args.data_file, args.stock_update_file)
            if any(value is not None and Path(args.output).resolve() == Path(value).resolve() for value in inputs):
                parser.error("Выходной файл должен отличаться от входных файлов.")
    else:
        if any(value is not None for value in (args.request_id, args.processing_at, args.stock_update_file, args.output)) or args.approve:
            parser.error("Параметры обработки заявки требуют --data-file.")
        if args.batch_mass_kg is None or args.composition_file is None:
            parser.error("Укажите массу и файл состава либо используйте --data-file.")
    if args.task_id is not None and args.stock_file is None:
        parser.error("Параметр --task-id используется вместе с --stock-file.")

    try:
        if args.data_file is not None:
            return run_workflow(args)
        composition = load_recipe_composition(args.composition_file)
        requirements = calculate_requirements(args.batch_mass_kg, composition)
        availability = None
        if args.stock_file is not None:
            stock = load_stock_data(args.stock_file)
            availability = check_availability(
                requirements, stock["stock_lots"], stock["reservations"],
                stock["reservation_items"], current_task_id=args.task_id
            )
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1

    print("Потребность в компонентах:")
    for item in requirements:
        quantity = format_quantity(item["required_quantity_kg"])
        print(f"Компонент {item['component_id']}: {quantity} кг")
    if availability is not None:
        print("Проверка обеспеченности:")
        for item in availability["components"]:
            required = format_quantity(item["required_quantity_kg"])
            available = format_quantity(item["available_quantity_kg"])
            shortage = format_quantity(item["shortage_quantity_kg"])
            print(f"Компонент {item['component_id']}: требуется {required} кг, доступно {available} кг, дефицит {shortage} кг")
        if availability["can_fulfill"]:
            print("Партия обеспечена сырьём.")
        else:
            print("Партия не обеспечена сырьём.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
