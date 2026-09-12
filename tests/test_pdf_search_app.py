import importlib
import sys

import pytest

from pdf_search_app import escape_like, normalize


def test_normalize_removes_accents_and_lowercases():
    assert normalize("Página\r\ndo Ó") == "pagina  do o"


def test_normalize_empty_input():
    assert normalize("") == ""
    assert normalize(None) == ""


def test_escape_like_escapes_special_characters():
    assert escape_like("50% off_100\\") == "50\\% off\\_100\\\\"


def test_escape_like_leaves_plain_text_unchanged():
    assert escape_like("relatorio final") == "relatorio final"


class _SemWinreg:
    """Finder que simula um sistema sem o modulo winreg (o Linux do CI)."""

    def find_spec(self, name, path=None, target=None):
        if name == "winreg":
            raise ImportError("No module named 'winreg'")
        return None


@pytest.fixture
def app_sem_winreg(monkeypatch):
    """Reimporta o modulo como se estivesse num sistema sem winreg."""
    monkeypatch.delitem(sys.modules, "winreg", raising=False)
    monkeypatch.delitem(sys.modules, "pdf_search_app", raising=False)
    monkeypatch.setattr(sys, "meta_path", [_SemWinreg()] + sys.meta_path)
    return importlib.import_module("pdf_search_app")


def test_modulo_importa_sem_winreg(app_sem_winreg):
    """O CI roda em Linux: o import nao pode depender de um modulo so do Windows."""
    assert app_sem_winreg.winreg is None


def test_default_pdf_reader_exe_sem_winreg(app_sem_winreg):
    """Sem o registro do Windows nao ha leitor padrao a descobrir."""
    assert app_sem_winreg.default_pdf_reader_exe() is None
