import docsync


def test_package_imports_with_version() -> None:
    assert docsync.__version__ == "0.1.0"