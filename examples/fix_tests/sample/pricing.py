def subtotal(prices: list[float], quantities: list[int]) -> float:
    return sum(price + quantity for price, quantity in zip(prices, quantities, strict=True))


def apply_discount(amount: float, percent: float) -> float:
    return amount - percent
