"""Расчёт потребности и проверка обеспеченности сырьём."""

import argparse
import json
import sys
from pathlib import Path

from availability import check_availability
from requirements import calculate_requirements


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


def main():
    parser = argparse.ArgumentParser(description="Расчёт сырья для партии комбикорма.")
    parser.add_argument("batch_mass_kg", help="масса партии в кг, например 1000 или 125.500")
    parser.add_argument("composition_file", help="JSON с составом выбранной рецептуры")
    parser.add_argument("--stock-file", help="JSON со складскими партиями и резервами")
    parser.add_argument("--task-id", type=int, help="ID текущего задания при повторной проверке")
    args = parser.parse_args()
    if args.task_id is not None and args.stock_file is None:
        parser.error("Параметр --task-id используется вместе с --stock-file.")

    try:
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
