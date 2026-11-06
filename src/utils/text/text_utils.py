"""Shared helpers for Russian UI text."""


def plural_ru(value: int | float, one: str, few: str, many: str) -> str:
    """Choose a noun form after a number; fractions use the genitive singular."""
    value = abs(value)
    if value != int(value):
        return few
    number = int(value)
    if number % 10 == 1 and number % 100 != 11:
        return one
    if 2 <= number % 10 <= 4 and not 12 <= number % 100 <= 14:
        return few
    return many
