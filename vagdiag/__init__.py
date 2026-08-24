"""VAGDIAG – öppen VCDS-liknande diagnosmjukvara för äldre VAG-bilar (KWP1281).

Paketet är uppdelat i rena lager:

* :mod:`vagdiag.transport`  – fysiskt lager (serieport eller virtuell buss)
* :mod:`vagdiag.kwp1281`    – protokollet, helt utan användargränssnitt
* :mod:`vagdiag.formler`    – omräkning av mätvärden (formel-id, a, b)
* :mod:`vagdiag.felkoder`   – felkodsdatabas och statusbyteavkodning
* :mod:`vagdiag.styrdon`    – styrdonsadresser och gruppetiketter
* :mod:`vagdiag.loggning`   – CSV-loggning av provkörningar
* :mod:`vagdiag.meny`       – terminalgränssnitt
* :mod:`vagdiag.gui`        – tkinter-instrumentpanel
* :mod:`vagdiag.simulator`  – virtuell ECU för test utan bil

Hobbyverktyg – används på egen risk. VCDS är facit.
"""

from __future__ import annotations

__version__ = "1.0.0"
__all__ = ["__version__"]
