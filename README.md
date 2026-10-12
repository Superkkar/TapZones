# TapZones

Tocca la scrivania, il PC esegue un'azione.

TapZones usa il microfono del tuo computer per capire **dove** stai battendo sulla scrivania (sinistra, destra, sotto… le zone le scegli tu) e lancia l'azione che hai assegnato a quella zona: aprire un sito, premere una scorciatoia, avviare un programma.

È scritto in Python, funziona tutto in locale e non registra né salva l'audio.

> Progetto ispirato a [Holo](https://github.com/JustinGamer191/Holo) di JustinGamer191 (licenza MIT), che fa la stessa cosa su macOS in Swift. TapZones è una versione Python pensata soprattutto per Windows.

## Come funziona

1. **Rilevamento**: il programma impara il rumore di fondo e riconosce un colpo quando il livello sale di colpo e poi decade in fretta. Voce, musica e suoni prolungati vengono scartati.
2. **Feature**: per ogni colpo calcola alcune caratteristiche acustiche (forma dello spettro, andamento nel tempo e, se il microfono è stereo, le differenze tra i due canali), indipendenti dal volume.
3. **Classificazione**: un classificatore (LDA regolarizzata) addestrato sui tuoi campioni di calibrazione decide la zona. Se il colpo è dubbio o non assomiglia a nessuna zona, viene rifiutato invece di eseguire un'azione a caso.
4. **Azione**: viene eseguita l'azione assegnata alla zona. Se per quella zona hai definito anche un **doppio tap**, il programma aspetta un attimo un eventuale secondo colpo nella stessa zona.

## Requisiti

- Python 3.9 o superiore
- Un microfono, meglio se stereo o con più capsule (i microfoni a array dei portatili vanno bene)
- Windows (supporto principale), macOS e Linux (base)

```bash
pip install -r requirements.txt
```

Opzionali:

```bash
pip install pyautogui          # azioni "keys"
pip install pystray pillow     # icona nella barra
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

Per ogni zona premi INVIO e tocca quell'area il numero di volte richiesto, variando un po' il punto. Alla fine vedi l'accuratezza stimata e una matrice di confusione; se una zona è debole puoi rifarla prima di salvare.

**Tieni il PC fermo** nella posizione in cui lo userai: spostarlo invalida la calibrazione.

La calibrazione viene salvata in `profile.json`.

### Azioni

Alla prima calibrazione viene creato `actions.json`. In alternativa parti da `actions.example.json`:

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
| `keys` | Preme una scorciatoia (richiede `pyautogui`), es. `ctrl+shift+m`, `volumeup` |

Il file viene riletto automaticamente quando lo modifichi: non serve riavviare `run`.

### Doppio tap

Per assegnare un'azione al doppio tap aggiungi una voce con il nome della zona seguito da ` x2` (vedi `actions.example.json`): `"sinistra x2"` scatta con due tap ravvicinati **nella stessa zona**.

- Se due tap cadono in zone diverse (es. sinistra e poi destra) valgono come due tap singoli.
- Una zona **senza** voce `x2` non ha nessuna attesa: l'azione singola parte subito. Solo le zone con un doppio tap aspettano `--double-ms` (default 400 ms) prima di eseguire il singolo, per vedere se arriva il secondo colpo.
- Quando c'è almeno un doppio tap il periodo di "pausa" dopo ogni colpo si riduce da 0.35 s a 0.10 s, per riuscire a rilevare il secondo. Se noti un singolo tap contato due volte (eco), sposta il PC su una superficie più ferma o disattiva il doppio tap con `--double-ms 0`.
- Prova con `python tapzones.py run --dry` e guarda se il terminale scrive `sinistra x2` quando fai il doppio tap.

> ⚠️ **Attenzione:** le azioni `run` vengono eseguite con `shell=True` a ogni tap riconosciuto. Metti in `actions.json` solo comandi di cui ti fidi e non usare file di azioni scaricati da altri senza controllarli.

## Icona nella barra (Windows)

```bash
python tapzones.py tray --autostart
```

Mostra un'icona che avvia anche `run` in background:

- 🟢 verde: in ascolto
- 🟠 arancione: in pausa
- 🔴 rosso: non in esecuzione

A ogni tap l'icona **lampeggia per mezzo secondo**: 🔵 **blu** se il tap è stato riconosciuto (azione eseguita), ⚪ **grigio** se è sembrato un tap ma è stato rifiutato (ambiguo, non calibrato, troppo debole), al massimo una volta ogni 3 secondi: voce e suoni prolungati non fanno lampeggiare l'icona. Passando il mouse sull'icona vedi l'ultimo evento con l'orario, per esempio `ultimo: sinistra (0.62) (14:32:05)` oppure `ultimo: rifiutato: ambiguo tra zone (0.08)`: utile per capire perché un tap non fa nulla senza aprire il terminale.

Dal menu puoi avviare, **riavviare** (utile dopo aver cambiato opzioni), mettere in pausa, fermare e **calibrare**. La voce **Esci** chiude l'icona **e ferma anche `run`**, così il microfono viene rilasciato (chiudi sempre dal menu: chiudere la finestra del terminale con la X può lasciare `run` attivo in background). Eventuali opzioni extra (es. `--pause-hold 300`) vengono passate a `run`. Gli errori finiscono in `tapzones-errors.log`, che si azzera a ogni avvio.

### Calibrare dall'icona

Tasto destro sull'icona → **Calibra zone...**. TapZones ferma `run` (per liberare il microfono), apre una finestra di terminale con la normale calibrazione e, quando la chiudi, **riavvia `run`** con il nuovo profilo. Durante la calibrazione l'icona è arancione ("calibrazione in corso").

La calibrazione riusa microfono, guadagno e nomi delle zone del profilo esistente, così le azioni in `actions.json` restano valide; se non c'è ancora un profilo usa le zone di default (`sinistra,destra,sotto`). Se all'avvio dell'icona hai passato `--device`, `--gain` o `--sensitivity`, vengono usati anche per la calibrazione. Per cambiare i nomi o il numero delle zone usa `python tapzones.py calibrate --zones ...` da terminale. Su macOS e Linux questa voce non è ancora disponibile.

## Pausa automatica e manuale

Per non tenere occupato il microfono durante le chiamate, TapZones lo **rilascia** quando in primo piano c'è una finestra il cui titolo contiene una di queste parole: `whatsapp, discord, zoom, microsoft teams, google meet, skype, telegram` (e per 2 minuti dopo averla lasciata).

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
| `--device N` | tutti | Indice del microfono (vedi `devices`) |
| `--samples N` | `calibrate` | Tocchi per zona (se omesso te lo chiede: 10, 20 o 50) |
| `--sensitivity X` | `calibrate`, `run` | `>1` = più sensibile |
| `--gain X` | `meter`, `calibrate`, `run` | Amplificazione software del segnale |
| `--min-conf X` | `run` | Confidenza minima (0-1, default 0.15) |
| `--dry` | `run` | Non eseguire le azioni |
| `--double-ms N` | `run` | Finestra per il secondo tap in ms (default 400, `0` = doppio tap disattivato) |

### Ridurre i falsi positivi

```bash
python tapzones.py negatives
```

Registra esempi di voce, tastiera e rumori da ignorare e li aggiunge al profilo.

## Problemi comuni

**`PortAudioError: Error opening InputStream: Invalid device [PaErrorCode -9996]`**

- Lo stesso microfono compare più volte in `devices`, una per ogni driver (MME, DirectSound, WASAPI, WDM-KS). Le voci **WDM-KS** spesso non si aprono: prova quelle **MME** o **WASAPI**. Per vedere il driver di ogni voce: `python -c "import sounddevice as sd; print(sd.query_devices())"`.
- Controlla che non ci sia già un `run` attivo (anche in background dall'icona) che tiene occupato il microfono: fermalo dal menu dell'icona con **Esci** o dal Task Manager (`python.exe`).
- Gli indici possono cambiare quando colleghi o scolleghi dispositivi audio: rifai `devices`.

## File generati

| File | Contenuto |
|------|-----------|
| `profile.json` | Calibrazione (solo feature numeriche, **nessun audio**) e nome del microfono |
| `actions.json` | Le tue azioni |
| `status.json` | Stato di `run`, scritto solo quando cambia (letto dall'icona) |
| `pause.flag` | Esiste solo durante la pausa manuale |
| `tapzones-errors.log` | Errori di `run` avviato dall'icona |

Sono tutti nel `.gitignore`: sono personali e legati al tuo microfono.

## Privacy

L'audio viene elaborato in memoria e subito scartato: non viene salvato né inviato in rete. Nel profilo finiscono solo i vettori di feature numeriche calcolati dai tuoi colpi di calibrazione.

## Limiti noti

- La precisione dipende dal portatile, dal microfono e dalla scrivania: va calibrata per ogni setup.
- Se sposti il PC o cambi superficie, rifai la calibrazione.
- Pausa automatica per app e scorciatoia Ctrl+Alt+P funzionano solo su Windows.
- È un progetto sperimentale: non usarlo per azioni critiche.

## Licenza

[MIT](LICENSE). Ispirato a [Holo](https://github.com/JustinGamer191/Holo) (MIT, © 2026 JustinGamer191 and Holo contributors).