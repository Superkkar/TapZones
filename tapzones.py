#!/usr/bin/env python3
"""
TapZones - tocca la scrivania, il PC esegue un'azione (Windows / macOS / Linux).

Ispirato a Holo (JustinGamer191/Holo), ma scritto in Python.

Uso:
    pip install numpy sounddevice          (opzionale: pyautogui per le scorciatoie da tastiera)
    python tapzones.py devices             # elenca i microfoni
    python tapzones.py meter --device 1    # controlla che il microfono senta i tap
    python tapzones.py calibrate           # registra le zone (default: sinistra,destra,sotto)
    python tapzones.py calibrate --gui     # calibrazione guidata in una finestra grafica
    python tapzones.py calibrate --zones sinistra,destra,davanti,dietro --samples 20
    python tapzones.py tray --autostart    # icona nella barra (verde/arancione/rosso) che avvia anche 'run'
                                           # (tasto destro > "Calibra zone..." per ricalibrare senza terminale)
    python tapzones.py negatives           # (opzionale) insegna a ignorare voce e rumori
    python tapzones.py run                 # ascolta ed esegue le azioni di actions.json
    python tapzones.py run --dry           # ascolta e stampa soltanto, senza eseguire nulla

Azioni (file actions.json, creato dopo la calibrazione):
    {"sinistra": {"type": "url",  "value": "https://youtube.com"},
     "destra":   {"type": "keys", "value": "playpause"},
     "sotto":    {"type": "run",  "value": "calc.exe"}}
    type: none | url | run | keys   (keys richiede pyautogui; es. "ctrl+shift+m", "volumeup")
Doppio tap: aggiungi una voce con il nome della zona + " x2", es. "sinistra x2" (stessa zona, due tap ravvicinati).
Se una zona non ha il doppio tap, l'azione singola parte subito, senza attesa.
ATTENZIONE: le azioni "run" vengono eseguite automaticamente a ogni tap riconosciuto.
"""
import argparse
import json
import math
import os
import queue
import signal
import subprocess
import sys
import time
import unicodedata
import webbrowser
from collections import deque
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
PROFILE = HERE / "profile.json"
ACTIONS = HERE / "actions.json"
STATUS = HERE / "status.json"        # scritto da 'run' solo quando lo stato cambia (letto dall'icona)
PAUSE_FLAG = HERE / "pause.flag"     # se esiste: pausa manuale

# ---- parametri DSP -------------------------------------------------------
BLOCK = 512            # campioni per blocco audio
WIN_MS = 90            # finestra di analisi dopo l'impulso
PRE_BLOCKS = 3         # blocchi di pre-roll conservati
LEARN_S = 1.0          # secondi iniziali per imparare il rumore ambiente
REFRACTORY_S = 0.35    # pausa dopo ogni evento
REFRACTORY_DOUBLE_S = 0.10   # pausa ridotta quando in actions.json c'e' almeno un doppio tap
DOUBLE_SUFFIX = " x2"       # chiave del doppio tap in actions.json: "sinistra x2"
ABS_MIN = 0.003        # soglia RMS minima assoluta
RATIO = 6.0            # l'impulso deve superare di N volte il rumore di fondo
SUSTAIN = 0.5          # scarta suoni che non decadono (voce, musica)
CLIP = 0.98            # scarta se saturato
WEAK = 0.01            # scarta se il picco è troppo basso
N_BANDS = 24
ENV_SEGS = 8

def rms(x):
    return float(np.sqrt(np.mean(np.square(x)))) if len(x) else 0.0

# ---- rilevamento dell'impulso -------------------------------------------
class Detector:
    def __init__(self, sr, sensitivity=1.0):
        self.sr = sr
        self.win = int(sr * WIN_MS / 1000)
        self.ratio = RATIO / sensitivity
        self.learn_left = int(LEARN_S * sr / BLOCK)
        self.floor = None
        self.hist = deque(maxlen=PRE_BLOCKS)
        self.buf = None
        self.n = 0
        self.need = 0
        self.cool = 0
        self.refractory = REFRACTORY_S

    def reset(self, cool_s=0.3):
        self.buf = None
        self.hist.clear()
        self.cool = int(cool_s * self.sr / BLOCK)

    def feed(self, block):
        """Ritorna None, oppure ('ok', finestra) / ('rej', motivo)."""
        mono = block.mean(axis=1)
        r = rms(mono) + 1e-12
        if self.buf is not None:                       # sto raccogliendo la finestra
            self.buf.append(block)
            self.n += len(block)
            return self._finish() if self.n >= self.need else None
        if self.floor is None:
            self.floor = r
        if self.learn_left > 0:                        # fase di apprendimento rumore
            self.learn_left -= 1
            self.floor = 0.9 * self.floor + 0.1 * r
            return None
        if self.cool > 0:                              # periodo refrattario
            self.cool -= 1
            if r < self.floor * 3:
                self.floor = 0.95 * self.floor + 0.05 * r
            self.hist.clear()
            return None
        if r > max(ABS_MIN, self.floor * self.ratio):  # onset!
            self.buf = list(self.hist) + [block]
            self.hist.clear()
            self.n = sum(len(b) for b in self.buf)
            self.need = self.n + self.win
            return None
        self.floor = 0.95 * self.floor + 0.05 * r
        self.hist.append(block)
        return None

    def _finish(self):
        x = np.concatenate(self.buf)
        self.buf = None
        self.cool = int(self.refractory * self.sr / BLOCK)
        self.hist.clear()
        m = x.mean(axis=1)
        peak = float(np.abs(m).max())
        if float(np.abs(x).max()) >= CLIP:
            return ("rej", "troppo forte (saturato)")
        if peak < WEAK:
            return ("rej", "troppo debole")
        idx = int(np.argmax(np.abs(m) > 0.25 * peak))   # allinea sull'inizio del colpo
        start = max(0, idx - int(0.002 * self.sr))
        w = x[start:start + self.win]
        if len(w) < self.win:
            return ("rej", "finestra incompleta")
        mw = w.mean(axis=1)
        early = rms(mw[:int(0.02 * self.sr)])
        late = rms(mw[int(0.05 * self.sr):int(0.09 * self.sr)])
        if late > SUSTAIN * early:
            return ("rej", "suono prolungato (voce/rumore?)")
        return ("ok", w)

# ---- feature acustiche ---------------------------------------------------
def features(w, sr):
    """w: array (campioni, canali). Vettore di feature indipendente dal volume."""
    ch = w.shape[1]
    m = w.mean(axis=1)
    N = 1 << (len(m) - 1).bit_length()
    freqs = np.fft.rfftfreq(N, 1 / sr)
    edges = np.geomspace(100, min(16000, sr / 2 * 0.95), N_BANDS + 1)
    centers = np.sqrt(edges[:-1] * edges[1:])

    def bands(sig):
        S = np.abs(np.fft.rfft(sig * np.hanning(len(sig)), N)) ** 2
        out = []
        for lo, hi in zip(edges[:-1], edges[1:]):
            k = (freqs >= lo) & (freqs < hi)
            out.append(np.log10(S[k].sum() + 1e-12) if k.any() else (out[-1] if out else -12.0))
        return np.array(out)

    B = bands(m)
    P = 10 ** B
    centroid = math.log10((P * centers).sum() / P.sum())
    spec = B - B.mean()                                   # forma dello spettro, non il volume

    seg = int(0.01 * sr)
    env = np.array([rms(m[i * seg:(i + 1) * seg]) for i in range(ENV_SEGS)])
    env = np.log10(env / (env.sum() + 1e-12) + 1e-3)      # forma temporale

    feats = [spec, env, [centroid]]
    if ch >= 2:                                           # differenze tra i due microfoni
        a, b = w[:, 0], w[:, 1]
        n30 = int(0.03 * sr)
        a30, b30 = a[:n30], b[:n30]
        ild = math.log10((rms(a30) + 1e-9) / (rms(b30) + 1e-9))
        M = max(1, int(sr * 0.0005))
        best, bl = -1e18, 0
        for lag in range(-M, M + 1):
            c = np.dot(a30[lag:], b30[:n30 - lag]) if lag >= 0 else np.dot(a30[:n30 + lag], b30[-lag:])
            if c > best:
                best, bl = c, lag
        coarse = (bands(a) - bands(b)).reshape(6, 4).mean(axis=1)
        feats += [[ild, bl / M], coarse]
    return np.concatenate([np.asarray(f, dtype=float).ravel() for f in feats])

# ---- classificatore: LDA regolarizzata + rifiuto dei casi dubbi -----------
class Model:
    def fit(self, X, y):
        X = np.asarray(X, float)
        y = np.asarray(y, int)
        self.k = int(y.max()) + 1
        self.mean = X.mean(0)
        sd = X.std(0)
        self.sd = np.maximum(sd, 0.2 * np.median(sd) + 1e-9)
        Z = (X - self.mean) / self.sd
        self.mus = np.array([Z[y == c].mean(0) for c in range(self.k)])
        res = Z - self.mus[y]
        Sw = res.T @ res / max(1, len(Z) - self.k)
        d = Z.shape[1]
        lam = 0.35
        S = (1 - lam) * Sw + lam * np.trace(Sw) / d * np.eye(d)
        self.P = np.linalg.inv(S)
        return self

    def dists(self, x):
        z = (np.asarray(x, float) - self.mean) / self.sd
        diff = self.mus - z
        return np.einsum("ij,jk,ik->i", diff, self.P, diff)

    def predict(self, x, ood, min_conf):
        """Ritorna (zona|None, confidenza, motivo)."""
        d = self.dists(x)
        o = np.argsort(d)
        b = int(o[0])
        if d[b] > ood:
            return None, 0.0, "non sembra un tap calibrato"
        conf = 1 - d[b] / (d[o[1]] + 1e-12) if self.k > 1 else 1.0
        if conf < min_conf:
            return None, conf, "ambiguo tra zone"
        return b, conf, ""

def leave_one_out(X, y):
    X, y = np.asarray(X, float), np.asarray(y, int)
    preds, own = [], []
    for i in range(len(X)):
        keep = np.ones(len(X), bool)
        keep[i] = False
        d = Model().fit(X[keep], y[keep]).dists(X[i])
        preds.append(int(np.argmin(d)))
        own.append(d[y[i]])
    return np.array(preds), np.array(own)

# ---- audio ---------------------------------------------------------------
class Listener:
    def __init__(self, sr, channels, device=None, sensitivity=1.0, gain=1.0):
        self.sr, self.ch, self.device, self.gain = sr, channels, device, gain
        self.sens = sensitivity
        self.q = queue.Queue()
        self.det = Detector(sr, sensitivity)
        self.refractory = REFRACTORY_S
        self.stream = None
        self.open_ch = channels

    def __enter__(self):
        return self.open()

    def open(self):
        import sounddevice as sd
        self.q = queue.Queue()                    # stato pulito a ogni (ri)apertura
        self.det = Detector(self.sr, self.sens)
        self.det.refractory = self.refractory
        maxch = sd.query_devices(self.device, "input")["max_input_channels"]
        # Alcuni driver (es. WASAPI sul Mic Array) accettano solo tutti i canali del dispositivo:
        # provo prima con i canali che servono, poi con tutti, e uso comunque solo i primi.
        tries = [self.ch] + ([maxch] if maxch > self.ch else [])
        last = None
        for n in tries:
            try:
                self.open_ch = n
                self.stream = sd.InputStream(samplerate=self.sr, channels=n, blocksize=BLOCK,
                                             device=self.device, dtype="float32", callback=self._cb)
                self.stream.start()
                return self
            except sd.PortAudioError as e:
                last, self.stream = e, None
        raise last

    def set_refractory(self, seconds):
        self.refractory = seconds
        self.det.refractory = seconds

    def __exit__(self, *exc):
        self.close()

    def close(self):
        """Rilascia il microfono (così altre app, es. WhatsApp, possono usarlo)."""
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None

    def _cb(self, indata, frames, t, status):
        x = indata[:, :self.ch] * self.gain
        self.q.put(np.clip(x, -1.0, 1.0) if self.gain != 1.0 else x.copy())

    def flush(self, cool_s=0.6):
        try:
            while True:
                self.q.get_nowait()
        except queue.Empty:
            pass
        self.det.reset(cool_s)

    def events(self, stop=lambda: False):
        while not stop():
            try:
                blk = self.q.get(timeout=0.1)
            except queue.Empty:
                continue
            ev = self.det.feed(blk)
            if ev:
                yield ev

    def settle(self, seconds=1.5):
        end = time.time() + seconds
        for _ in self.events(stop=lambda: time.time() > end):
            pass

def pick_device(device):
    import sounddevice as sd
    info = sd.query_devices(device, "input")
    ch = 2 if info["max_input_channels"] >= 2 else 1
    return int(info["default_samplerate"]), ch, info["name"]

def device_identity(index):
    """Nome + driver (host API) del dispositivo: stabili anche se gli indici cambiano."""
    import sounddevice as sd
    d = sd.query_devices(index, "input")
    return d["name"], sd.query_hostapis(d["hostapi"])["name"]

def resolve_device(index, name, api):
    """Ritrova il microfono per nome+driver; se non c'è, ripiega sull'indice salvato."""
    import sounddevice as sd
    if name:
        apis = sd.query_hostapis()
        for i, d in enumerate(sd.query_devices()):
            if d["max_input_channels"] > 0 and d["name"] == name and apis[d["hostapi"]]["name"] == api:
                return i
        sys.exit(f"Microfono «{name}» ({api}) non trovato. È collegato/attivo? "
                 "Controlla con 'python tapzones.py devices'.")
    return index

def cmd_devices(args):
    import sounddevice as sd
    for i, d in enumerate(sd.query_devices()):
        if d["max_input_channels"] > 0:
            print(f"[{i}] {d['name']}  ({d['max_input_channels']} canali, {int(d['default_samplerate'])} Hz)")

def cmd_meter(args):
    sr, ch, name = pick_device(args.device)
    print(f"Microfono: {name} | {ch} canale/i | {sr} Hz")
    print(f"Misuro il livello per {args.seconds} secondi: tocca la scrivania e guarda se il picco sale. Ctrl+C per uscire.\n")
    top = 0.0
    with Listener(sr, ch, args.device, gain=args.gain) as L:
        end = time.time() + args.seconds
        try:
            while time.time() < end:
                try:
                    blk = L.q.get(timeout=0.5)
                except queue.Empty:
                    print("\nNessun audio ricevuto dal dispositivo.")
                    continue
                p = float(np.abs(blk).max())
                top = max(top, p)
                bar = "#" * min(50, int(p * 250))
                print(f"\rpicco {p:.4f} | max {top:.4f} | {bar:<50}", end="", flush=True)
        except KeyboardInterrupt:
            pass
    print(f"\n\nPicco massimo: {top:.4f}")
    if top == 0.0:
        print("Audio completamente silenzioso: controlla Impostazioni > Privacy > Microfono (app desktop) e che il microfono non sia disattivato.")
    elif top < 0.01:
        print("Livello molto basso: alza il volume di ingresso in Windows o prova un altro dispositivo.")

# ---- schermata ASCII per la calibrazione (fallback) ----------------------
# Tutto passa da Screen.draw, che riduce ogni riga a ASCII puro: la schermata
# resta identica su qualsiasi console (anche con code page tipo cp1252/cp850).
W = 46                    # larghezza interna della cornice
BAR = 20                  # lunghezza barre
CELL = 12                 # larghezza dell'etichetta di una zona nel disegno del PC
PAD = CELL + 3            # spazio a sinistra del PC: etichetta + onda
LAPTOP = [".-------------.",
          "|             |",
          "|     PC      |",
          "|             |",
          "'-------------'"]
SIDE_BY_NAME = {"sinistra": "L", "left": "L", "destra": "R", "right": "R",
                "sotto": "B", "davanti": "B", "bottom": "B", "front": "B",
                "sopra": "T", "dietro": "T", "top": "T", "back": "T"}

def to_ascii(s):
    """Toglie accenti e simboli non ASCII: l'output della schermata resta sempre ASCII."""
    s = unicodedata.normalize("NFKD", str(s)).encode("ascii", "ignore").decode("ascii")
    return "".join(c if " " <= c < "\x7f" else " " for c in s)

def bar(v, tot, width=BAR):
    k = 0 if tot <= 0 else min(width, int(width * v / tot))
    return "#" * k + "." * (width - k)

class Screen:
    """Schermata ASCII minimale. Niente loop di refresh: ridisegna (un solo write) solo quando cambia qualcosa."""

    def __init__(self, compact=False):
        self.vt = self._enable_vt()
        if compact and sys.platform == "win32":
            os.system(f"mode con: cols={W + 4} lines=26")      # finestra piccola (console classica)
        if self.vt:
            sys.stdout.write("\x1b[2J")

    @staticmethod
    def _enable_vt():
        if sys.platform != "win32":
            return True
        try:
            import ctypes
            k = ctypes.windll.kernel32
            h = k.GetStdHandle(-11)
            m = ctypes.c_ulong()
            if not k.GetConsoleMode(h, ctypes.byref(m)):
                return False
            return bool(k.SetConsoleMode(h, m.value | 0x0004))   # ENABLE_VIRTUAL_TERMINAL_PROCESSING
        except Exception:
            return False

    def draw(self, rows):
        edge = "+" + "=" * W + "+"
        mid = "+" + "-" * W + "+"
        out = [edge]
        for r in rows:
            out.append(mid if r == "-" else "|" + to_ascii(" " + r)[:W].ljust(W) + "|")
        out.append(edge)
        txt = "\n".join(out)
        if self.vt:
            sys.stdout.write("\x1b[H" + txt.replace("\n", "\x1b[K\n") + "\x1b[K\n\x1b[J")
        else:
            os.system("cls" if sys.platform == "win32" else "clear")
            sys.stdout.write(txt + "\n")
        sys.stdout.flush()

def place_zones(names):
    """Lato del PC per ogni zona: {'L'|'R'|'T'|'B': indice}. None se le zone sono piu' di 4 (resta solo l'elenco)."""
    if len(names) > 4:
        return None
    slots = {}
    for i, nm in enumerate(names):
        s = SIDE_BY_NAME.get(nm.strip().lower())
        if s and s not in slots:
            slots[s] = i
    free = [s for s in "LRBT" if s not in slots]
    for i in range(len(names)):
        if i not in slots.values():
            slots[free.pop(0)] = i
    return slots

def _cell(i, names, counts, n, cur):
    nm = names[i][:8]
    if counts[i] >= n:
        s = f"[{nm}]"            # completata
    elif i == cur:
        s = f"*{nm}*"             # da toccare adesso
    else:
        s = nm
    return s.center(CELL)

def diagram_rows(names, counts, n, cur, slots):
    """Il PC visto dall'alto con le zone attorno; la zona in corso 'manda' onde che crescono a ogni tocco."""
    def label(side):
        i = slots.get(side)
        return " " * CELL if i is None else _cell(i, names, counts, n, cur)

    def wave(side):
        i = slots.get(side)
        if i is None or i != cur or counts[i] >= n:
            return "   "
        k = counts[i] % 3 + 1
        if side == "L":
            return (">" * k).rjust(3)
        if side == "R":
            return ("<" * k).ljust(3)
        return " ".join("v" if side == "T" else "^" for _ in range(k))

    full = PAD + len(LAPTOP[0]) + 3 + CELL          # = W - 1
    rows = []
    if "T" in slots:
        rows += [label("T").strip().center(full), wave("T").center(full)]
    for r, line in enumerate(LAPTOP):
        if r == 2:
            rows.append(label("L") + wave("L") + line + wave("R") + label("R"))
        else:
            rows.append(" " * PAD + line)
    if "B" in slots:
        rows += [wave("B").center(full), label("B").strip().center(full)]
    return rows

def zone_rows(names, counts, n, cur):
    rows = []
    for i, nm in enumerate(names):
        c = counts[i]
        tag = "OK" if c >= n else (f"{c}/{n}" if i == cur else "")
        rows.append(f"{'>' if i == cur else ' '} {i + 1} {nm[:10]:<10} [{bar(c, n)}] {tag}")
    return rows

def ask_samples(ui):
    """Chiede quanti tocchi registrare per ogni zona (10, 20 o 50)."""
    msg = ""
    while True:
        ui.draw(["TAPZONES - calibrazione", "-",
                 "Quanti tocchi per zona?", "",
                 "  [10]  veloce      (ambiente silenzioso)",
                 "  [20]  consigliato (premi solo INVIO)",
                 "  [50]  robusto     (rumore o musica)", "-",
                 msg or "Scrivi 10, 20 o 50 e premi INVIO"])
        try:
            ans = input("  > ").strip()
        except EOFError:
            return 20
        if not ans:
            return 20
        if ans in ("10", "20", "50"):
            return int(ans)
        msg = "Scelta non valida: 10, 20 o 50"

def collect_zone(L, sr, i, name, n, counts, show):
    out = []
    counts[i] = 0
    show(i, f'Zona "{name}": premi INVIO', f"poi tocca {n} volte (varia il punto)")
    input("  > ")
    L.flush(0.7)
    msg = f'Tocca la zona "{name}"'
    show(i, msg, "")
    for kind, data in L.events(stop=lambda: len(out) >= n):
        if kind == "rej":
            show(i, msg, "scartato: " + data)
            continue
        out.append(features(data, sr))
        counts[i] = len(out)
        show(i, msg, f"ok - tocco {len(out)}/{n}")
    return out

def calibration_device(args):
    """--device se c'e'; altrimenti il microfono salvato in profile.json (ritrovato per nome+driver); altrimenti None."""
    if args.device is not None:
        return args.device
    try:
        prof = json.loads(PROFILE.read_text(encoding="utf-8"))
    except Exception:
        return None
    dev = prof.get("device")
    if prof.get("device_name"):
        dev = resolve_device(dev, prof["device_name"], prof.get("device_api"))
    return dev

def cmd_calibrate(args):
    if getattr(args, "gui", False):
        return gui_calibrate(args)
    names = [z.strip() for z in args.zones.split(",") if z.strip()]
    if len(names) < 2:
        sys.exit("Servono almeno 2 zone.")
    args.device = calibration_device(args)
    ui = Screen(compact=getattr(args, "hold", False))
    if args.samples is None:
        args.samples = ask_samples(ui)
    n, k = args.samples, len(names)
    sr, ch, dev_name = pick_device(args.device)
    counts = [0] * k
    slots = place_zones(names)

    def show(cur, l1="", l2=""):
        done = sum(min(c, n) for c in counts)
        rows = ["TAPZONES - calibrazione", f"mic: {dev_name}"[:W - 1],
                f"totale [{bar(done, n * k)}] {done}/{n * k}", "-"]
        if slots:
            rows += diagram_rows(names, counts, n, cur, slots) + ["-"]
        ui.draw(rows + zone_rows(names, counts, n, cur) + ["-", l1, l2])

    samples = {}
    with Listener(sr, ch, args.device, args.sensitivity, args.gain) as L:
        show(-1, "Imparo il rumore ambiente...", "Silenzio e PC fermo un attimo")
        L.settle(1.5)
        for i, name in enumerate(names):
            samples[i] = collect_zone(L, sr, i, name, n, counts, show)
        while True:
            X = np.array([f for i in range(k) for f in samples[i]])
            y = np.array([i for i in range(k) for _ in samples[i]])
            show(-1, "Calcolo coerenza...", "")
            preds, own = leave_one_out(X, y)
            acc = [float(np.mean(preds[y == i] == i)) for i in range(k)]
            w = int(np.argmin(acc))
            hint = "Zone ben separate"
            if acc[w] < 0.8:
                wrong = preds[y == w]
                wrong = wrong[wrong != w]
                if len(wrong):
                    hint = f'"{names[w]}" si confonde con "{names[int(np.bincount(wrong).argmax())]}"'
            rows = ["TAPZONES - risultato", "-"]
            for i, nm in enumerate(names):
                flag = "+" if acc[i] >= 0.9 else ("~" if acc[i] >= 0.8 else "!")
                rows.append(f"  {i + 1} {nm[:10]:<10} [{bar(acc[i], 1)}] {acc[i] * 100:3.0f}% {flag}")
            M = np.zeros((k, k), int)
            np.add.at(M, (y, preds), 1)
            rows += ["-", f"Accuratezza stimata: {np.mean(preds == y) * 100:.0f}%", hint,
                     "+ ottima   ~ buona   ! da rifare"]
            if k <= 5:
                rows += ["-", "Scambi (riga = tocco vero, colonna = letto)",
                         " " * 14 + "".join(f"{j + 1:>4}" for j in range(k))]
                rows += [f"  {i + 1} {nm[:10]:<10}" + "".join(f"{M[i, j]:>4}" for j in range(k))
                         for i, nm in enumerate(names)]
            rows += ["-", "N. zona = rifai | INVIO = salva"]
            ui.draw(rows)
            ans = input("  > ").strip()
            if not ans:
                break
            if ans.isdigit() and 1 <= int(ans) <= k:
                i = int(ans) - 1
            elif ans in names:
                i = names.index(ans)
            else:
                continue
            samples[i] = collect_zone(L, sr, i, names[i], n, counts, show)
    ood = float(1.5 * np.percentile(own, 95))
    dname, dapi = device_identity(args.device)
    PROFILE.write_text(json.dumps({"sr": sr, "channels": ch, "device": args.device,
                                   "device_name": dname, "device_api": dapi, "gain": args.gain, "zones": names,
                                   "X": X.tolist(), "y": y.tolist(), "ood": ood}))
    msg = ["[OK] Profilo salvato.", ""]
    if not ACTIONS.exists():
        ACTIONS.write_text(json.dumps({nm: {"type": "none", "value": ""} for nm in names},
                                      indent=2, ensure_ascii=False))
        msg = ["[OK] Profilo salvato.", "[OK] Creato actions.json: assegna le azioni."]
    ui.draw(["TAPZONES - fatto", "-"] + msg)

# ---- calibrazione guidata (tkinter) --------------------------------------
def gui_calibrate(args):
    """Calibrazione guidata in una finestra grafica. Fallback automatico al terminale se tkinter manca."""
    try:
        import tkinter as tk          # noqa: F401
    except ImportError:
        # Con pythonw non c'è stdout: loggo su file, altrimenti il fallback è invisibile.
        try:
            with open(HERE / "tapzones-errors.log", "a", encoding="utf-8") as f:
                f.write("tkinter non disponibile: passo alla calibrazione da terminale\n")
        except Exception:
            pass
        print("tkinter non disponibile: passo alla calibrazione da terminale.")
        args.gui = False
        return cmd_calibrate(args)

    names = [z.strip() for z in args.zones.split(",") if z.strip()]
    if len(names) < 2:
        sys.exit("Servono almeno 2 zone.")
    args.device = calibration_device(args)
    sr, ch, dev_name = pick_device(args.device)

    args._gui_used = True             # serve a main() per non chiedere "Premi INVIO"

    import tkinter as tk
    root = tk.Tk()
    root.title("TapZones - Calibrazione")
    root.resizable(False, False)
    CalibrationWindow(root, names, sr, ch, dev_name, args)
    root.mainloop()

class CalibrationWindow:
    """Finestra di calibrazione: PC al centro, zone intorno, click per rifare."""

    LAPTOP_W, LAPTOP_H = 200, 120
    ZONE_W, ZONE_H = 132, 64

    # Palette chiara, sobria
    BG             = "#f4f4f6"
    PANEL          = "#ffffff"
    LAPTOP_FILL    = "#d8d8de"
    LAPTOP_EDGE    = "#a0a0ac"
    LAPTOP_SCREEN  = "#ececf0"
    TEXT           = "#18181b"
    TEXT_DIM       = "#71717a"
    ZONE_IDLE      = "#e8e8ec"
    ZONE_IDLE_EDGE = "#a0a0ac"
    ZONE_ACTIVE    = "#5b5bd6"
    ZONE_ACTIVE_ED = "#4343b8"
    ZONE_DONE      = "#10b981"
    ZONE_DONE_ED   = "#059669"
    ZONE_WARN      = "#f59e0b"
    ZONE_WARN_ED   = "#d97706"
    ZONE_BAD       = "#ef4444"
    ZONE_BAD_ED    = "#dc2626"
    ZONE_TEXT      = "#18181b"
    ZONE_TEXT_ON   = "#ffffff"
    PULSE          = "#6366f1"
    OK             = "#16a34a"
    REJECT         = "#ef4444"

    def __init__(self, root, names, sr, ch, dev_name, args):
        import tkinter as tk
        import threading as _t
        self._threading = _t
        self.tk = tk
        self.root = root
        self.names = names
        self.sr, self.ch = sr, ch
        self.dev_name = dev_name
        self.args = args
        self.k = len(names)
        self.n = args.samples if args.samples else 20

        self.samples = {i: [] for i in range(self.k)}
        self.history = []             # zona di ogni tocco accettato, in ordine (serve per l'annulla)
        self.counts  = [0] * self.k
        self.acc     = None
        self.active  = None
        self.started = False
        self.done_all = False

        self.event_queue = queue.Queue()
        self.stop_flag   = self._threading.Event()
        self.L           = None
        self.worker      = None

        self.zone_pos     = self._assign_positions()
        self.zone_items   = {}
        self.zone_centers = {}
        self.pulses       = []

        self._build_ui()
        self._draw_canvas()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.after(30, self._tick)

    # -- layout -----------------------------------------------------------
    def _assign_positions(self):
        used, pos = set(), {}
        for i, nm in enumerate(self.names):
            s = SIDE_BY_NAME.get(nm.strip().lower())
            if s and s not in used:
                pos[i] = s
                used.add(s)
        for i in range(self.k):
            if i not in pos:
                for s in "LRBT":
                    if s not in used:
                        pos[i] = s
                        used.add(s)
                        break
        return pos

    def _compute_layout(self):
        W = 660
        cx, cy = W // 2, 230
        slots = {"L": (100, cy), "R": (W - 100, cy),
                 "T": (cx, 90),  "B": (cx, 370)}
        centers = {i: slots[s] for i, s in self.zone_pos.items()}
        extras = [i for i in range(self.k) if i not in centers]
        H = 460
        if extras:
            rows = (len(extras) + 3) // 4
            H = 480 + rows * 90
            for j, i in enumerate(extras):
                row, col = divmod(j, 4)
                centers[i] = (100 + col * 150, 480 + row * 90)
        return W, H, (cx, cy), centers

    # -- costruzione UI ---------------------------------------------------
    def _build_ui(self):
        tk = self.tk
        self.root.configure(bg=self.BG)

        top = tk.Frame(self.root, bg=self.BG, padx=16, pady=12)
        top.pack(fill="x")
        tk.Label(top, text="TapZones - Calibrazione",
                 font=("Segoe UI", 14, "bold"), bg=self.BG, fg=self.TEXT).pack(side="left")
        tk.Label(top, text=f"Mic: {self.dev_name[:40]}",
                 font=("Segoe UI", 9), bg=self.BG, fg=self.TEXT_DIM).pack(side="right")

        ctr = tk.Frame(self.root, bg=self.BG, padx=16)
        ctr.pack(fill="x")
        tk.Label(ctr, text="Tocchi per zona:", font=("Segoe UI", 10),
                 bg=self.BG, fg=self.TEXT).pack(side="left", padx=(0, 8))
        self.samples_var = tk.IntVar(value=self.n)
        self.radio_btns = []
        for val in (10, 20, 50):
            rb = tk.Radiobutton(ctr, text=str(val), variable=self.samples_var, value=val,
                                font=("Segoe UI", 10), bg=self.BG, fg=self.TEXT,
                                activebackground=self.BG, selectcolor=self.PANEL,
                                highlightthickness=0, bd=0, command=self._on_n_change)
            rb.pack(side="left", padx=4)
            self.radio_btns.append(rb)
        self.start_btn = tk.Button(ctr, text="Inizia", command=self._on_start,
                                   font=("Segoe UI", 10, "bold"),
                                   bg=self.ZONE_ACTIVE, fg="white",
                                   activebackground=self.ZONE_ACTIVE_ED,
                                   activeforeground="white",
                                   relief="flat", padx=20, pady=4, cursor="hand2", borderwidth=0)
        self.start_btn.pack(side="right")

        W, H, laptop, centers = self._compute_layout()
        self.canvas = tk.Canvas(self.root, width=W, height=H, bg=self.BG,
                                highlightthickness=0, bd=0)
        self.canvas.pack(fill="both", expand=True)
        self.laptop_center = laptop
        self.zone_centers  = centers

        undo_row = tk.Frame(self.root, bg=self.BG, padx=16)
        undo_row.pack(fill="x")
        self.undo_btns = []
        for label, cnt in (("Annulla ultimo", 1), ("Annulla ultimi 5", 5)):
            b = tk.Button(undo_row, text=label, command=lambda c=cnt: self._undo(c),
                          font=("Segoe UI", 9), bg=self.ZONE_IDLE, fg=self.TEXT,
                          activebackground=self.ZONE_IDLE_EDGE, relief="flat",
                          padx=12, pady=2, cursor="hand2", borderwidth=0, state="disabled")
            b.pack(side="left", padx=(0, 8))
            self.undo_btns.append(b)
        tk.Label(undo_row, text="(Ctrl+Z / Backspace = annulla ultimo)",
                 font=("Segoe UI", 8), bg=self.BG, fg=self.TEXT_DIM).pack(side="left")
        self.root.bind("<Control-z>", lambda e: self._undo(1))
        self.root.bind("<BackSpace>", lambda e: self._undo(1))

        bot = tk.Frame(self.root, bg=self.BG, padx=16, pady=10)
        bot.pack(fill="x")
        self.status_lbl = tk.Label(bot, text="Scegli i tocchi e premi Inizia.",
                                   font=("Segoe UI", 10), bg=self.BG, fg=self.TEXT_DIM,
                                   anchor="w", justify="left")
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self.save_btn = tk.Button(bot, text="Salva", command=self._on_save,
                                  font=("Segoe UI", 10, "bold"),
                                  bg=self.ZONE_DONE, fg="white",
                                  activebackground=self.ZONE_DONE_ED, activeforeground="white",
                                  relief="flat", padx=20, pady=4, cursor="hand2",
                                  borderwidth=0, state="disabled")
        self.save_btn.pack(side="right")

    def _draw_canvas(self):
        c = self.canvas
        lx, ly = self.laptop_center
        lw, lh = self.LAPTOP_W, self.LAPTOP_H
        c.create_rectangle(lx - lw / 2, ly - lh / 2, lx + lw / 2, ly + lh / 2,
                           fill=self.LAPTOP_FILL, outline=self.LAPTOP_EDGE, width=2)
        m = 14
        c.create_rectangle(lx - lw / 2 + m, ly - lh / 2 + m, lx + lw / 2 - m, ly + lh / 2 - m,
                           fill=self.LAPTOP_SCREEN, outline=self.LAPTOP_EDGE, width=1)
        c.create_text(lx, ly, text="PC", font=("Segoe UI", 16, "bold"), fill=self.TEXT_DIM)

        for i in range(self.k):
            cx, cy = self.zone_centers[i]
            zw, zh = self.ZONE_W, self.ZONE_H
            x1, y1, x2, y2 = cx - zw / 2, cy - zh / 2, cx + zw / 2, cy + zh / 2
            r = 10
            pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r,
                   x2, y2 - r, x2, y2, x2 - r, y2, x1 + r, y2,
                   x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
            tag = f"zone_{i}"
            rect = c.create_polygon(pts, smooth=True, fill=self.ZONE_IDLE,
                                    outline=self.ZONE_IDLE_EDGE, width=2, tags=(tag,))
            txt = c.create_text(cx, cy - 7, text=self.names[i][:14],
                                font=("Segoe UI", 11, "bold"),
                                fill=self.ZONE_TEXT, tags=(tag,))
            cnt = c.create_text(cx, cy + 14, text=f"0/{self.n}",
                                font=("Segoe UI", 9), fill=self.TEXT_DIM, tags=(tag,))
            self.zone_items[i] = {"rect": rect, "text": txt, "count": cnt}
            c.tag_bind(tag, "<Button-1>", lambda e, idx=i: self._on_zone_click(idx))
            c.tag_bind(tag, "<Enter>", lambda e: c.configure(cursor="hand2"))
            c.tag_bind(tag, "<Leave>", lambda e: c.configure(cursor=""))

    # -- stile / redraw ---------------------------------------------------
    def _zone_style(self, i):
        if self.acc is not None and self.counts[i] >= self.n:
            a = self.acc[i]
            if a >= 0.9:   return self.ZONE_DONE, self.ZONE_DONE_ED, self.ZONE_TEXT_ON
            if a >= 0.8:   return self.ZONE_WARN, self.ZONE_WARN_ED, self.ZONE_TEXT_ON
            return              self.ZONE_BAD,  self.ZONE_BAD_ED,  self.ZONE_TEXT_ON
        if i == self.active:
            return self.ZONE_ACTIVE, self.ZONE_ACTIVE_ED, self.ZONE_TEXT_ON
        if self.counts[i] >= self.n:
            return self.ZONE_DONE, self.ZONE_DONE_ED, self.ZONE_TEXT_ON
        return self.ZONE_IDLE, self.ZONE_IDLE_EDGE, self.ZONE_TEXT

    def _redraw_zone(self, i):
        fill, edge, tf = self._zone_style(i)
        it = self.zone_items[i]
        self.canvas.itemconfig(it["rect"], fill=fill, outline=edge)
        self.canvas.itemconfig(it["text"], fill=tf)
        t = f"{self.counts[i]}/{self.n}"
        if self.acc is not None and self.counts[i] >= self.n:
            t += f"  ({self.acc[i] * 100:.0f}%)"
        self.canvas.itemconfig(it["count"], text=t,
                               fill=tf if fill != self.ZONE_IDLE else self.TEXT_DIM)

    def _redraw_all(self):
        for i in range(self.k):
            self._redraw_zone(i)

    # -- flusso di calibrazione ------------------------------------------
    def _on_start(self):
        if self.started:
            return
        self.n = self.samples_var.get()
        self.started = True
        for rb in self.radio_btns:
            rb.configure(state="disabled")
        self.start_btn.configure(state="disabled", bg="#a0a0ac")
        if not self._start_audio():
            self.started = False
            self.start_btn.configure(state="normal", bg=self.ZONE_ACTIVE)
            for rb in self.radio_btns:
                rb.configure(state="normal")
            return
        self._set_active(self._next_pending())
        self._redraw_all()
        if self.active is not None:
            self._set_status(f"Tocca '{self.names[self.active]}' {self.n} volte (varia un po').")

    def _next_pending(self):
        for i in range(self.k):
            if self.counts[i] < self.n:
                return i
        return None

    def _set_active(self, i):
        old = self.active
        if old is not None and old != i:
            self.canvas.itemconfig(self.zone_items[old]["rect"], width=2)
        self.active = i

    def _start_audio(self):
        try:
            self.L = Listener(self.sr, self.ch, self.args.device,
                              self.args.sensitivity, self.args.gain)
            self.L.open()
        except Exception as e:
            self._set_status(f"Errore microfono: {e}", error=True)
            return False
        self.worker = self._threading.Thread(target=self._audio_loop, daemon=True)
        self.worker.start()
        self._set_status("Imparo il rumore ambiente... 2 secondi di silenzio.")
        return True

    def _audio_loop(self):
        # Precalcolo le feature nel thread audio: la UI resta reattiva.
        try:
            self.L.settle(1.5)
        except Exception:
            pass
        while not self.stop_flag.is_set():
            try:
                for kind, data in self.L.events(stop=lambda: self.stop_flag.is_set()):
                    if kind == "ok":
                        try:
                            self.event_queue.put(("ok", features(data, self.sr)))
                        except Exception:
                            pass
                    else:
                        self.event_queue.put(("rej", data))
            except Exception:
                break

    def _tick(self):
        try:
            while True:
                kind, data = self.event_queue.get_nowait()
                self._handle_event(kind, data)
        except queue.Empty:
            pass
        self._animate_pulses()
        self._animate_active()
        self.root.after(30, self._tick)

    def _handle_event(self, kind, data):
        if kind == "rej":
            self._set_status(f"Scartato: {data}", dim=True)
            return
        if self.active is None:
            return
        i = self.active
        self.samples[i].append(data)
        self.counts[i] += 1
        self.history.append(i)
        self._update_undo_state()
        self._redraw_zone(i)
        self._start_pulse(i)
        if self.counts[i] >= self.n:
            nxt = self._next_pending()
            self._set_active(nxt)
            if nxt is None:
                self._finish_all()
            else:
                self._set_status(f"Zona '{self.names[i]}' ok. Ora tocca '{self.names[nxt]}'.")
                self._redraw_zone(nxt)

    def _finish_all(self):
        X = np.array([f for i in range(self.k) for f in self.samples[i]])
        y = np.array([i for i in range(self.k) for _ in self.samples[i]])
        preds, own = leave_one_out(X, y)
        self.acc = [float(np.mean(preds[y == i] == i)) for i in range(self.k)]
        self._redraw_all()
        overall = float(np.mean(preds == y))
        msg = f"Fine. Accuratezza stimata: {overall * 100:.0f}%."
        worst = int(np.argmin(self.acc))
        if self.acc[worst] < 0.8:
            wrong = preds[y == worst]
            wrong = wrong[wrong != worst]
            if len(wrong):
                other = self.names[int(np.bincount(wrong).argmax())]
                msg += f"  '{self.names[worst]}' si confonde con '{other}' - clicca la zona per rifarla."
        self._set_status(msg, ok=(overall >= 0.85))
        self._X, self._y, self._own = X, y, own
        self.done_all = True
        self.save_btn.configure(state="normal")

    def _on_zone_click(self, i):
        if not self.started:
            return
        self.samples[i] = []
        self.counts[i] = 0
        self.history = [z for z in self.history if z != i]
        self._update_undo_state()
        self.acc = None
        self.done_all = False
        self.save_btn.configure(state="disabled")
        try:
            while True:
                self.event_queue.get_nowait()
        except queue.Empty:
            pass
        if self.L is not None:
            try:
                self.L.flush(0.4)
            except Exception:
                pass
        self._set_active(i)
        self._redraw_all()
        self._set_status(f"Rifai '{self.names[i]}': tocca {self.n} volte.")

    def _on_n_change(self):
        """Cambio 10/20/50 prima di iniziare: aggiorna subito i contatori 0/N sotto ogni zona."""
        if self.started:
            return
        self.n = self.samples_var.get()
        self._redraw_all()

    def _update_undo_state(self):
        st = "normal" if (self.started and self.history) else "disabled"
        for b in getattr(self, "undo_btns", []):
            b.configure(state=st)

    def _undo(self, count=1):
        """Toglie gli ultimi `count` tocchi (anche attraverso più zone) senza rifare da capo."""
        if not self.started or not self.history:
            return
        zone = None
        for _ in range(min(count, len(self.history))):
            zone = self.history.pop()
            self.samples[zone].pop()
            self.counts[zone] -= 1
        # il calcolo finale non vale più: si riparte dalla zona dell'ultimo tocco tolto
        self.acc = None
        self.done_all = False
        self.save_btn.configure(state="disabled")
        try:
            while True:
                self.event_queue.get_nowait()
        except queue.Empty:
            pass
        if self.L is not None:
            try:
                self.L.flush(0.4)
            except Exception:
                pass
        self._set_active(zone)
        self._redraw_all()
        self._update_undo_state()
        self._set_status(f"Annullato. Continua con '{self.names[zone]}': "
                         f"{self.counts[zone]}/{self.n}.")

    def _on_save(self):
        if not self.done_all:
            return
        ood = float(1.5 * np.percentile(self._own, 95))
        dname, dapi = device_identity(self.args.device)
        PROFILE.write_text(json.dumps({
            "sr": self.sr, "channels": self.ch, "device": self.args.device,
            "device_name": dname, "device_api": dapi,
            "gain": self.args.gain, "zones": self.names,
            "X": self._X.tolist(), "y": self._y.tolist(), "ood": ood,
        }))
        if not ACTIONS.exists():
            ACTIONS.write_text(json.dumps(
                {nm: {"type": "none", "value": ""} for nm in self.names},
                indent=2, ensure_ascii=False))
        self._set_status("[OK] Profilo salvato.", ok=True)
        self.root.after(700, self._on_close)

    # -- animazioni --------------------------------------------------------
    def _start_pulse(self, i):
        x, y = self.zone_centers[i]
        item = self.canvas.create_oval(x - 8, y - 8, x + 8, y + 8,
                                       outline=self.PULSE, width=4, fill="")
        self.pulses.append({"item": item, "t0": time.time(), "x": x, "y": y})

    def _animate_pulses(self):
        if not self.pulses:
            return
        now = time.time()
        alive = []
        for p in self.pulses:
            dt = now - p["t0"]
            if dt > 0.55:
                self.canvas.delete(p["item"])
                continue
            k = dt / 0.55
            r = 10 + k * 70
            self.canvas.coords(p["item"], p["x"] - r, p["y"] - r, p["x"] + r, p["y"] + r)
            w = max(1, int(round(4 * (1 - k))))
            self.canvas.itemconfig(p["item"], width=w)
            alive.append(p)
        self.pulses = alive

    def _animate_active(self):
        if self.active is None:
            return
        # leggero pulsare del bordo della zona attiva
        w = 2 + int(round(1.5 + 1.5 * math.sin(time.time() * 4)))
        self.canvas.itemconfig(self.zone_items[self.active]["rect"], width=w)

    # -- utility ----------------------------------------------------------
    def _set_status(self, text, error=False, ok=False, dim=False):
        color = self.TEXT_DIM if dim else (self.REJECT if error else (self.OK if ok else self.TEXT))
        self.status_lbl.configure(text=text, fg=color)

    def _on_close(self):
        self.stop_flag.set()
        if self.L is not None:
            try:
                self.L.close()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

# ---- esecuzione ----------------------------------------------------------
def do_action(a):
    t, v = a.get("type", "none"), a.get("value", "")
    if t == "url":
        webbrowser.open(v)
    elif t == "run":
        subprocess.Popen(v, shell=True)
    elif t == "keys":
        try:
            import pyautogui
        except ImportError:
            print("   (per le scorciatoie installa: pip install pyautogui)")
            return
        pyautogui.hotkey(*[k.strip() for k in v.split("+")])

def profile_device(prof, args):
    dev = args.device if args.device is not None else prof.get("device")
    if args.device is None and prof.get("device_name"):
        dev = resolve_device(dev, prof["device_name"], prof.get("device_api"))
    elif not prof.get("device_name") and dev is not None:
        # vecchio profilo senza nome: lo fisso ora (verifica che il nome stampato sia quello giusto)
        prof["device_name"], prof["device_api"] = device_identity(dev)
        PROFILE.write_text(json.dumps(prof))
        print(f"Microfono fissato nel profilo: {prof['device_name']} ({prof['device_api']})")
    return dev

def collect_negatives(L, sr, n=20, max_s=40):
    """Raccoglie i suoni che superano il filtro ma NON sono tap (voce, tasti, tosse...)."""
    input("\nPremi INVIO, poi per qualche decina di secondi parla normalmente, "
          "scrivi sulla tastiera, tossisci, tocca il portatile... ")
    L.flush(0.5)
    out, end = [], time.time() + max_s
    for kind, data in L.events(stop=lambda: len(out) >= n or time.time() > end):
        if kind == "ok":
            out.append(features(data, sr))
            print(f"   + esempio negativo {len(out)}/{n}")
    return out

def cmd_negatives(args):
    if not PROFILE.exists():
        sys.exit("Nessun profilo: esegui prima 'python tapzones.py calibrate'.")
    prof = json.loads(PROFILE.read_text())
    names, sr, ch = prof["zones"], prof["sr"], prof["channels"]
    dev = profile_device(prof, args)
    gain = args.gain if args.gain is not None else prof.get("gain", 1.0)
    with Listener(sr, ch, dev, args.sensitivity, gain) as L:
        L.settle(1.5)
        neg = collect_negatives(L, sr, args.samples, args.seconds)
    if len(neg) < 5:
        print(f"\nSolo {len(neg)} esempi raccolti: troppo pochi, profilo non modificato. "
              "Il filtro scarta già quasi tutto il parlato: probabilmente non serve altro.")
        return
    k = len(names)
    X = np.array(prof["X"] + [list(v) for v in neg])
    y = np.array(prof["y"] + [k] * len(neg))
    preds, own = leave_one_out(X, y)
    zmask = y < k
    print(f"\nEsempi negativi totali: {int((y == k).sum())}")
    print(f"Tap riconosciuti correttamente (leave-one-out): {np.mean(preds[zmask] == y[zmask]) * 100:.0f}%")
    print(f"Voce/rumori riconosciuti come tali:             {np.mean(preds[~zmask] == k) * 100:.0f}%")
    prof["X"], prof["y"] = X.tolist(), y.tolist()
    prof["ood"] = float(1.5 * np.percentile(own[zmask], 95))
    PROFILE.write_text(json.dumps(prof))
    print("Profilo aggiornato. Ora i suoni simili ai negativi vengono ignorati da 'run'.")

_CUR_STATE = None        # ultimo stato scritto da questo processo
_LAST_EVENT = None       # ultimo tap visto da 'run' (l'icona lo mostra con un lampeggio)

def write_status(state, reason=""):
    global _CUR_STATE
    _CUR_STATE = (state, reason)
    d = {"state": state, "reason": reason, "pid": os.getpid(), "ts": time.time()}
    if _LAST_EVENT:
        d["event"] = _LAST_EVENT
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, ensure_ascii=False))
    os.replace(tmp, STATUS)

def note_event(kind, text):
    """Registra l'ultimo tap in status.json. kind: 'ok' (azione eseguita) o 'rej' (rifiutato/ignorato)."""
    global _LAST_EVENT
    now = time.time()
    if kind == "rej" and _LAST_EVENT and _LAST_EVENT["kind"] == "rej" and now - _LAST_EVENT["ts"] < 3:
        return                                   # con musica/film non deve lampeggiare di continuo
    _LAST_EVENT = {"kind": kind, "text": text, "ts": now}
    if _CUR_STATE:
        write_status(*_CUR_STATE)

def pid_alive(pid):
    """True se il processo con questo PID esiste ancora."""
    if not pid:
        return False
    if sys.platform == "win32":
        import ctypes
        k = ctypes.windll.kernel32
        h = k.OpenProcess(0x1000, False, int(pid))        # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            ok = k.GetExitCodeProcess(h, ctypes.byref(code))
            return bool(ok) and code.value == 259         # STILL_ACTIVE
        finally:
            k.CloseHandle(h)
    try:
        os.kill(int(pid), 0)
        return True
    except PermissionError:
        return True
    except OSError:
        return False

def read_status():
    """Stato di 'run' se è vivo, altrimenti None (il file viene scritto solo ai cambi di stato)."""
    if not STATUS.exists():
        return None
    d = None
    for _ in range(3):                  # su Windows la lettura può cadere proprio mentre il file viene sostituito
        try:
            d = json.loads(STATUS.read_text())
            break
        except Exception:
            time.sleep(0.02)
    if d is None:
        return None
    if d.get("state") == "stopped" or not pid_alive(d.get("pid")):
        return None
    return d

def toggle_pause_flag():
    if PAUSE_FLAG.exists():
        PAUSE_FLAG.unlink()
    else:
        PAUSE_FLAG.write_text("1")

def foreground_title():
    """Titolo della finestra in primo piano (solo Windows)."""
    if sys.platform != "win32":
        return ""
    import ctypes
    u = ctypes.windll.user32
    h = u.GetForegroundWindow()
    n = u.GetWindowTextLengthW(h)
    buf = ctypes.create_unicode_buffer(n + 1)
    u.GetWindowTextW(h, buf, n + 1)
    return buf.value

def start_hotkey(toggle):
    """Scorciatoia globale Ctrl+Alt+P: pausa/riprendi manuale (solo Windows)."""
    if sys.platform != "win32":
        return False
    import ctypes
    import ctypes.wintypes as wt
    import threading

    def loop():
        u = ctypes.windll.user32
        # MOD_ALT=1, MOD_CONTROL=2, MOD_NOREPEAT=0x4000, tasto 'P'=0x50
        if not u.RegisterHotKey(None, 1, 0x0001 | 0x0002 | 0x4000, 0x50):
            print("  (scorciatoia Ctrl+Alt+P non disponibile: già usata da un'altra app?)")
            return
        msg = wt.MSG()
        while u.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
            if msg.message == 0x0312:             # WM_HOTKEY
                toggle()

    threading.Thread(target=loop, daemon=True).start()
    return True

def cmd_run(args):
    if not PROFILE.exists():
        sys.exit("Nessun profilo: esegui prima 'python tapzones.py calibrate'.")
    prof = json.loads(PROFILE.read_text())
    names, sr, ch = prof["zones"], prof["sr"], prof["channels"]
    model = Model().fit(prof["X"], prof["y"])
    actions, actions_mtime = {}, 0.0
    window = max(0, args.double_ms) / 1000.0      # finestra per il secondo tap (0 = doppio tap disattivato)
    L = None                                      # il Listener viene creato piu' sotto

    def wants_fast():
        """True se c'e' almeno un doppio tap con un'azione: serve il periodo refrattario ridotto."""
        return window > 0 and any(k.endswith(DOUBLE_SUFFIX) and isinstance(v, dict)
                                  and v.get("type", "none") != "none" for k, v in actions.items())

    def load_actions():
        """Rilegge actions.json solo se è cambiato (controllo fatto solo quando arriva un tap)."""
        nonlocal actions, actions_mtime
        try:
            mt = ACTIONS.stat().st_mtime
        except OSError:
            return
        if mt != actions_mtime:
            try:
                actions = json.loads(ACTIONS.read_text(encoding="utf-8"))
                actions_mtime = mt
                if L is not None:
                    L.set_refractory(REFRACTORY_DOUBLE_S if wants_fast() else REFRACTORY_S)
            except Exception as e:
                print(f"  (actions.json non leggibile, tengo la versione precedente: {e})")

    load_actions()
    dev = profile_device(prof, args)
    gain = args.gain if args.gain is not None else prof.get("gain", 1.0)
    keywords = [k.strip().lower() for k in args.pause_apps.split(",") if k.strip()]

    st = read_status()
    if st and st.get("pid") != os.getpid():
        sys.exit(f"TapZones è già in esecuzione (PID {st['pid']}). Chiudilo prima di avviarne un altro.")
    PAUSE_FLAG.unlink(missing_ok=True)

    # Stato condiviso con la scorciatoia (che gira in un altro thread):
    #   app_paused = in questo momento sono in pausa per un'app "da chiamata" (o per il tempo di attesa dopo);
    #   override   = l'utente ha forzato la ripresa: ignoro la pausa per app finché quell'app resta in primo piano.
    ps = {"app_paused": False, "override": False}

    def on_hotkey():
        """Ctrl+Alt+P: se sono in pausa (manuale o per app) riprende subito, altrimenti mette in pausa."""
        was_manual = PAUSE_FLAG.exists()
        if was_manual:
            PAUSE_FLAG.unlink(missing_ok=True)
        if ps["app_paused"]:
            ps["override"] = True                 # riprendo anche se l'app è ancora in primo piano
        elif not was_manual:
            PAUSE_FLAG.write_text("1")            # non ero in pausa: pausa manuale

    hotkey = start_hotkey(on_hotkey)

    def stamp():
        return time.strftime("%H:%M:%S")

    pending = None                                # primo tap in attesa di un eventuale secondo

    def has_double(name):
        a = actions.get(name + DOUBLE_SUFFIX)
        return window > 0 and isinstance(a, dict) and a.get("type", "none") != "none"

    def fire(name, conf, ms=0.0, double=False):
        label = name + DOUBLE_SUFFIX if double else name
        act = actions.get(label, {"type": "none"})
        print(f"  ✔ {label}  conf {conf:.2f}  ({ms:.0f} ms)  -> {act.get('type', 'none')}")
        note_event("ok", f"{label} ({conf:.2f})")
        if not args.dry:
            do_action(act)

    def flush_pending():
        """Il secondo tap non e' arrivato (o e' arrivato altrove): il primo vale come tap singolo."""
        nonlocal pending
        if pending is not None:
            p, pending = pending, None
            fire(p["name"], p["conf"], p["ms"])

    def pending_due():
        return pending is not None and time.time() - pending["ts"] > window

    def handle(kind, data):
        nonlocal pending
        if kind == "rej":
            print(f"  · ignorato: {data}")
            if "debole" in data:                 # solo i tap mancati; suoni prolungati/voce non fanno lampeggiare
                note_event("rej", f"ignorato: {data}")
            return
        t0 = time.perf_counter()
        zone, conf, why = model.predict(features(data, sr), prof["ood"], args.min_conf)
        ms = (time.perf_counter() - t0) * 1000
        if zone is None:
            print(f"  ? rifiutato: {why} (conf {conf:.2f})")
            note_event("rej", f"rifiutato: {why} ({conf:.2f})")
        elif zone >= len(names):
            print(f"  · scartato: sembra voce/rumore (conf {conf:.2f})")
        else:
            name = names[zone]
            load_actions()
            now = time.time()
            if pending is not None and pending["name"] == name and now - pending["ts"] <= window:
                pending = None
                fire(name, conf, ms, double=True)      # stessa zona entro la finestra: doppio tap
                return
            flush_pending()                            # altra zona o troppo tardi: il primo era un singolo
            if has_double(name):
                pending = {"name": name, "conf": conf, "ms": ms, "ts": now}
                print(f"  … {name}: attendo un secondo tap ({args.double_ms} ms)")
            else:
                fire(name, conf, ms)                   # nessun doppio per questa zona: parte subito

    last_app = 0.0                                # ultimo istante in cui un'app "da chiamata" era in primo piano

    def pause_reason():
        nonlocal last_app
        title = foreground_title().lower()
        hit = next((k for k in keywords if k in title), None)
        if hit:
            last_app = time.time()
            app_reason = f"app «{hit}» in primo piano (Ctrl+Alt+P per riprendere subito)"
        elif keywords and time.time() - last_app < args.pause_hold:
            app_reason = "da poco in un'app di chiamata (Ctrl+Alt+P per riprendere subito)"
        else:
            app_reason = None
        if ps["override"]:
            if hit is None:                       # ho lasciato l'app: torno al comportamento normale
                ps["override"] = False
                last_app = 0.0                    # e niente tempo di attesa extra
            app_reason = None
        ps["app_paused"] = app_reason is not None
        if PAUSE_FLAG.exists():
            return "pausa manuale (Ctrl+Alt+P per riprendere)"
        return app_reason

    print("In ascolto... (Ctrl+C per uscire)" + ("  [DRY RUN: nessuna azione]" if args.dry else ""))
    print(f"Pausa automatica con: {', '.join(keywords) if keywords else 'disattivata'}"
          + ("  |  Ctrl+Alt+P = pausa/riprendi a mano" if hotkey else ""))
    L = Listener(sr, ch, dev, args.sensitivity, gain)
    L.set_refractory(REFRACTORY_DOUBLE_S if wants_fast() else REFRACTORY_S)
    is_open, warned_busy = False, False
    last_state = None

    def tick(state, why=""):
        """Scrive status.json solo quando stato o motivo cambiano."""
        nonlocal last_state
        if (state, why) != last_state:
            write_status(state, why)
            last_state = (state, why)

    print(f"[{stamp()}] run avviato (PID {os.getpid()}, microfono: {prof.get('device_name', dev)})")
    try:
        while True:
            reason = pause_reason()
            if reason and is_open:
                L.close()
                is_open = False
                pending = None
                print(f"[{stamp()}] ⏸  microfono rilasciato: {reason}")
            elif not reason and not is_open:
                try:
                    L.open()
                except Exception as e:
                    L.close()
                    if not warned_busy:
                        print(f"[{stamp()}] microfono non disponibile (occupato da un'altra app?): "
                              f"riprovo ogni 2 s... ({e})")
                        warned_busy = True
                    tick("paused", "microfono occupato da un'altra app")
                    time.sleep(2)
                    continue
                is_open, warned_busy = True, False
                L.settle(1.5)
                print(f"[{stamp()}] ▶  in ascolto")
            if not is_open:
                tick("paused", reason or "in pausa")
                time.sleep(0.3)
                continue
            tick("listening")
            deadline = time.time() + 1.0          # titolo finestra / flag pausa controllati ~1 volta al secondo
            for kind, data in L.events(stop=lambda: time.time() > deadline or pending_due()):
                try:
                    handle(kind, data)
                except Exception:
                    import traceback
                    traceback.print_exc()
            if pending_due():                     # nessun secondo tap: esegui il singolo
                try:
                    flush_pending()
                except Exception:
                    import traceback
                    traceback.print_exc()
    except KeyboardInterrupt:
        print("\nChiuso.")
    except Exception:
        import traceback
        traceback.print_exc()
        raise
    finally:
        L.close()
        try:
            write_status("stopped")
        except Exception:
            pass

def _log(msg):
    """Riga nel log degli errori (usato dall'icona, che gira senza console)."""
    try:
        with open(HERE / "tapzones-errors.log", "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
    except Exception:
        pass

def calibrate_args(extra):
    """Argomenti per 'calibrate' lanciato dall'icona.

    Dalle opzioni extra del tray tiene solo quelle valide anche per la calibrazione
    (--device, --sensitivity, --gain); per il resto riusa microfono, guadagno e nomi
    delle zone del profilo esistente, cosi' le azioni in actions.json restano valide.
    """
    keep, i = [], 0
    while i < len(extra):
        a = extra[i]
        if a.split("=", 1)[0] in ("--device", "--sensitivity", "--gain"):
            if "=" in a:
                keep.append(a)
                i += 1
                continue
            if i + 1 < len(extra):
                keep += [a, extra[i + 1]]
                i += 2
                continue
        i += 1
    try:
        prof = json.loads(PROFILE.read_text(encoding="utf-8"))
    except Exception:
        prof = {}
    # Se il profilo ha nome+driver del microfono, NON passo l'indice: calibration_device()
    # lo ritrova per nome (gli indici possono cambiare tra un avvio e l'altro).
    if (not any(k.startswith("--device") for k in keep) and prof.get("device") is not None
            and not prof.get("device_name")):
        keep += ["--device", str(prof["device"])]
    if not any(k.startswith("--gain") for k in keep) and prof.get("gain"):
        keep += ["--gain", str(prof["gain"])]
    if prof.get("zones"):
        keep += ["--zones", ",".join(prof["zones"])]
    return keep

def cmd_tray(args, extra):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit("Servono due pacchetti: pip install pystray pillow")
    import threading
    colors = {"green": (34, 197, 94), "orange": (245, 158, 11), "red": (239, 68, 68),
              "blue": (59, 130, 246), "gray": (148, 163, 184)}
    FLASH_S = 0.6

    def make(rgb):
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse((6, 6, 58, 58), fill=rgb + (255,))
        return im

    imgs = {k: make(v) for k, v in colors.items()}
    calib = {"on": False}

    def has_tk():
        try:
            import tkinter  # noqa: F401
            return True
        except Exception:
            return False

    def state():
        if calib["on"]:
            return "orange", "calibrazione in corso"
        st = read_status()
        if st is None:
            return "red", "non in esecuzione"
        if st["state"] == "listening":
            ev = st.get("event")
            text = "in ascolto"
            if ev:
                text += f" | ultimo: {ev['text']} ({time.strftime('%H:%M:%S', time.localtime(ev['ts']))})"
                if time.time() - ev["ts"] < FLASH_S:
                    return ("blue" if ev["kind"] == "ok" else "gray"), text
            return "green", text
        return "orange", "in pausa: " + (st.get("reason") or "")

    def running():
        return read_status() is not None

    def start(icon=None, item=None):
        if running() or calib["on"]:
            return
        flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}   # senza finestra
        # Niente log continuo: l'output normale viene scartato; su file vanno solo gli errori
        # (e il file si azzera a ogni avvio, quindi non cresce mai).
        log = HERE / "tapzones-errors.log"
        try:
            big = log.exists() and log.stat().st_size > 100_000
        except OSError:
            big = False
        err = open(log, "w" if big else "a", encoding="utf-8")   # append: non perdo gli errori della calibrazione
        subprocess.Popen([sys.executable, "-u", str(Path(__file__).resolve()), "run", *extra],
                         stdout=subprocess.DEVNULL, stderr=err, stdin=subprocess.DEVNULL,
                         cwd=str(HERE), **flags)

    def stop(icon=None, item=None):
        st = read_status()
        if not st:
            return
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(st["pid"]), "/F"], capture_output=True,
                               creationflags=0x08000000)
            else:
                os.kill(st["pid"], signal.SIGTERM)
        finally:
            write_status("stopped")

    def restart(icon=None, item=None):
        stop()                      # se non è in esecuzione non fa nulla
        time.sleep(0.5)             # lascia il tempo al microfono di liberarsi
        start()

    alive = {"v": True}

    def calibrate(icon=None, item=None):
        """Ferma 'run', apre la calibrazione guidata, poi riavvia 'run'."""
        if calib["on"]:
            return
        calib["on"] = True

        def work():
            was_running = running()
            try:
                if was_running:
                    stop()
                    time.sleep(0.5)

                # Uso SEMPRE python.exe (non pythonw): è quello con cui la GUI
                # funziona già. Su Windows nascondo la console con CREATE_NO_WINDOW
                # così non c'è nessuna finestra cmd visibile, ma stdout/stderr
                # del figlio li catturiamo e logghiamo.
                exe = Path(sys.executable)
                if sys.platform == "win32":
                    if exe.name.lower() == "pythonw.exe":
                        cand = exe.parent / "python.exe"
                        if cand.exists():
                            exe = cand
                    flags = 0x08000000                     # CREATE_NO_WINDOW
                else:
                    flags = 0

                cmd = [str(exe), "-u", str(Path(__file__).resolve()),
                       "calibrate", *calibrate_args(extra), "--gui"]
                _log("avvio calibrazione: " + " ".join(str(c) for c in cmd))

                err = open(HERE / "tapzones-errors.log", "a", encoding="utf-8")
                try:
                    proc = subprocess.Popen(cmd, cwd=str(HERE), creationflags=flags,
                                            stdout=err, stderr=err, stdin=subprocess.DEVNULL)
                    rc = proc.wait()
                    _log(f"calibrazione terminata (exit code {rc})")
                finally:
                    try:
                        err.close()
                    except Exception:
                        pass
            except Exception as e:
                _log(f"calibrazione: eccezione nel lancio: {e!r}")
            finally:
                calib["on"] = False
                if was_running and alive["v"]:
                    start()

        threading.Thread(target=work, daemon=True).start()

    def quit_panel(icon, item):
        alive["v"] = False
        stop()                      # ferma anche 'run': il microfono viene rilasciato davvero
        icon.stop()

    def toggle(icon, item):
        toggle_pause_flag()
        icon.update_menu()

    menu = pystray.Menu(
        pystray.MenuItem(lambda i: "TapZones: " + state()[1], lambda i: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Avvia", start, enabled=lambda i: not running() and not calib["on"]),
        pystray.MenuItem("Riavvia", restart, enabled=lambda i: not calib["on"]),
        pystray.MenuItem(lambda i: "Riprendi (Ctrl+Alt+P)" if PAUSE_FLAG.exists() else "Pausa (Ctrl+Alt+P)",
                         toggle, enabled=lambda i: running()),
        pystray.MenuItem("Calibra zone...", calibrate, enabled=lambda i: not calib["on"]),
        pystray.MenuItem("Ferma", stop, enabled=lambda i: running() and not calib["on"]),
        pystray.MenuItem("Esci", quit_panel),
    )
    icon = pystray.Icon("tapzones", imgs["red"], "TapZones", menu)

    def watcher(icon):
        icon.visible = True
        if args.autostart:
            start()
        last = None
        while alive["v"]:
            cur = state()
            if cur != last:
                icon.icon = imgs[cur[0]]
                icon.title = ("TapZones - " + cur[1])[:120]
                icon.update_menu()
                last = cur
            time.sleep(0.15)

    try:
        icon.run(watcher)
    finally:
        stop()                      # anche con Ctrl+C: non lascia 'run' in giro

def main():
    for stream in (sys.stdout, sys.stderr):      # None con pythonw; su file Windows usa spesso cp1252
        if stream is not None and hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    p = argparse.ArgumentParser(description="Tap sulla scrivania -> azioni")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("devices", help="elenca i microfoni").set_defaults(fn=cmd_devices)
    sm = sub.add_parser("meter", help="mostra il livello del microfono")
    sm.add_argument("--device", type=int, default=None)
    sm.add_argument("--seconds", type=int, default=15, help="durata della misura")
    sm.add_argument("--gain", type=float, default=1.0, help="amplificazione software del segnale")
    sm.set_defaults(fn=cmd_meter)
    for name, fn in (("calibrate", cmd_calibrate), ("run", cmd_run)):
        s = sub.add_parser(name)
        s.add_argument("--device", type=int, default=None, help="indice del microfono (vedi 'devices')")
        s.add_argument("--sensitivity", type=float, default=1.0, help=">1 = più sensibile")
        s.add_argument("--gain", type=float, default=1.0 if name == "calibrate" else None,
                       help="amplificazione software (se omesso in run: quella della calibrazione)")
        if name == "calibrate":
            s.add_argument("--zones", default="sinistra,destra,sotto")
            s.add_argument("--gui", action="store_true",
                           help="calibrazione guidata in una finestra grafica (tkinter)")
            s.add_argument("--hold", action="store_true", help=argparse.SUPPRESS)   # usata dall'icona: finestra aperta a fine lavoro
            s.add_argument("--samples", type=int, default=None,
                           help="tocchi per zona (se omesso te lo chiede: 10, 20 o 50)")
        else:
            s.add_argument("--min-conf", type=float, default=0.15, help="confidenza minima (0-1)")
            s.add_argument("--dry", action="store_true", help="non eseguire le azioni")
            s.add_argument("--double-ms", type=int, default=400,
                           help="finestra (ms) per il secondo tap del doppio tap; 0 = disattivato (default 400)")
            s.add_argument("--pause-apps", default="whatsapp,discord,zoom,microsoft teams,google meet,skype,telegram",
                           help="rilascia il microfono quando la finestra in primo piano contiene una di queste parole "
                                "(separate da virgola; \"\" per disattivare)")
            s.add_argument("--pause-hold", type=int, default=120,
                           help="secondi di pausa extra dopo aver lasciato quell'app (default 120)")
        s.set_defaults(fn=fn)
    sn = sub.add_parser("negatives", help="aggiunge esempi di voce/rumori da ignorare")
    sn.add_argument("--device", type=int, default=None)
    sn.add_argument("--sensitivity", type=float, default=1.0)
    sn.add_argument("--gain", type=float, default=None)
    sn.add_argument("--samples", type=int, default=20, help="esempi da raccogliere")
    sn.add_argument("--seconds", type=int, default=40, help="durata massima")
    sn.set_defaults(fn=cmd_negatives)
    st = sub.add_parser("tray", help="icona nella barra: verde = ascolta, arancione = pausa, rosso = spento")
    st.add_argument("--autostart", action="store_true", help="avvia 'run' insieme all'icona")
    st.set_defaults(fn=None)
    args, extra = p.parse_known_args()
    if args.cmd == "tray":
        cmd_tray(args, extra)          # le opzioni extra (es. --pause-hold 300) vanno a 'run'
        return
    if extra:
        p.error("argomenti non riconosciuti: " + " ".join(extra))
    if getattr(args, "hold", False):
        try:
            args.fn(args)
        except SystemExit as e:
            if isinstance(e.code, str):
                print(e.code)
        except Exception:
            import traceback
            traceback.print_exc()
        finally:
            if not getattr(args, "_gui_used", False):
                try:
                    input("\nPremi INVIO per chiudere questa finestra...")
                except EOFError:
                    pass
        return
    args.fn(args)

if __name__ == "__main__":
    main()