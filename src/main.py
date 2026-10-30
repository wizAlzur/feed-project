"""Консольный запуск расчёта потребности в компонентах."""

import argparse
import json
import sys
from pathlib import Path

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


def main():
    parser = argparse.ArgumentParser(description="Расчёт сырья для партии комбикорма.")
    parser.add_argument("batch_mass_kg", help="масса партии в кг, например 1000 или 125.500")
    parser.add_argument("composition_file", help="JSON с составом выбранной рецептуры")
    args = parser.parse_args()

    try:
        composition = load_recipe_composition(args.composition_file)
        requirements = calculate_requirements(args.batch_mass_kg, composition)
    except (OSError, UnicodeError, ValueError) as error:
        print(f"Ошибка: {error}", file=sys.stderr)
        return 1

    print("Потребность в компонентах:")
    for item in requirements:
        quantity = format(item["required_quantity_kg"], "f")
        if "." in quantity:
            quantity = quantity.rstrip("0").rstrip(".")
        print(f"Компонент {item['component_id']}: {quantity} кг")
    return 0


if __name__ == "__main__":
    sys.exit(main())
