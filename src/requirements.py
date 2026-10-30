"""Расчёт потребности в сырье по составу выбранной рецептуры."""

from decimal import Decimal, Inexact, InvalidOperation, localcontext


def parse_positive_decimal(value, field_name, fractional_digits, upper_bound):
    """Прочитать положительное число из строки без округления."""
    if not isinstance(value, str):
        raise ValueError(f"{field_name}: число нужно передать строкой.")

    try:
        number = Decimal(value)
    except InvalidOperation:
        raise ValueError(f"{field_name}: некорректное число.") from None

    if not number.is_finite():
        raise ValueError(f"{field_name}: число должно быть конечным.")
    if number <= 0 or number > upper_bound:
        raise ValueError(f"{field_name}: допустимо значение больше 0 и не больше {upper_bound}.")
    if number.as_tuple().exponent < -fractional_digits:
        raise ValueError(f"{field_name}: допускается не больше {fractional_digits} знаков после запятой.")

    return number


def calculate_requirements(batch_mass_kg, recipe_composition):
    """Вернуть component_id и required_quantity_kg для каждой строки состава.

    Состав относится к одной уже выбранной утверждённой версии рецептуры.
    Уникальность компонентов и корректность ключей проверяются до расчёта.
    При ошибке входных чисел выбрасывается ValueError с причиной отказа.
    Исходные данные не изменяются; частичный результат не возвращается.
    """
    mass = parse_positive_decimal(batch_mass_kg, "Масса партии", 3, 1_000_000)
    if not isinstance(recipe_composition, list) or not recipe_composition:
        raise ValueError("Состав рецептуры должен быть непустым списком.")

    with localcontext() as context:
        context.prec = 40
        context.traps[Inexact] = True
        total_share = Decimal("0")
        prepared_composition = []

        for item in recipe_composition:
            if not isinstance(item, dict) or "component_id" not in item or "share_percent" not in item:
                raise ValueError("В строке состава нужны component_id и share_percent.")
            share = parse_positive_decimal(item["share_percent"], "Доля компонента", 4, 100)
            total_share += share
            prepared_composition.append((item["component_id"], share))

        if total_share != Decimal("100"):
            raise ValueError(f"Сумма долей должна равняться 100%, получено {total_share}%.")

        requirements = []
        for component_id, share in prepared_composition:
            quantity = mass * share / Decimal("100")
            requirements.append({
                "component_id": component_id,
                "required_quantity_kg": quantity,
            })

    return requirements
