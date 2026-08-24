"""Tester för mätvärdesformlerna."""

from __future__ import annotations

import pytest

from vagdiag import formler
from vagdiag.formler import berakna, koda


@pytest.mark.parametrize(
    "formel_id, a, b, vantat, enhet",
    [
        (1, 100, 50, 1000.0, "1/min"),      # 0.2 * a * b
        (2, 200, 100, 40.0, "%"),           # a * 0.002 * b
        (3, 100, 50, 10.0, "grader"),       # 0.002 * a * b
        (4, 20, 137, 2.0, "°FÖD"),          # abs(b-127) * 0.01 * a
        (5, 10, 200, 100.0, "°C"),          # a * (b-100) * 0.1
        (6, 100, 138, 13.8, "V"),           # 0.001 * a * b
        (7, 100, 90, 90.0, "km/h"),         # 0.01 * a * b
        (9, 50, 137, 10.0, "grader"),       # (b-127) * 0.02 * a
        (12, 100, 200, 20.0, "ohm"),        # 0.001 * a * b
        (14, 100, 20, 10.0, "bar"),         # 0.005 * a * b
        (15, 100, 25, 25.0, "ms"),          # 0.01 * a * b
        (18, 200, 100, 800.0, "mbar"),      # 0.04 * a * b
        (20, 128, 192, 64.0, "%"),          # a * (b-128) / 128
        (25, 182, 10, 15.21, "g/s"),        # (b*1.421) + (a/182)
    ],
)
def test_kanda_formler(formel_id, a, b, vantat, enhet):
    """Kända indata ska ge kända utdata."""
    varde = berakna(formel_id, a, b)
    assert varde.tal == pytest.approx(vantat, rel=1e-9, abs=1e-9)
    assert varde.enhet == enhet
    assert varde.kand


def test_kall_varm():
    """Id 10 är en binär temperaturflagga."""
    assert berakna(10, 0, 0).varde == "kall"
    assert berakna(10, 0, 1).varde == "varm"


def test_bitfalt():
    """Id 16 visar åtta binära flaggor."""
    assert berakna(16, 0xFF, 0b10100101).varde == "10100101"
    assert "mask" in str(berakna(16, 0b00001111, 0b10100101).varde)


def test_ascii_par():
    """Id 17 är två ASCII-tecken; oskrivbara byte blir punkt."""
    assert berakna(17, 65, 66).varde == "AB"
    assert berakna(17, 0, 66).varde == ".B"


def test_okand_formel_ger_ravarde():
    """Okänt formel-id ska aldrig krascha, bara visas rått."""
    varde = berakna(250, 1, 2)
    assert varde.kand is False
    assert varde.tal is None
    assert varde.text == "raw(250,1,2)"


def test_ingen_formel_kraschar():
    """Alla id 0–255 med extremvärden ska gå igenom utan undantag."""
    for formel_id in range(256):
        for a in (0, 1, 127, 128, 255):
            for b in (0, 1, 127, 128, 255):
                varde = berakna(formel_id, a, b)
                assert varde.text  # alltid något att visa


def test_overifierade_formler_markeras():
    """Rekonstruerade formler ska flaggas med ? i texten."""
    assert berakna(1, 100, 50).verifierad is True
    assert berakna(39, 20, 138).verifierad is False
    assert berakna(39, 20, 138).text.endswith("?")


def test_division_med_noll_ger_noll():
    """Formler med nämnare ska ge 0 i stället för att kasta."""
    assert berakna(22, 100, 0).tal == 0.0
    assert berakna(33, 100, 0).tal == 0.0


def test_koda_ar_invers_av_berakna():
    """Simulatorns kodning ska ge tillbaka ungefär det önskade värdet."""
    for formel_id, onskat, a, tolerans in [
        (1, 2000.0, 100, 20.0),
        (5, 85.0, 10, 1.0),
        (18, 1800.0, 200, 10.0),
        (51, 640.0, 40, 5.0),
        (2, 45.0, 200, 1.0),
    ]:
        trippel = koda(formel_id, onskat, a=a)
        assert trippel[0] == formel_id
        assert 0 <= trippel[1] <= 255 and 0 <= trippel[2] <= 255
        tillbaka = berakna(*trippel)
        assert tillbaka.tal == pytest.approx(onskat, abs=tolerans)


def test_koda_okant_id_kraschar_inte():
    """Kodning av ett okänt id ska ge ett giltigt men nollställt trippel."""
    assert koda(250, 100.0) == (250, 100, 0)


def test_formatering_av_text():
    """Texten ska ha enhet och lagom antal decimaler."""
    assert berakna(1, 100, 50).text == "1000 1/min"
    assert berakna(6, 100, 138).text == "13.80 V"


def test_registrera_egen_formel():
    """Tabellen ska gå att patcha efter jämförelse mot VCDS."""
    original = formler.FORMLER[51]
    try:
        formler.registrera(
            formler.Formel(51, "mg/slag", "egen", lambda a, b: float(a + b))
        )
        assert berakna(51, 10, 5).tal == 15.0
    finally:
        formler.registrera(original)
    assert berakna(51, 10, 5).tal == pytest.approx(5.0)
