#!/usr/bin/env python3
"""
TapZones - tocca la scrivania, il PC esegue un'azione (Windows / macOS / Linux).

Ispirato a Holo (JustinGamer191/Holo), ma scritto in Python.

Uso:
    pip install numpy sounddevice          (opzionale: pyautogui per le scorciatoie da tastiera)
    python tapzones.py devices             # elenca i microfoni
    python tapzones.py meter --device 1    # controlla che il microfono senta i tap
    python tapzones.py calibrate           # registra le zone (default: sinistra,destra,sopra)
    python tapzones.py calibrate --zones sinistra,destra,davanti,dietro --samples 12
    python tapzones.py tray --autostart    # icona nella barra (verde/arancione/rosso) che avvia anche 'run'
    python tapzones.py negatives           # (opzionale) insegna a ignorare voce e rumori
    python tapzones.py run                 # ascolta ed esegue le azioni di actions.json
    python tapzones.py run --dry           # ascolta e stampa soltanto, senza eseguire nulla

Azioni (file actions.json, creato dopo la calibrazione):
    {"sinistra": {"type": "url",  "value": "https://youtube.com"},
     "destra":   {"type": "keys", "value": "playpause"},
     "sopra":    {"type": "run",  "value": "calc.exe"}}
    type: none | url | run | keys   (keys richiede pyautogui; es. "ctrl+shift+m", "volumeup")
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
        self.cool = int(REFRACTORY_S * self.sr / BLOCK)
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
        self.stream = None
        self.open_ch = channels

    def __enter__(self):
        return self.open()

    def open(self):
        import sounddevice as sd
        self.q = queue.Queue()                    # stato pulito a ogni (ri)apertura
        self.det = Detector(self.sr, self.sens)
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


# ---- calibrazione --------------------------------------------------------
def collect_zone(L, sr, name, n):
    out, got = [], 0
    input(f"\nZona «{name}»: premi INVIO, poi tocca {n} volte quell'area (variando un po' il punto). ")
    L.flush(0.7)
    for kind, data in L.events(stop=lambda: got >= n):
        if kind == "rej":
            print(f"   ✗ scartato: {data}")
            continue
        out.append(features(data, sr))
        got += 1
        print(f"   ✓ {got}/{n}")
    return out


def cmd_calibrate(args):
    names = [z.strip() for z in args.zones.split(",") if z.strip()]
    if len(names) < 2:
        sys.exit("Servono almeno 2 zone.")
    sr, ch, dev_name = pick_device(args.device)
    print(f"Microfono: {dev_name} | {ch} canale/i | {sr} Hz")
    print("Tieni il PC fermo nella posizione in cui lo userai. Spostarlo invalida la calibrazione.")
    samples = {}
    with Listener(sr, ch, args.device, args.sensitivity, args.gain) as L:
        print("Imparo il rumore ambiente, silenzio per un attimo...")
        L.settle(1.5)
        for i, name in enumerate(names):
            samples[i] = collect_zone(L, sr, name, args.samples)
        while True:
            X = np.array([f for i in range(len(names)) for f in samples[i]])
            y = np.array([i for i in range(len(names)) for _ in samples[i]])
            preds, own = leave_one_out(X, y)
            print("\n=== Controllo di coerenza (leave-one-out) ===")
            print(f"Accuratezza stimata: {np.mean(preds == y) * 100:.0f}%")
            print("Matrice di confusione (righe = zona vera, colonne = riconosciuta):")
            print("            " + " ".join(f"{n[:9]:>9}" for n in names))
            worst, worst_acc = 0, 2.0
            for i, n in enumerate(names):
                row = [int(np.sum((y == i) & (preds == j))) for j in range(len(names))]
                acc = row[i] / max(1, sum(row))
                if acc < worst_acc:
                    worst, worst_acc = i, acc
                print(f"{n[:11]:>11} " + " ".join(f"{v:>9}" for v in row) + f"   {acc * 100:.0f}%")
            if worst_acc < 0.8:
                print(f"\nLa zona più debole è «{names[worst]}» ({worst_acc * 100:.0f}%).")
            ans = input("Rifare una zona? Scrivi il suo nome, oppure INVIO per salvare: ").strip()
            if not ans:
                break
            if ans in names:
                i = names.index(ans)
                samples[i] = collect_zone(L, sr, ans, args.samples)
            else:
                print("Nome non valido.")
    ood = float(1.5 * np.percentile(own, 95))
    dname, dapi = device_identity(args.device)
    PROFILE.write_text(json.dumps({"sr": sr, "channels": ch, "device": args.device,
                                   "device_name": dname, "device_api": dapi, "gain": args.gain, "zones": names,
                                   "X": X.tolist(), "y": y.tolist(), "ood": ood}))
    print(f"\nProfilo salvato in {PROFILE}")
    if not ACTIONS.exists():
        ACTIONS.write_text(json.dumps({n: {"type": "none", "value": ""} for n in names},
                                      indent=2, ensure_ascii=False))
        print(f"Creato {ACTIONS}: modificalo per assegnare le azioni, poi lancia: python tapzones.py run")


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


def write_status(state, reason=""):
    tmp = STATUS.with_suffix(".tmp")
    tmp.write_text(json.dumps({"state": state, "reason": reason, "pid": os.getpid(), "ts": time.time()},
                              ensure_ascii=False))
    os.replace(tmp, STATUS)


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
    try:
        d = json.loads(STATUS.read_text())
    except Exception:
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
    hotkey = start_hotkey(toggle_pause_flag)

    def stamp():
        return time.strftime("%H:%M:%S")

    def handle(kind, data):
        if kind == "rej":
            print(f"  · ignorato: {data}")
            return
        t0 = time.perf_counter()
        zone, conf, why = model.predict(features(data, sr), prof["ood"], args.min_conf)
        ms = (time.perf_counter() - t0) * 1000
        if zone is None:
            print(f"  ? rifiutato: {why} (conf {conf:.2f})")
        elif zone >= len(names):
            print(f"  · scartato: sembra voce/rumore (conf {conf:.2f})")
        else:
            name = names[zone]
            load_actions()
            act = actions.get(name, {"type": "none"})
            print(f"  ✔ {name}  conf {conf:.2f}  ({ms:.0f} ms)  -> {act.get('type', 'none')}")
            if not args.dry:
                do_action(act)

    last_app = 0.0                                # ultimo istante in cui un'app "da chiamata" era in primo piano

    def pause_reason():
        nonlocal last_app
        if PAUSE_FLAG.exists():
            return "pausa manuale (Ctrl+Alt+P per riprendere)"
        title = foreground_title().lower()
        hit = next((k for k in keywords if k in title), None)
        if hit:
            last_app = time.time()
            return f"app «{hit}» in primo piano"
        if keywords and time.time() - last_app < args.pause_hold:
            return f"da poco in «{hit or 'app di chiamata'}» (riprendo tra pochi minuti)"
        return None

    print("In ascolto... (Ctrl+C per uscire)" + ("  [DRY RUN: nessuna azione]" if args.dry else ""))
    print(f"Pausa automatica con: {', '.join(keywords) if keywords else 'disattivata'}"
          + ("  |  Ctrl+Alt+P = pausa/riprendi a mano" if hotkey else ""))
    L = Listener(sr, ch, dev, args.sensitivity, gain)
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
            for kind, data in L.events(stop=lambda: time.time() > deadline):
                try:
                    handle(kind, data)
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


def cmd_tray(args, extra):
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        sys.exit("Servono due pacchetti: pip install pystray pillow")
    import threading
    colors = {"green": (34, 197, 94), "orange": (245, 158, 11), "red": (239, 68, 68)}

    def make(rgb):
        im = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        ImageDraw.Draw(im).ellipse((6, 6, 58, 58), fill=rgb + (255,))
        return im

    imgs = {k: make(v) for k, v in colors.items()}

    def state():
        st = read_status()
        if st is None:
            return "red", "non in esecuzione"
        if st["state"] == "listening":
            return "green", "in ascolto"
        return "orange", "in pausa: " + (st.get("reason") or "")

    def running():
        return read_status() is not None

    def start(icon=None, item=None):
        if running():
            return
        flags = {"creationflags": 0x08000000} if sys.platform == "win32" else {}   # senza finestra
        # Niente log continuo: l'output normale viene scartato; su file vanno solo gli errori
        # (e il file si azzera a ogni avvio, quindi non cresce mai).
        err = open(HERE / "tapzones-errors.log", "w", encoding="utf-8")
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

    alive = {"v": True}

    def quit_panel(icon, item):
        alive["v"] = False
        icon.stop()

    def toggle(icon, item):
        toggle_pause_flag()
        icon.update_menu()

    menu = pystray.Menu(
        pystray.MenuItem(lambda i: "TapZones: " + state()[1], lambda i: None, enabled=False),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Avvia", start, enabled=lambda i: not running()),
        pystray.MenuItem(lambda i: "Riprendi" if PAUSE_FLAG.exists() else "Metti in pausa (Ctrl+Alt+P)",
                         toggle, enabled=lambda i: running()),
        pystray.MenuItem("Ferma", stop, enabled=lambda i: running()),
        pystray.MenuItem("Chiudi icona (lo script resta com'è)", quit_panel),
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
            time.sleep(1)

    icon.run(watcher)


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
            s.add_argument("--zones", default="sinistra,destra,sopra")
            s.add_argument("--samples", type=int, default=10)
        else:
            s.add_argument("--min-conf", type=float, default=0.15, help="confidenza minima (0-1)")
            s.add_argument("--dry", action="store_true", help="non eseguire le azioni")
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
    args.fn(args)


if __name__ == "__main__":
    main()