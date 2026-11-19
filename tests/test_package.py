from importlib.resources import files


def test_package_ships_type_marker() -> None:
    assert files("cuaderna").joinpath("py.typed").is_file()
