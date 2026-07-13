import libracore


def test_version_is_set():
    assert isinstance(libracore.__version__, str)
    assert libracore.__version__ != ""
