from pricing import apply_discount, subtotal


def test_subtotal_multiplies_each_price_by_its_quantity() -> None:
    assert subtotal([2.0, 5.0], [3, 1]) == 11.0


def test_a_discount_is_a_percentage_of_the_amount() -> None:
    assert apply_discount(200.0, 10.0) == 180.0
