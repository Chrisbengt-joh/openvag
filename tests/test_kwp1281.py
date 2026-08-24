"""Tester för protokollstacken – körs helt mot ECU-simulatorn, ingen hårdvara."""

from __future__ import annotations

import time

import pytest

from vagdiag.felkoder import Felkod
from vagdiag.kwp1281 import KWP1281, Blocktitel, KWPKanal, skanna
from vagdiag.simulator import SIM_LOGIN, starta_simulator
from vagdiag.transport import skapa_lankat_par
from vagdiag.undantag import (
    KWPAnslutningsFel,
    KWPFel,
    KWPProtokollFel,
    KWPTimeout,
)


@pytest.fixture()
def koppling():
    """En startad simulator med kort sessionstimeout (snabb nedstängning)."""
    sim = starta_simulator(sessionstimeout=1.0)
    try:
        yield sim
    finally:
        sim.stang()


@pytest.fixture()
def klient(koppling):
    """En ansluten klient mot simulatorn."""
    k = KWP1281(koppling.transport, timeout=0.5, init_timeout=2.0, ack_fordrojning=0.0)
    k.anslut(0x01)
    try:
        yield k
    finally:
        k.stoppa_keepalive()


# ---------------------------------------------------------------------------
# Blockformatet i sig
# ---------------------------------------------------------------------------


def test_blockformat_fram_och_tillbaka():
    """Ett block ska överleva resan med rätt titel, data och räknare."""
    a_transport, b_transport = skapa_lankat_par()
    a = KWPKanal(a_transport, timeout=1.0, ack_fordrojning=0.0)
    b = KWPKanal(b_transport, timeout=1.0, ack_fordrojning=0.0)

    import threading

    mottaget = []
    trad = threading.Thread(target=lambda: mottaget.append(b.las_block()))
    trad.start()
    a.skicka_block(Blocktitel.LAS_MATGRUPP, bytes([3]))
    trad.join(timeout=3.0)

    assert len(mottaget) == 1
    block = mottaget[0]
    assert block.titel == Blocktitel.LAS_MATGRUPP
    assert block.data == bytes([3])
    assert block.raknare == 1


def test_blockraknaren_wrappar():
    """Blockräknaren ska gå 0xFF -> 0x00."""
    transport, _ = skapa_lankat_par()
    kanal = KWPKanal(transport)
    kanal.raknare = 0xFE
    assert kanal.nasta_raknare() == 0xFF
    assert kanal.nasta_raknare() == 0x00


# ---------------------------------------------------------------------------
# Anslutning och identifikation
# ---------------------------------------------------------------------------


def test_anslut_laser_ident(klient):
    """Init ska ge nyckelbytes, delnummer, komponentnamn och kodning."""
    ident = klient.ident
    assert klient.ansluten
    assert ident is not None
    assert ident.kb1 == 0x01 and ident.kb2 == 0x8A
    assert ident.delnummer == "1Z9906019"
    assert ident.komponent == "TDI SIMULATOR"
    assert ident.kodning == 1
    assert ident.wsc == 12345
    assert "Motorstyrdon" in ident.sammanfattning()


def test_anslut_till_tyst_adress_ger_anslutningsfel(koppling):
    """Adress utan styrdon ska ge ett begripligt fel, inte en hängning."""
    k = KWP1281(koppling.transport, timeout=0.3, init_timeout=0.3)
    with pytest.raises(KWPAnslutningsFel) as info:
        k.anslut(0x02, forsok=1)
    assert info.value.tips


def test_koppla_ner_avslutar_sessionen(klient, koppling):
    """Efter 0x06 ska simulatorn vara tillbaka och vänta på ny väckning."""
    klient.koppla_ner()
    assert not klient.ansluten
    time.sleep(0.1)
    klient.anslut(0x01)
    assert koppling.ecu.sessioner == 2


# ---------------------------------------------------------------------------
# Felkoder
# ---------------------------------------------------------------------------


def test_las_felkoder(klient):
    """Båda simulerade felkoderna ska läsas och avkodas."""
    koder = klient.las_felkoder()
    assert [k.kod for k in koder] == [17965, 560]
    assert isinstance(koder[0], Felkod)
    assert koder[0].sporadisk is True
    assert koder[1].sporadisk is False
    assert "Laddtrycksreglering" in koder[0].text
    assert "sporadisk" in koder[0].statustext


def test_radera_felkoder(klient, koppling):
    """Efter radering ska minnet vara tomt."""
    klient.radera_felkoder()
    assert koppling.ecu.raderingar == 1
    assert klient.las_felkoder() == []


# ---------------------------------------------------------------------------
# Mätvärden
# ---------------------------------------------------------------------------


def test_las_matgrupp_3(klient):
    """Grupp 3 ska ge varvtal, luftmassa BÖR/ÄR och EGR-styrgrad."""
    varden = klient.las_matgrupp(3)
    assert len(varden) == 4
    assert varden[0].enhet == "1/min"
    assert 800 <= varden[0].tal <= 3500
    assert varden[1].enhet == "mg/slag"
    assert varden[2].enhet == "mg/slag"
    assert varden[3].enhet == "%"


def test_las_matgrupp_11_laddtryck(klient):
    """Grupp 11 ska ge laddtryck i mbar."""
    varden = klient.las_matgrupp(11)
    assert varden[1].enhet == "mbar"
    assert 900 <= varden[1].tal <= 2200


def test_okand_matgrupp_ger_tom_lista(klient):
    """En grupp som styrdonet saknar ska ge tom lista, inte undantag."""
    assert klient.las_matgrupp(200) == []


def test_grundinstallning(klient):
    """Grundinställning ska ge mätvärden precis som en vanlig grupp."""
    varden = klient.grundinstallning(3)
    assert len(varden) == 4


# ---------------------------------------------------------------------------
# Ställdon, anpassning, login
# ---------------------------------------------------------------------------


def test_stalldonssekvens(klient):
    """Ställdonstestet ska stega igenom sekvensen och sedan ta slut."""
    namn = []
    for _ in range(10):
        stalldon = klient.stalldonstest_nasta()
        if stalldon is None:
            break
        namn.append(stalldon.namn)
    assert len(namn) == 4
    assert "N18" in namn[0]
    assert klient.stalldonstest_nasta() is None


def test_anpassning_las_testa_spara(klient, koppling):
    """Läsning ger startvärdet, test ändrar inget, spara skriver."""
    assert klient.las_anpassning(2).varde == 100
    assert klient.testa_anpassning(2, 130).varde == 130
    assert koppling.ecu.anpassning[2] == 100  # test sparar inte
    assert klient.spara_anpassning(2, 130).varde == 130
    assert koppling.ecu.anpassning[2] == 130
    assert klient.las_anpassning(2).matvarden  # svaret bär även mätvärden


def test_anpassning_utanfor_intervall(klient):
    """Värden över 65535 ska stoppas innan de når bussen."""
    from vagdiag.undantag import VagdiagFel

    with pytest.raises(VagdiagFel):
        klient.testa_anpassning(1, 70000)


def test_login(klient):
    """Rätt kod ger True, fel kod ger False."""
    assert klient.login(SIM_LOGIN) is True
    assert klient.login(12345) is False


# ---------------------------------------------------------------------------
# Keep-alive
# ---------------------------------------------------------------------------


def test_keepalive_haller_sessionen_vid_liv(koppling):
    """Med keep-alive-tråden ska sessionen överleva en tyst period."""
    koppling.ecu.sessionstimeout = 0.4
    k = KWP1281(koppling.transport, timeout=0.5, ack_fordrojning=0.0)
    k.anslut(0x01)
    k.starta_keepalive(intervall=0.1)
    try:
        time.sleep(1.2)
        assert k.bakgrundsfel is None
        assert len(k.las_matgrupp(3)) == 4
    finally:
        k.stoppa_keepalive()
        k.koppla_ner()


def test_utan_keepalive_dor_sessionen(koppling):
    """Utan keep-alive ska styrdonet tappa sessionen – och felet vara begripligt."""
    koppling.ecu.sessionstimeout = 0.3
    k = KWP1281(koppling.transport, timeout=0.4, ack_fordrojning=0.0)
    k.anslut(0x01)
    time.sleep(0.9)
    with pytest.raises(KWPFel) as info:
        k.las_matgrupp(3)
    assert info.value.tips
    assert not k.ansluten


# ---------------------------------------------------------------------------
# Felinjektion – klienten ska tappa sessionen snyggt, aldrig hänga
# ---------------------------------------------------------------------------


def test_tappad_kvittens_ger_timeout(klient, koppling):
    """Uteblivet kvittensbyte ska ge KWPTimeout och död session."""
    koppling.ecu.fel.tappa_nasta_kvittens = True
    with pytest.raises(KWPTimeout):
        klient.las_matgrupp(3)
    assert not klient.ansluten


def test_korrupt_kvittens_ger_protokollfel(klient, koppling):
    """Fel komplement ska upptäckas direkt."""
    koppling.ecu.fel.korrupt_nasta_kvittens = True
    with pytest.raises(KWPProtokollFel) as info:
        klient.las_matgrupp(3)
    assert "kvittens" in str(info.value).lower()


def test_uteblivet_svar_ger_timeout(klient, koppling):
    """Styrdon som tystnar efter ett kommando ska ge timeout."""
    koppling.ecu.fel.tyst_pa_nasta_kommando = True
    with pytest.raises(KWPTimeout):
        klient.las_matgrupp(3)


def test_korrupt_etx_ger_protokollfel(klient, koppling):
    """Block som inte avslutas med 0x03 ska underkännas."""
    koppling.ecu.fel.korrupt_etx_i_nasta_svar = True
    with pytest.raises(KWPProtokollFel) as info:
        klient.las_matgrupp(3)
    assert "0x03" in str(info.value)


def test_fel_blockraknare_upptacks(klient, koppling):
    """En blockräknare som hoppar betyder att sessionen är ur synk."""
    koppling.ecu.fel.fel_raknare_i_nasta_svar = True
    with pytest.raises(KWPProtokollFel) as info:
        klient.las_matgrupp(3)
    assert "räknare" in str(info.value).lower()


def test_ingen_respons_pa_init(koppling):
    """Styrdon som inte vaknar ska ge anslutningsfel med tips."""
    koppling.ecu.fel.svara_inte_pa_init = True
    k = KWP1281(koppling.transport, timeout=0.3, init_timeout=0.3)
    with pytest.raises(KWPAnslutningsFel):
        k.anslut(0x01, forsok=1)


def test_fel_synkbyte(koppling):
    """Fel synkbyte i stället för 0x55 ska ge anslutningsfel."""
    koppling.ecu.fel.fel_synkbyte = True
    k = KWP1281(koppling.transport, timeout=0.3, init_timeout=0.5)
    with pytest.raises(KWPAnslutningsFel) as info:
        k.anslut(0x01, forsok=1)
    assert "0x55" in str(info.value)


def test_klienten_aterhamtar_sig_efter_fel(klient, koppling):
    """Efter ett protokollfel ska en ny anslutning fungera."""
    koppling.ecu.fel.tappa_nasta_kvittens = True
    with pytest.raises(KWPTimeout):
        klient.las_matgrupp(3)
    time.sleep(1.1)  # låt simulatorn ge upp sin trasiga session
    klient.anslut(0x01)
    assert len(klient.las_matgrupp(3)) == 4


# ---------------------------------------------------------------------------
# Auto-scan
# ---------------------------------------------------------------------------


def test_skanna_hittar_bara_svarande_styrdon(koppling):
    """Tysta adresser ska rapporteras som tysta utan att scanningen stannar."""
    resultat = skanna(
        koppling.transport,
        adresser=(0x01, 0x02, 0x03),
        forsok=1,
        timeout=0.3,
        init_timeout=0.3,
        paus=0.0,
    )
    assert [r.adress for r in resultat] == [0x01, 0x02, 0x03]
    motor = resultat[0]
    assert motor.svarade is True
    assert motor.ident is not None
    assert len(motor.felkoder) == 2
    assert all(not r.svarade for r in resultat[1:])
    assert all(r.fel for r in resultat[1:])
