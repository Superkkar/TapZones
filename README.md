# TapZones

Tocca la scrivania, il PC esegue un'azione.

TapZones usa il microfono del tuo computer per capire **dove** stai battendo sulla scrivania (sinistra, destra, sotto… le zone le scegli tu) e lancia l'azione che hai assegnato a quella zona: aprire un sito, premere una scorciatoia, avviare un programma.

È scritto in Python ed elabora l’audio localmente in memoria, senza salvarne registrazioni. Può però aprire siti web o eseguire comandi che usano la rete, se li configuri nelle azioni.

> Progetto ispirato a [Holo](https://github.com/JustinGamer191/Holo) di JustinGamer191 (licenza MIT), che fa la stessa cosa su macOS in Swift. TapZones è una versione Python pensata soprattutto per Windows.

## Come funziona

1. **Rilevamento**: il programma impara il rumore di fondo e riconosce un colpo quando il livello sale di colpo e poi decade in fretta. Voce, musica e suoni prolungati vengono scartati.
2. **Feature**: per ogni colpo calcola alcune caratteristiche acustiche (forma dello spettro, andamento nel tempo e, se il microfono è stereo, le differenze tra i due canali), indipendenti dal volume.
3. **Classificazione**: un classificatore (LDA regolarizzata) addestrato sui tuoi campioni di calibrazione decide la zona. Se il colpo è dubbio o non assomiglia a nessuna zona, viene rifiutato invece di eseguire un'azione a caso.
4. **Azione**: viene eseguita l'azione assegnata alla zona. Se per quella zona hai definito anche un **doppio tap**, il programma aspetta un attimo un eventuale secondo colpo nella stessa zona.

## Requisiti

- Python 3.9 o superiore (consigliata una versione recente compatibile con le dipendenze)
- Un microfono, meglio se stereo o con più capsule (i microfoni a array dei portatili vanno bene)
- Windows (supporto principale), macOS e Linux (base)

Installa le dipendenze principali (presenti negli import del programma):

```bash
python -m pip install numpy sounddevice
```

Se il progetto include un `requirements.txt`, puoi invece usare `python -m pip install -r requirements.txt`. Il file non era tra gli allegati verificati.

Su macOS potrebbe essere necessario autorizzare il microfono per Terminale o per l'app con cui avvii Python; su Linux `sounddevice` può richiedere PortAudio disponibile nel sistema.

Opzionali:

```bash
python -m pip install pyautogui       # azioni "keys"
python -m pip install pystray pillow  # icona nella barra
```

## Primi passi

```bash
python tapzones.py devices                 # elenca i microfoni
python tapzones.py meter --device 1        # verifica che il microfono senta i tap
python tapzones.py calibrate --device 1    # registra le zone
python tapzones.py run                     # ascolta ed esegue le azioni
```

Per provare senza eseguire nulla:

```bash
python tapzones.py run --dry
```

### Calibrazione

```bash
python tapzones.py calibrate
python tapzones.py calibrate --zones sinistra,destra,davanti,dietro --samples 20
```

Se non indichi `--samples`, il programma ti chiede quanti tocchi registrare per ogni zona:

| Scelta | Quando usarla |
|--------|---------------|
| `10` | veloce, va bene in un ambiente silenzioso |
| `20` | consigliato (premendo solo INVIO si sceglie questo) |
| `50` | più robusto (rumore o musica in sottofondo), ma richiede più tempo |

Con `--samples N` la domanda viene saltata.

Nella calibrazione da terminale, per ogni zona premi INVIO e tocca quell’area il numero di volte richiesto, variando un po’ il punto. Alla fine vedi l’accuratezza stimata e, fino a 5 zone, la matrice di confusione; se una zona è debole puoi rifarla prima di salvare. Sono necessarie almeno **2 zone**.

#### Calibrazione guidata (finestra grafica)

```bash
python tapzones.py calibrate --gui
```

Al posto del terminale si apre una finestra con il PC al centro e le zone attorno (le zone chiamate `sinistra`, `destra`, `sotto`/`davanti`, `sopra`/`dietro` stanno dal lato giusto; gli altri nomi occupano i posti liberi). Scegli quanti tocchi per zona (10, 20 o 50: cambiando la scelta i contatori sotto ogni zona si aggiornano subito) e premi **Inizia**: la zona da toccare pulsa, a ogni tocco riconosciuto da quel lato parte un'onda luminosa che si allarga e il contatore sale; i tocchi scartati (troppo deboli, voce) li vedi segnalati e non vengono contati. Alla fine ogni zona mostra la sua accuratezza (verde, arancione o rossa): **clicca su una zona per rifare i suoi tocchi singoli** (**Ctrl+clic** per rifare i suoi doppi tocchi), poi **Salva**.

**Doppi tocchi:** con la casella «Calibra anche i doppi tocchi» attiva (di default), dopo i tocchi singoli parte una seconda fase: per ogni zona dai due colpi veloci di seguito, esattamente come li userai (5, 10 o 25 coppie secondo la scelta 10/20/50). Il programma misura la distanza tra i due colpi e la salva nel profilo, insieme ai due suoni di ogni coppia, che aiutano a riconoscere anche il secondo colpo (spesso un po' diverso dal primo). Coppie con colpi più vicini di 120 ms (eco) o più lontani di 600 ms non contano. Da questi dati `run` ricava da solo l'attesa del secondo tap e la pausa dopo ogni colpo. Se un colpo non viene contato, in basso vedi il motivo e il totale degli scartati (deboli, prolungati, ravvicinati, lenti); se il secondo colpo viene scartato, la coppia va rifatta. Togli la spunta se non usi i doppi tap.

Se un rumore o una notifica viene registrata come tocco, non serve ricominciare: usa **Annulla ultimo** (o `Ctrl+Z` / `Backspace`) oppure **Annulla ultimi 5**. L'annullamento toglie i colpi più recenti anche attraverso più zone, riapre la zona dove eri arrivato e, se avevi già finito, cancella il risultato così puoi aggiungere altri tocchi e ricalcolare. Dopo l'annullamento aspetta mezzo secondo prima di toccare di nuovo.

L’interfaccia richiede `tkinter`: la sua disponibilità dipende dall’installazione di Python (su Debian/Ubuntu, in genere `sudo apt install python3-tk`). Se manca, il codice prova a tornare alla calibrazione da terminale.

**Importante:** la ricalibrazione **riscrive** `profile.json` e non conserva gli esempi negativi aggiunti con `negatives`. Dopo una nuova calibrazione, esegui nuovamente `python tapzones.py negatives` se ne hai bisogno.

**Tieni il PC fermo** nella posizione in cui lo userai: spostarlo invalida la calibrazione.

La calibrazione viene salvata in `profile.json`.

### Azioni

Alla prima calibrazione viene creato `actions.json` con azioni di tipo `none` (quindi inizialmente nessuna azione viene eseguita). Modificalo con un editor di testo. Se nella tua copia del progetto esiste `actions.example.json`, puoi usarlo come riferimento; non era incluso negli allegati verificati:

```json
{
  "sinistra":    { "type": "url",  "value": "https://www.youtube.com" },
  "sinistra x2": { "type": "url",  "value": "https://claude.ai" },
  "destra":      { "type": "keys", "value": "playpause" },
  "destra x2":   { "type": "keys", "value": "nexttrack" },
  "sotto":       { "type": "run",  "value": "calc.exe" }
}
```

| `type` | Cosa fa |
|--------|---------|
| `none` | Niente |
| `url`  | Apre l'indirizzo nel browser predefinito |
| `run`  | Esegue un comando di shell |
| `keys` | Simula una combinazione di tasti con `pyautogui` (es. `ctrl+shift+m`); tasti multimediali come `volumeup` o `playpause` dipendono dal sistema |

Il file viene riletto **al successivo tap rilevato** quando la sua data di modifica cambia: normalmente non serve riavviare `run`. Se il JSON è invalido, il programma conserva temporaneamente le ultime azioni valide. Cambiare i nomi delle zone in `actions.json` non modifica quelli del profilo: ricalibra se vuoi cambiare le zone.

### Doppio tap

Per assegnare un'azione al doppio tap aggiungi una voce con il nome della zona seguito da ` x2` (vedi `actions.example.json`): `"sinistra x2"` scatta con due tap ravvicinati **nella stessa zona**.

- Se due tap cadono in zone diverse (es. sinistra e poi destra) valgono come due tap singoli.
- Una zona **senza** voce `x2` non ha nessuna attesa: l'azione singola parte subito. Solo le zone con una voce `x2` attiva (tipo diverso da `none`) aspettano `--double-ms` prima di eseguire il singolo, per vedere se arriva il secondo colpo. Se hai calibrato i doppi tocchi, l'attesa è quella misurata (di solito 250-400 ms); altrimenti 400 ms.
- Quando c’è almeno un doppio tap attivo il periodo di "pausa" dopo ogni colpo si riduce da 0.35 s a 0.04 s (o al valore ricavato dalla calibrazione dei doppi tocchi), per riuscire a rilevare anche il secondo colpo dei doppi veloci. Se `run` non riconosce i tuoi doppi tap, ricalibra con la casella dei doppi tocchi attiva. Se noti un singolo tap contato due volte (eco), sposta il PC su una superficie più ferma o disattiva il doppio tap con `--double-ms 0`.
- Prova con `python tapzones.py run --dry` e guarda se il terminale scrive `sinistra x2` quando fai il doppio tap.

> ⚠️ **Attenzione:** le azioni `run` vengono eseguite con `shell=True` a ogni tap riconosciuto. Metti in `actions.json` solo comandi di cui ti fidi e non usare file di azioni scaricati da altri senza controllarli.

## Icona nella barra (pensata soprattutto per Windows)

```bash
python tapzones.py tray --autostart
```

Mostra un'icona che avvia anche `run` in background:

- 🟢 verde: in ascolto
- 🟠 arancione: in pausa
- 🔴 rosso: non in esecuzione

Dopo un evento segnalato l’icona **lampeggia per circa 0,6 secondi**: 🔵 **blu** se il tap singolo è stato riconosciuto (azione eseguita), 🟣 **viola** se è stato riconosciuto un **doppio tap**, ⚪ **grigio** se è sembrato un tap ma è stato rifiutato (ambiguo, non calibrato, troppo debole), gli eventi rifiutati vengono limitati a uno ogni 3 secondi quando consecutivi; voce e suoni prolungati non fanno lampeggiare l’icona. Passando il mouse sull'icona vedi l'ultimo evento con l'orario, per esempio `ultimo: sinistra (0.62) (14:32:05)` oppure `ultimo: rifiutato: ambiguo tra zone (0.08)`: utile per capire perché un tap non fa nulla senza aprire il terminale.

Dal menu puoi avviare, **riavviare** (utile dopo aver cambiato opzioni), mettere in pausa, fermare e **calibrare**. La voce **Esci** chiude l'icona **e ferma anche `run`**, così il microfono viene rilasciato (chiudi sempre dal menu: chiudere la finestra del terminale con la X può lasciare `run` attivo in background). Eventuali opzioni extra (es. `--pause-hold 300`) vengono passate a `run`. Gli errori finiscono in `tapzones-errors.log`, che si accumula tra un avvio e l'altro (così non perdi gli errori di una calibrazione) e si azzera da solo solo quando supera i 100 KB.

**Metti in pausa o Ferma?** La pausa (o `Ctrl+Alt+P` su Windows) lascia `run` in vita ma chiude il microfono; riprendi subito con **Riprendi** e l'icona diventa arancione. **Ferma** termina del tutto `run` (icona rossa) e per ripartire serve **Avvia**.

### Calibrare dall'icona

Tasto destro sull'icona → **Calibra zone...**. TapZones ferma `run` (per liberare il microfono), apre la **calibrazione guidata** (vedi sopra) e, quando chiudi la finestra, **riavvia `run`** con il nuovo profilo. Durante la calibrazione l'icona è arancione ("calibrazione in corso").

La calibrazione riusa microfono, guadagno e nomi delle zone del profilo esistente (il microfono viene ritrovato per nome e driver, non per indice, che può cambiare), così le azioni in `actions.json` restano valide; se non c'è ancora un profilo usa le zone di default (`sinistra,destra,sotto`). Se all'avvio dell'icona hai passato `--device`, `--gain` o `--sensitivity`, vengono usati anche per la calibrazione. Per cambiare i nomi o il numero delle zone usa `python tapzones.py calibrate --gui --zones ...` da terminale. Se `tkinter` non è disponibile, su Windows si apre la vecchia calibrazione da terminale; su macOS e Linux la voce chiede di installarlo.

## Pausa automatica e manuale

Su **Windows**, per non tenere occupato il microfono durante le chiamate, TapZones lo **rilascia** quando in primo piano c’è una finestra il cui titolo contiene una di queste parole: `whatsapp, discord, zoom, microsoft teams, google meet, skype, telegram` (e per 2 minuti dopo averla lasciata).

```bash
python tapzones.py run --pause-apps "zoom,teams"   # personalizza l'elenco
python tapzones.py run --pause-apps ""             # disattiva
python tapzones.py run --pause-hold 300            # secondi di pausa extra
```

Su Windows puoi anche usare **Ctrl+Alt+P**:

- se TapZones è in ascolto, mette in pausa a mano;
- se è in pausa a mano, riprende;
- se è in pausa perché c'è un'app "da chiamata" in primo piano (o nei minuti successivi), **riprende subito**, anche se quell'app è ancora aperta. Quando passi a un'altra finestra torna il comportamento normale.

## Opzioni utili

| Opzione | Comando | Descrizione |
|---------|---------|-------------|
| `--device N` | `meter`, `calibrate`, `run`, `negatives` | Indice del microfono (vedi `devices`); `run` riprende normalmente quello del profilo |
| `--samples N` | `calibrate`, `negatives` | Tocchi per zona nella calibrazione (scelta 10, 20 o 50 se omesso); esempi negativi (default 20) |
| `--gui` | `calibrate` | Calibrazione guidata in una finestra grafica |
| `--sensitivity X` | `calibrate`, `run`, `negatives` | `>1` = più sensibile (default 1.0) |
| `--gain X` | `meter`, `calibrate`, `run`, `negatives` | Amplificazione software; `run` e `negatives` usano quella del profilo se omessa |
| `--seconds N` | `meter`, `negatives` | Durata misura `meter` (default 15 s) o limite raccolta negativi (default 40 s) |
| `--zones A,B,...` | `calibrate` | Nomi delle zone, separati da virgole (default `sinistra,destra,sotto`) |
| `--min-conf X` | `run` | Confidenza minima (0-1, default 0.15) |
| `--dry` | `run` | Non eseguire le azioni |
| `--double-ms N` | `run` | Finestra per il secondo tap in ms (se omessa: quella misurata in calibrazione, altrimenti 400; `0` = doppio tap disattivato) |
| `--pause-apps "..."` | `run` | Su Windows, titoli di app che attivano la pausa; lista separata da virgole, `""` la disattiva |
| `--pause-hold N` | `run` | Durata aggiuntiva della pausa in secondi (default 120) |
| `--autostart` | `tray` | Avvia anche `run` all’apertura dell’icona |

### Ridurre i falsi positivi

```bash
python tapzones.py negatives
```

Raccoglie esempi di suoni da ignorare e li aggiunge al profilo. Per esempio:

```bash
python tapzones.py negatives --samples 30 --seconds 60
```

Occorre aver già calibrato. Devono essere rilevati **almeno 5 esempi**; altrimenti il profilo rimane invariato. I suoni che il rilevatore iniziale scarta automaticamente non diventano esempi negativi. Il comando può migliorare il riconoscimento ma non garantisce l’eliminazione di tutti i falsi positivi.

## Problemi comuni

**`PortAudioError: Error opening InputStream: Invalid device [PaErrorCode -9996]`**

- Lo stesso microfono compare più volte in `devices`, una per ogni driver (MME, DirectSound, WASAPI, WDM-KS). Le voci **WDM-KS** spesso non si aprono: prova quelle **MME** o **WASAPI**. Per vedere il driver di ogni voce: `python -c "import sounddevice as sd; print(sd.query_devices())"`.
- Controlla che non ci sia già un `run` attivo (anche in background dall'icona) che tiene occupato il microfono: fermalo dal menu dell'icona con **Esci** o dal Task Manager (`python.exe`).
- Gli indici possono cambiare quando colleghi o scolleghi dispositivi audio: rifai `devices`.

## File generati

| File | Contenuto |
|------|-----------|
| `profile.json` | Calibrazione (solo feature numeriche, **nessun audio**), nome del microfono e tempi dei doppi tocchi |
| `actions.json` | Le tue azioni |
| `status.json` | Stato di `run`, scritto solo quando cambia (letto dall'icona) |
| `pause.flag` | Esiste solo durante la pausa manuale |
| `tapzones-calib.log` | Cronologia dell'ultima calibrazione grafica: ogni colpo accettato o scartato (con il motivo) e la distanza di ogni coppia dei doppi tocchi |
| `tapzones-errors.log` | Errori di `run` e della calibrazione avviati dall'icona (si accumula, si azzera oltre 100 KB) |

Sono dati locali potenzialmente personali. È consigliabile aggiungerli al `.gitignore` prima di pubblicare il progetto; il file `.gitignore` non era incluso negli allegati verificati.

## Privacy

L'audio viene elaborato in memoria e subito scartato: non viene salvato né inviato in rete. Nel profilo finiscono solo i vettori di feature numeriche calcolati dai tuoi colpi di calibrazione.

## Limiti noti

- La precisione dipende dal portatile, dal microfono e dalla scrivania: va calibrata per ogni setup.
- Se sposti il PC o cambi superficie, rifai la calibrazione.
- Pausa automatica per app e scorciatoia Ctrl+Alt+P funzionano solo su Windows. Su macOS/Linux `--pause-apps` non individua la finestra attiva.
- Il codice usa al massimo i primi due canali del microfono; non esiste un’opzione `--channels` nella versione esaminata.
- La calibrazione fornisce un’accuratezza **stimata**, non una garanzia per tutti gli ambienti.
- Il programma esegue comandi configurati con i permessi dell’utente: evitare azioni irreversibili o critiche.
- È un progetto sperimentale: non usarlo per azioni critiche.

## Licenza

Il README originale indica la licenza **MIT** e attribuisce l’ispirazione a [Holo](https://github.com/JustinGamer191/Holo) (MIT, © 2026 JustinGamer191 and Holo contributors). **Nota:** il file `LICENSE` non era tra gli allegati, quindi verifica che sia presente nel repository prima di pubblicare il collegamento.

## Diagnostica rapida

```bash
python tapzones.py --help
python tapzones.py calibrate --help
python tapzones.py run --help
python tapzones.py meter --seconds 10
python tapzones.py run --dry --min-conf 0.25
```

- **Non trova il microfono:** esegui `devices` e verifica indice, collegamento e permessi del microfono.
- **Riconosce la zona sbagliata:** ripeti la calibrazione con il computer nella posizione d’uso e scegli zone più distinguibili; valuta più campioni.
- **Non parte alcuna azione:** assicurati di non aver usato `--dry` e controlla `actions.json` (le azioni iniziali sono `none`). Per `keys` installa `pyautogui`.
- **Dice che TapZones è già in esecuzione:** chiudi il precedente `run` o usa il menu **Esci** dell’icona prima di riavviare.
- **Microfono occupato:** `run` tenta di riaprirlo ogni 2 secondi; chiudi altri programmi che lo bloccano.
- **Problemi con il pannello:** controlla `tapzones-errors.log`.

## Note sulla verifica

Questa documentazione è stata aggiornata confrontando `tapzones.py` con il README fornito. Non è stato effettuato un test reale con microfono o sistemi operativi diversi. File citati ma non inclusi negli allegati (`requirements.txt`, `actions.example.json`, `.gitignore`, `LICENSE`) non sono stati verificati.