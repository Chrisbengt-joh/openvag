"""Tester för felkodsdatabas och statusbyteavkodning."""

from __future__ import annotations

from vagdiag import felkoder
from vagdiag.felkoder import avkoda, avkoda_block, stalldonsnamn, statustext


def test_avkoda_kand_kod():
    """17965 = 0x462D ska bli laddtrycksreglering, sporadisk."""
    kod = avkoda(0x46, 0x2D, 0xAA)
    assert kod.kod == 17965
    assert kod.nummer == "17965"
    assert kod.kand is True
    assert "Laddtrycksreglering" in kod.text
    assert kod.sporadisk is True


def test_avkoda_statisk_kod():
    """Utan bit 0x80 är felet statiskt."""
    kod = avkoda(0x02, 0x30, 0x2A)
    assert kod.kod == 560
    assert kod.nummer == "00560"
    assert kod.sporadisk is False
    assert "statisk" in kod.statustext
    assert "EGR" in kod.text


def test_okand_kod_ger_googlingstips():
    """Okänd kod ska visa numret och be användaren söka."""
    kod = avkoda(0x30, 0x39, 0x00)
    assert kod.kand is False
    assert "12345" in kod.text
    assert "VAG felkod" in kod.text


def test_statustext_innehaller_ravardet():
    """Elaborationen är en tolkning – hexvärdet ska alltid finnas kvar."""
    text = statustext(0xAA)
    assert "sporadisk" in text
    assert "0xAA" in text
    assert "reglergräns" in text


def test_statustext_okand_elaboration():
    """Okänd elaboration ska inte krascha, bara utelämnas."""
    text = statustext(0x7F)
    assert "statisk" in text
    assert "0x7F" in text


def test_avkoda_block_flera_koder():
    """Ett block med tre koder ska ge tre poster."""
    data = bytes([0x46, 0x2D, 0xAA, 0x02, 0x30, 0x2A, 0x00, 0x01, 0x00])
    koder = avkoda_block(data)
    assert [k.kod for k in koder] == [17965, 560, 1]


def test_avkoda_block_filtrerar_tomma_platser():
    """0xFFFF betyder 'ingen felkod' och ska filtreras bort."""
    assert avkoda_block(bytes([0xFF, 0xFF, 0x88])) == []
    koder = avkoda_block(bytes([0xFF, 0xFF, 0x88, 0x46, 0x2D, 0xAA]))
    assert [k.kod for k in koder] == [17965]


def test_avkoda_block_ofullstandig_svans():
    """En avhuggen sista trippel ska ignoreras, inte krascha."""
    assert avkoda_block(bytes([0x46, 0x2D])) == []


def test_databasen_tacker_tdi_nyckelkoder():
    """De koder felsökningsguiden hänvisar till ska finnas i databasen."""
    for kod in (17965, 17966, 16485, 16486, 16487, 553, 560, 17811,
                16785, 16786, 550, 1248, 1268, 1111, 65535):
        assert kod in felkoder.FELKODER, f"saknar {kod}"


def test_felkod_str_ar_lasbar():
    """Textrepresentationen ska innehålla nummer, klartext och status."""
    text = str(avkoda(0x46, 0x2D, 0xAA))
    assert "17965" in text
    assert "[SP]" in text
    assert "0xAA" in text


def test_stalldonsnamn():
    """Kända komponentkoder ska namnges, okända visas som hex."""
    assert "N18" in stalldonsnamn(0x0102)
    assert "0x0999" in stalldonsnamn(0x0999)
