"""Tester för CSV-loggningen."""

from __future__ import annotations

from vagdiag.formler import berakna
from vagdiag.loggning import CsvLogg


def _matning():
    """Ett avläsningsvarv med grupp 3 och 11."""
    return {
        3: [berakna(1, 100, 100), berakna(51, 40, 20),
            berakna(51, 40, 17), berakna(2, 200, 50)],
        11: [berakna(1, 100, 100), berakna(18, 200, 225),
             berakna(18, 200, 220), berakna(2, 200, 100)],
    }


def test_rubrik_och_rad(tmp_path):
    """Rubriken ska namnge grupp, position, etikett och enhet."""
    fil = tmp_path / "logg.csv"
    with CsvLogg(fil, [3, 11]) as logg:
        logg.logga(_matning())
        assert logg.rader == 1

    rader = fil.read_text(encoding="utf-8-sig").splitlines()
    assert len(rader) == 2
    kolumner = rader[0].split(";")
    assert kolumner[0] == "tidpunkt"
    assert kolumner[1] == "sekunder"
    assert kolumner[2] == "003.1 Varvtal [1/min]"
    assert kolumner[3] == "003.2 Luftmassa BÖR [mg/slag]"
    assert kolumner[6] == "011.1 Varvtal [1/min]"
    assert len(kolumner) == 2 + 8


def test_decimalkomma_som_standard(tmp_path):
    """Svensk Excel vill ha semikolon och decimalkomma."""
    fil = tmp_path / "logg.csv"
    with CsvLogg(fil, [3]) as logg:
        logg.logga({3: _matning()[3]})
    varderad = fil.read_text(encoding="utf-8-sig").splitlines()[1]
    assert ";2000,000;" in varderad


def test_punktformat(tmp_path):
    """--csv-punkt ger komma som avgränsare och decimalpunkt."""
    fil = tmp_path / "logg.csv"
    with CsvLogg(fil, [3], avgransare=",", decimalkomma=False) as logg:
        logg.logga({3: _matning()[3]})
    varderad = fil.read_text(encoding="utf-8-sig").splitlines()[1]
    assert ",2000.000," in varderad


def test_saknade_varden_ger_tomma_celler(tmp_path):
    """En grupp som styrdonet saknar ska ge tomma celler, inte kortare rad."""
    fil = tmp_path / "logg.csv"
    with CsvLogg(fil, [3, 200]) as logg:
        logg.logga({3: _matning()[3], 200: []})
    rader = fil.read_text(encoding="utf-8-sig").splitlines()
    assert len(rader[0].split(";")) == len(rader[1].split(";")) == 10
    assert rader[1].endswith(";;;;")


def test_textvarden_loggas_som_text(tmp_path):
    """Bitfält och ASCII ska hamna i loggen som text."""
    fil = tmp_path / "logg.csv"
    with CsvLogg(fil, [2]) as logg:
        logg.logga({2: [berakna(16, 255, 0b10100101)]})
    assert "10100101" in fil.read_text(encoding="utf-8-sig").splitlines()[1]


def test_varje_rad_flushas(tmp_path):
    """Loggen ska gå att läsa medan den fortfarande är öppen."""
    fil = tmp_path / "logg.csv"
    logg = CsvLogg(fil, [3])
    try:
        logg.logga({3: _matning()[3]})
        logg.logga({3: _matning()[3]})
        assert len(fil.read_text(encoding="utf-8-sig").splitlines()) == 3
    finally:
        logg.stang()
