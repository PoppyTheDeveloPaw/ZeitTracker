import ast

import make_version_info as mvi


def test_version_tuple():
    assert mvi.version_tuple("1.2.0") == (1, 2, 0, 0)
    assert mvi.version_tuple("2.10") == (2, 10, 0, 0)
    assert mvi.version_tuple("1.2.3-beta.4") == (1, 2, 3, 0)
    assert mvi.version_tuple("dev") == (0, 0, 0, 0)


def test_render_is_valid_python_with_version():
    text = mvi.render("1.2.0")
    ast.parse(text)  # PyInstaller wertet die Datei als Python-Ausdruck aus
    assert "filevers=(1, 2, 0, 0)" in text
    assert "StringStruct('ProductVersion', '1.2.0')" in text
    assert "'OriginalFilename', 'ZeitTracker.exe'" in text
