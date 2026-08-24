# VAGDIAG

Öppen, VCDS-liknande diagnosmjukvara för äldre VAG-bilar som pratar
**KWP1281 över K-line** – alltså modeller från ungefär 1996–2003, före
CAN-diagnos. Byggd för felsökning av 1.9 TDI med VP37-fördelarpump
(EDC15, motorkoder AHF/ALH/AFN/AGR m.fl.), men fungerar mot vilket
KWP1281-styrdon som helst.

> **Hobbyverktyg – används på egen risk. VCDS är facit.**
> Programmet läser och skriver mot bilens styrdon. Felaktig anpassning eller
> grundinställning kan i värsta fall ge en bil som inte startar. Läs
> varningstexterna i programmet och hoppa inte över dem.

**Innehåll**

- [Vad du behöver](#vad-du-behöver)
- [Installation steg för steg](#installation-steg-för-steg)
- [FTDI Latency Timer – 1 ms är ett krav](#ftdi-latency-timer--1-ms-är-ett-krav)
- [Prova utan bil (simulatorn)](#prova-utan-bil-simulatorn)
- [Snabbstart: felsöka en rökande TDI](#snabbstart-felsöka-en-rökande-tdi)
- [Kommandoraden](#kommandoraden)
- [Menyn](#menyn)
- [Instrumentpanelen (GUI)](#instrumentpanelen-gui)
- [CSV-loggarna](#csv-loggarna)
- [Tolkningsguide](#tolkningsguide)
- [Felsökning av programmet självt](#felsökning-av-programmet-självt)
- [Kända begränsningar](#kända-begränsningar)
- [För utvecklare](#för-utvecklare)

---

## Vad du behöver

| Sak | Kommentar |
|-----|-----------|
| **KKL-kabel "VAG 409.1"** | Måste ha **FTDI FT232RL**-chip. Billiga kablar med CH340 eller kloner av FTDI fungerar ofta dåligt eller inte alls med 5-baud-init. |
| **Python 3.10 eller nyare** | Testat på 3.10–3.13. |
| **pyserial** | Enda hårda beroendet. |
| **Windows eller Linux** | Windows är primär plattform, Linux fungerar. |
| **Bilen** | Tändning PÅ. Motorn behöver bara vara igång när du kör provkörningsloggar. |

Programmet pratar **inte** vanlig OBD2 med standard-PID:er, och **inte** CAN.
Det är KWP1281 på K-line (stift 7 i OBD-kontakten).

---

## Installation steg för steg

### 1. Installera Python

Windows: hämta från <https://www.python.org/downloads/> och **kryssa i
"Add python.exe to PATH"** i installationsprogrammet.

Kontrollera i en ny terminal:

```
python --version
```

Linux (Debian/Ubuntu):

```
sudo apt install python3 python3-pip python3-tk
```

### 2. Installera VAGDIAG

Från projektmappen:

```
pip install -e .
```

Det installerar pyserial och lägger till kommandot `vagdiag`. Vill du inte
installera något alls räcker det med:

```
pip install pyserial
python -m vagdiag --simulator
```

### 3. Installera drivrutin till kabeln

Windows hittar oftast FTDI-kabeln av sig själv. Annars: hämta "VCP drivers"
från <https://ftdichip.com/drivers/vcp-drivers/>.

Linux: FTDI-drivrutinen ingår i kärnan. Lägg till dig i rätt grupp så du
slipper `sudo`:

```
sudo usermod -aG dialout $USER
```

Logga ut och in igen.

### 4. Hitta din port

```
python -m vagdiag --lista-portar
```

Utskriften ser ut ungefär så här:

```
  COM3       USB Serial Port  [FTDI Latency Timer: 16 ms – SÄTT TILL 1!]
```

På Linux heter porten oftast `/dev/ttyUSB0`.

Hittar du inget: kontrollera i **Enhetshanteraren → Portar (COM & LPT)** att
kabeln syns som "USB Serial Port (COMx)".

---

## FTDI Latency Timer – 1 ms är ett krav

KWP1281 kvitterar **varje byte** och har hårda tidsfönster. FTDI-chipet
buffrar som standard i 16 ms innan det skickar vidare, vilket gör att
kvittenserna kommer för sent och anslutningen dör mitt i en session.

### Windows – manuellt (rekommenderat)

1. Högerklicka på Start → **Enhetshanteraren**.
2. Fäll ut **Portar (COM & LPT)**.
3. Högerklicka på **USB Serial Port (COMx)** → **Egenskaper**.
4. Fliken **Portinställningar** → knappen **Avancerat…**.
5. I rutan **BM-alternativ** finns **Latency Timer (msec)** – ställ den på **1**.
6. OK, OK. Dra ur och i USB-kabeln.

### Windows – automatiskt

Kör terminalen **som administratör** och sedan:

```
python -m vagdiag COM3 --satt-latens
```

Dra ur och i kabeln efteråt. Programmet varnar automatiskt vid start om
latensen är över 2 ms.

### Linux

```
echo 1 | sudo tee /sys/bus/usb-serial/devices/ttyUSB0/latency_timer
```

Vill du ha det permanent, lägg en udev-regel i
`/etc/udev/rules.d/99-ftdi-latency.rules`:

```
ACTION=="add", SUBSYSTEM=="usb-serial", DRIVER=="ftdi_sio", ATTR{latency_timer}="1"
```

---

## Prova utan bil (simulatorn)

Programmet har en inbyggd virtuell ECU. Den svarar på hela protokollet,
serverar mätvärden som varierar över tid och har två felkoder i minnet.
Ingen kabel, ingen bil, ingen com0com:

```
python -m vagdiag --simulator                 # menyn
python -m vagdiag --simulator --felkoder      # felkodsläsning
python -m vagdiag --simulator --grupper 3 4 11
python -m vagdiag --simulator --gui           # instrumentpanelen
```

Använd den för att lära dig menyn i lugn och ro innan du står böjd över
motorrummet.

---

## Snabbstart: felsöka en rökande TDI

Det här är arbetsgången för symptomen svartrök vid full gas, vitrök med
diesellukt, limp mode vid belastning och sotig EGR.

### 1. Kartlägg bilen

Tändning PÅ, motorn av:

```
python -m vagdiag COM3 --autoscan
```

Skriv av **alla** felkoder innan du gör något annat. Auto-scan tar ett par
minuter – de flesta adresser svarar inte, och det är normalt i en bil
från 1999.

### 2. Radera och nollställ

Meny → **1 Läs felkoder** → **2 Radera felkoder** (kräver att du skriver `JA`).

Poängen med att radera är att se **vilka koder som kommer tillbaka**. En kod
som återkommer efter en provkörning är ett aktivt fel; en som inte gör det
var historik.

### 3. Provkör

Kör bilen tills felen visar sig – full gas, hård belastning, gärna en backe
tills den går i limp mode.

### 4. Scanna igen

```
python -m vagdiag COM3 --felkoder
```

Nu vet du vilka koder som är aktiva.

### 5. Varm tomgång – grupp 013 (jämngångsreglering)

Motorn ska vara **helt varmkörd** och gå på tomgång:

```
python -m vagdiag COM3 --grupper 13
```

Läs av cylinderavvikelserna. Se [tolkningsguiden](#tolkningsguide).

### 6. Logga en backe – grupp 003, 004, 011

Starta loggningen innan du kör, kör upp för backen på full gas i en hög växel
tills det viker sig, och tryck sedan `q`:

```
python -m vagdiag COM3 --grupper 3 4 11 --logg backe_2026-08-24.csv
```

Öppna CSV-filen i Excel och rita upp laddtryck BÖR/ÄR och luftmassa BÖR/ÄR mot
tiden. Det är där du ser exakt när det viker av.

> **Kör inte och läs skärmen samtidigt.** Ta med en passagerare, eller starta
> loggningen, kör, och analysera efteråt. Loggen flushas varje rad, så den är
> komplett även om du bryter mitt i.

---

## Kommandoraden

```
python -m vagdiag [PORT] [flaggor]
```

| Flagga | Betydelse |
|--------|-----------|
| `PORT` | Serieport, t.ex. `COM3` eller `/dev/ttyUSB0`. |
| `--simulator` | Kör mot den virtuella ECU:n i stället för en bil. |
| `--gui` | Starta instrumentpanelen i stället för terminalmenyn. |
| `--autoscan` | Scanna alla styrdon och avsluta. |
| `--felkoder` | Läs felkoder och avsluta. |
| `--grupper 3 4 11` | Liveavläsning av angivna mätgrupper. |
| `--styrdon 01` | Styrdonsadress i hex för direktkörning (standard `01` = motor). |
| `--logg fil.csv` | Logga liveavläsningen till CSV. |
| `--lista-portar` | Visa serieportar och FTDI-latens. |
| `--satt-latens` | Försök sätta Latency Timer till 1 ms (Windows, kräver admin). |
| `--intervall 0.2` | Paus mellan avläsningsvarv (standard: så snabbt som möjligt). |
| `--timeout 1.0` | Timeout per byte. Höj om styrdonet är segt. |
| `--init-timeout 2.0` | Timeout för svaret på 5-baud-väckningen. |
| `--csv-punkt` | CSV med komma och decimalpunkt i stället för svensk Excel-stil. |
| `--debug` | Skriv all blocktrafik till stderr. |

Exempel:

```
python -m vagdiag COM3
python -m vagdiag COM3 --autoscan
python -m vagdiag COM3 --grupper 3 4 11 --logg backe.csv
python -m vagdiag COM3 --styrdon 17 --felkoder     # kombiinstrument
python -m vagdiag COM3 --gui --grupper 3 11
```

---

## Menyn

Utan aktiv session:

| Val | Funktion |
|-----|----------|
| 1 | **Auto-scan** – provar alla vanliga styrdonsadresser, hämtar identifikation och räknar felkoder, och avslutar varje session snyggt. Tål att de flesta adresser är tysta. |
| 2 | **Anslut till styrdon** – 5-baud-väckning av vald adress. |
| 3 | **Anslutningsinformation** – port, latens, protokollparametrar. |

Med aktiv session:

| Val | Funktion |
|-----|----------|
| 1 | **Läs felkoder** – femsiffrig kod, svensk klartext, sporadisk/statisk och avkodad statusbyte. |
| 2 | **Radera felkoder** – kräver att du skriver `JA`. Läser om direkt efteråt så du ser vilka koder som är aktiva. |
| 3 | **Mätgrupper live** – valfria grupper, fyra värden per grupp, med valfri CSV-logg. `q` tillbaka, `l` startar/stoppar loggning. |
| 4 | **Grundinställning** – som mätgrupper men via block 0x28. Varning och `JA` krävs. |
| 5 | **Ställdonstest** – stegar igenom ställdonen. Motorn ska vara AV. `Enter` för nästa, `q` för att avbryta. |
| 6 | **Anpassning** – läser kanal, visar alltid gamla värdet, testar nytt värde, och sparar bara om du skriver `SPARA`. |
| 7 | **Login** – femsiffrig kod (0–65535). |
| 8 | **Visa identifikation** |
| 9 | **Koppla ner** |

Sessionen hålls vid liv av en keep-alive-tråd i bakgrunden, så den dör inte
för att du sitter och funderar i en meny.

---

## Instrumentpanelen (GUI)

```
python -m vagdiag COM3 --gui --grupper 3 11
```

- Runda mätartavlor för varvtal, laddtryck och luftmassa. **BÖR-värdet ligger
  som en andra, orange visare i samma mätare** – avvikelsen syns direkt.
- Rullande realtidsgraf över de senaste 60 sekunderna, med alla numeriska
  värden ur de valda grupperna.
- Start/stopp för CSV-loggning med en knapp.
- Egen flik för felkoder med läs och radera (radering kräver `JA`).

All seriekommunikation sker i en egen tråd som aldrig rör tkinter; GUI:t
uppdateras via en kö och `after()`. Fönstret fryser alltså inte även om
K-line tappar kontakten.

---

## CSV-loggarna

Standardformatet är anpassat för svensk Excel: **semikolon** som avgränsare,
**decimalkomma** och UTF-8 med BOM (så att å/ä/ö blir rätt). Vill du läsa
loggen med pandas eller gnuplot, kör med `--csv-punkt`.

Rubrikraden namnger varje kolumn med grupp, position, etikett och enhet:

```
tidpunkt;sekunder;003.1 Varvtal [1/min];003.2 Luftmassa BÖR [mg/slag];...
```

Varje rad skrivs och flushas direkt till disk, så en 30-minuters
provkörningslogg är komplett även om programmet kraschar eller kabeln
rycks ur.

---

## Tolkningsguide

> Riktvärdena nedan är **ungefärliga** och skiljer sig mellan motorkoder.
> Jämför alltid mot verkstadsdata för just din motorkod, och mot VCDS.

### Grupp 003 – luftmassa BÖR/ÄR och EGR-styrgrad

Vid **full gas** ska ÄR ligga nära BÖR. På en frisk 1.9 TDI landar ÄR
typiskt runt 700–900 mg/slag vid full belastning.

- **ÄR under ~700 mg/slag vid full gas, tydligt lägre än BÖR** – motorn får
  för lite luft för den mängd bränsle som sprutas in. Det är precis vad som
  ger **svartrök**. Leta efter:
  - igensotat insugsrör och EGR (klassiskt på hög mil),
  - EGR-ventil som hänger öppen,
  - läckande laddluftsslang eller intercooler,
  - igensatt luftfilter,
  - trött luftmassemätare (G70).
- **ÄR som ligger nära BÖR men motorn ryker ändå** – då är luften inte
  problemet; gå vidare till insprutningsstart och mängd.
- **Trött MAF** ger ofta för *låga* värden, vilket i sin tur ger
  effektbortfall och kod 16485–16487 eller 00553.
- **EGR-styrgraden** ska gå mot noll vid full gas. Ligger den kvar högt
  under belastning stryper EGR:en luften.

### Grupp 004 – insprutningsstart BÖR/ÄR

- ÄR ska följa BÖR inom ungefär ±1 grad.
- **ÄR konstant efter BÖR (för sen insprutning)** ger dålig förbränning,
  svårstartad motor och **vitrök som luktar diesel** – det är oförbränt
  bränsle. Misstänk N108-ventilen (insprutningsstartventil), en trött
  insprutningspump, låg matningstryck eller luft i bränslet.
- Kolla samtidigt styrgraden på N108 i position 4: ligger den i botten eller
  i taket har regleringen gett upp.
- Relaterade koder: 00550, 01248, 17910–17912.

### Grupp 011 – laddtryck BÖR/ÄR och N75-styrgrad

Logga i en backe på full gas och rita upp BÖR och ÄR mot tiden.

- **ÄR som skjuter över BÖR och sedan tvärt viker av** = överladdning, och
  det är den vanligaste orsaken till **limp mode**. Kod **17965** (P1557,
  positiv avvikelse) hör hit. Vanligaste orsaken på en TDI med hög mil är
  **sotiga, kärvande VNT-skovlar** i turbon, följt av trasig N75-ventil eller
  spruckna vakuumslangar.
- **ÄR som aldrig når BÖR** = underladdning. Kod 17964 (P1556). Leta efter
  läckage i laddluftsystemet, kärvande turbo åt andra hållet, eller
  vakuumdosa som inte orkar.
- **N75-styrgraden i taket samtidigt som ÄR är låg** betyder att regleringen
  försöker allt den kan men inte får ut något – mekaniskt fel, inte elektriskt.

### Grupp 013 – jämngångsreglering (cylinderavvikelse)

Läs **bara** med helt varm motor på tomgång.

- Alla fyra cylindrar ska ligga inom ungefär **±2 mg/slag**, och gärna
  betydligt närmare noll.
- **En cylinder som konstant avviker mer än ±2 mg/slag** pekar på den
  cylindern: trött insprutare, dålig kompression eller läckande
  returledning. Byt plats på insprutarna och läs om – följer avvikelsen med
  insprutaren är det den, stannar den kvar i cylindern är det mekaniskt.
- Värdena hoppar naturligt lite – titta på trenden över 30 sekunder, inte på
  ett enskilt varv.

### Om symptomen tillsammans

Svartrök + limp mode + 17965 + sotig EGR pekar mot **luftvägen**: sotigt
insug, kärvande VNT och en EGR som inte stänger. Vitrök med diesellukt pekar
mot **insprutningen**: kolla grupp 004 och grupp 013.

**Trasig MIL-lampa:** motorstyrdonet kan inte varna dig, så du är helt
beroende av att läsa felkoder manuellt. Byt lampan i kombiinstrumentet –
en aktiv kod utan lampa är lätt att missa i månader.

---

## Felsökning av programmet självt

### "Inget svar från styrdon 0x01 efter 5-baud-väckning"

1. Är tändningen PÅ? (Nyckelläge 2, motorn behöver inte gå.)
2. Är Latency Timer 1 ms? Se avsnittet ovan – det här är den absolut
   vanligaste orsaken.
3. Sitter kabeln ordentligt i OBD-uttaget?
4. Slå av tändningen, vänta 5 sekunder, slå på den igen och försök om.
   Styrdonet behöver vara i viloläge när väckningen börjar.
5. Prova en annan adress, t.ex. `--styrdon 17` (kombiinstrument), för att se
   om kabeln fungerar överhuvudtaget.
6. Har kabeln verkligen FT232RL? Kloner klarar ofta inte break-signalen som
   5-baud-init bygger på.

### "Ekofel: skickade 0xNN men fick tillbaka 0xMM"

K-line ekar tillbaka allt som skickas. Får du fel eko är det nästan alltid
hårdvara: dålig kabel, dålig kontakt, eller ett annat program som håller
porten öppen samtidigt (stäng VCDS).

### "Felaktig kvittens på byte 0xNN"

Styrdonet hann inte svara i tid, eller så tappade vi en byte. Kontrollera
latensen och prova att höja `--timeout` till 2.0. Återkommer det ständigt
är det oftast en störning på K-line eller en kabel av dålig kvalitet.

### "Blockräknaren hoppade"

Sessionen är ur synk. Programmet bryter då anslutningen med flit – anslut om.
Händer det ofta, kör med `--debug` och titta på blocktrafiken.

### Sessionen dör medan jag står i en meny

Ska inte hända – keep-alive-tråden skickar ACK-block automatiskt. Om det
ändå händer: slå av tändningen i 5 sekunder och anslut om.

### Konstiga tecken i terminalen

Programmet ställer om konsolen till UTF-8 vid start och faller tillbaka på
rena ASCII-ramar om det inte går. Använd gärna Windows Terminal framför den
gamla `cmd.exe`-rutan.

### Ingen COM-port syns

`python -m vagdiag --lista-portar` ger tom lista om pyserial saknas –
kör `pip install pyserial`.

---

## Kända begränsningar

- **Omkodning (block 0x10) är inte implementerad.** Det är för lätt att göra
  en bil obrukbar med fel kodning, och det behövs inte för felsökning.
- **Mätvärdesformler med id 22 och uppåt är rekonstruerade**, inte
  verifierade mot facit. De visas med ett `?` efter värdet. De vanliga
  (varvtal, temperatur, spänning, tryck, luftmassa i g/s) är id 1–21 och 25
  och är pålitliga. Hittar du ett värde som inte stämmer mot VCDS kan du
  rätta tabellen i `vagdiag/formler.py` – den är datadriven, och
  `formler.registrera()` finns för att skriva över en enskild formel.
- **Gruppetiketterna är ungefärliga** och gäller 1.9 TDI VP37. De skiljer sig
  mellan motorkoder och programvaruversioner. Siffervärdena är alltid rätt –
  det är namnen bredvid som är en kvalificerad gissning.
- **Elaborationskoderna i statusbyten** (den del som säger "kortslutning till
  jord" osv.) är en tolkning. Biten för sporadiskt fel (0x80) är däremot
  säker. Råvärdet visas alltid som `status 0xNN` så att du kan jämföra.
- **Ställdonens komponentkoder** har bara en liten namntabell. Okända
  ställdon visas med sin hexkod.
- **Endast motorstyrdonet har gruppetiketter.** Övriga styrdon fungerar,
  men värdena visas som "Värde 1–4".

---

## För utvecklare

### Projektstruktur

```
vagdiag/
├── undantag.py    – felhierarki med svenska meddelanden och tips
├── transport.py   – Transport-abstraktionen: serieport eller virtuell buss
├── kwp1281.py     – protokollet (byte-, block- och kommandonivå), utan UI
├── formler.py     – mätvärdesformler, datadriven tabell
├── felkoder.py    – felkodsdatabas + statusbyteavkodning
├── styrdon.py     – styrdonsadresser och gruppetiketter
├── loggning.py    – CSV-logger
├── meny.py        – terminal-UI
├── gui.py         – tkinter-instrumentpanel
├── simulator.py   – virtuell ECU
└── __main__.py    – CLI
```

### Tester

Hela protokollstacken testas mot ECU-simulatorn över en virtuell K-line-buss
i minnet. Ingen hårdvara, inga serieportar, inget com0com:

```
pip install -e ".[test]"
pytest
```

Simulatorn kan injicera fel – tappad kvittens, korrupt kvittens, tystnad,
felaktig avslutningsbyte och hoppande blockräknare – och testerna verifierar
att klienten upptäcker det, kastar rätt undantag och kan återansluta.

### Designval

- **Transport-abstraktion i stället för pty/com0com.** Simulatorn kopplas in
  direkt i minnet, vilket gör testerna OS-oberoende och snabba. Den virtuella
  bussen ekar skrivna bytes tillbaka precis som riktig K-line, så
  ekohanteringen testas på riktigt i stället för att specialfallas bort.
- **Keep-alive i bakgrundstråd.** Både terminalen och GUI:t har lägen där
  huvudtråden blockerar på inmatning; en pump i huvudloopen hade tappat
  sessionen medan användaren funderar. All blocktrafik skyddas av ett
  `RLock`, så tråden kan aldrig hamna mitt i ett annat kommando. Behöver du
  ett strikt enkeltrådat läge finns `KWP1281.keep_alive()` att anropa själv.
- **Gemensam block- och kvittenslogik** (`KWPKanal`) för både klient och
  simulator, så att testerna kör samma kod som riktig hårdvara möter.

### Licens

MIT.
