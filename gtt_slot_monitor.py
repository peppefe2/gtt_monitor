#!/usr/bin/env python3
"""
Monitor degli slot GTT su Ufirst (Corso Francia 6 e Porta Nuova).

Ti avvisa con una notifica ntfy quando il servizio "Trasporto Pubblico" di una
delle due sedi torna selezionabile (cioe' quando si libera o si apre un posto).
NON prenota nulla: quando arriva l'avviso, prenoti tu dall'app o dal sito Ufirst.

Frequenza dei controlli (ora italiana):
  - ogni 2 minuti tra le 23:50 e le 12:00
  - ogni ora nel resto della giornata (ripartendo alle 23:50)

Installazione (una volta sola):
  pip install playwright tzdata
  playwright install chromium

Uso:
  python gtt_slot_monitor.py --probe   # test: stampa cosa vede nella pagina
  python gtt_slot_monitor.py --once    # un solo controllo ed esce
  python gtt_slot_monitor.py           # monitoraggio continuo (lascia il PC acceso)

Notifiche: installa l'app ntfy sul telefono e iscriviti a un topic a tua scelta
(un nome lungo e casuale). Passalo allo script con la variabile d'ambiente
NTFY_TOPIC oppure con --topic. Il topic non e' scritto nel file: chi ne conosce
il nome puo' leggere e inviare notifiche, quindi non pubblicarlo.

Senza PC acceso: vedi .github/workflows/gtt-monitor.yml (GitHub Actions, usa
--once a ogni esecuzione e il topic dal secret NTFY_TOPIC).

Nota: la pagina Ufirst e' disegnata su canvas (Flutter). Lo script attiva
l'accessibilita' della pagina per leggerne il testo e lo stato dei pulsanti.
Se Ufirst cambia struttura, usa --probe per vedere cosa e' cambiato.
"""

import argparse
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, time as dtime, timedelta

try:
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
except ImportError:  # pragma: no cover
    ZoneInfo = None
    ZoneInfoNotFoundError = Exception

POINTS = {
    "Corso Francia": "https://www.ufirst.com/book/it/organizations/172/points/QQSP000000101",
    "Porta Nuova": "https://www.ufirst.com/book/it/organizations/172/points/QQSP000000102",
}

SERVICE_RE = re.compile(r"trasporto pubblico", re.I)        # il servizio che ci interessa
ANY_SERVICE_RE = re.compile(r"trasporto pubblico|sosta a pagamento|ztl|disabili", re.I)

FAST_START = dtime(23, 50)   # da qui ...
FAST_END = dtime(12, 0)      # ... fino a qui: controllo ogni 2 minuti
FAST_INTERVAL = 120          # secondi
SLOW_INTERVAL = 3600         # secondi
REMIND_AFTER = timedelta(minutes=15)  # nuova notifica se resta aperto
FAIL_ALERT_AFTER = 5         # errori consecutivi prima di avvisarti

JS_NODES = """
() => Array.from(document.querySelectorAll('flt-semantics')).map(el => {
  const own = Array.from(el.childNodes)
    .filter(n => n.nodeType === 3).map(n => n.textContent).join(' ').trim();
  const label = (el.getAttribute('aria-label') || own || '').trim();
  const dis = el.closest('[aria-disabled]');
  return {
    role: el.getAttribute('role') || '',
    label: label,
    disabled: dis ? dis.getAttribute('aria-disabled') === 'true' : false,
    tappable: el.hasAttribute('flt-tappable'),
  };
})
"""


# ---------------------------------------------------------------- orari ----

def now_rome():
    if ZoneInfo is not None:
        try:
            return datetime.now(ZoneInfo("Europe/Rome")).replace(tzinfo=None)
        except ZoneInfoNotFoundError:
            pass
    return datetime.now()  # se manca tzdata si usa l'ora locale del PC


def in_fast_window(t):
    return t >= FAST_START or t < FAST_END


def seconds_until_next_check(now):
    """Secondi da aspettare dopo un controllo fatto alle `now` (ora naive)."""
    tod = now.time()
    if in_fast_window(tod):
        return FAST_INTERVAL
    start = datetime.combine(now.date(), FAST_START)
    until_start = (start - now).total_seconds()
    return max(1, min(SLOW_INTERVAL, until_start))


# ---------------------------------------------------------- notifiche ------

def notify(topic, title, message, click=None, priority="urgent"):
    # Gli header HTTP devono essere ASCII: niente accenti ne' emoji nel titolo.
    headers = {"Title": title.encode("ascii", "replace").decode(),
               "Priority": priority, "Tags": "bell"}
    if click:
        headers["Click"] = click
    req = urllib.request.Request(
        f"https://ntfy.sh/{topic}", data=message.encode("utf-8"),
        headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        r.read()


def log(msg):
    print(f"[{now_rome():%Y-%m-%d %H:%M:%S}] {msg}", flush=True)


# ------------------------------------------------------------- browser -----

def read_nodes(page, url):
    """Attiva l'accessibilita' di Flutter e restituisce i nodi con testo."""
    page.goto(url, wait_until="domcontentloaded", timeout=45000)
    page.wait_for_selector("flt-semantics-placeholder", state="attached", timeout=30000)
    # Il pulsante e' fuori schermo: lo si attiva via JS.
    page.evaluate("document.querySelector('flt-semantics-placeholder').click()")
    deadline = time.time() + 25
    nodes = []
    while time.time() < deadline:
        nodes = page.evaluate(JS_NODES)
        if any(ANY_SERVICE_RE.search(n["label"]) for n in nodes):
            return nodes
        time.sleep(1)
    return nodes


def is_open(nodes):
    """True se almeno un 'Trasporto Pubblico' e' selezionabile.

    Verificato sulle pagine reali: un servizio bloccato e' role='img' senza
    flt-tappable (disabled resta False, quindi NON va usato); uno selezionabile
    (visto a Ivrea) e' role='button' con flt-tappable.
    """
    targets = [n for n in nodes if SERVICE_RE.search(n["label"])]
    return any(n["role"] == "button" or n.get("tappable") for n in targets)


def check_point(browser, name, url, probe=False):
    """True = 'Trasporto Pubblico' selezionabile, False = bloccato."""
    page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="it-IT")
    try:
        nodes = read_nodes(page, url)
        if probe:
            page.screenshot(path=f"probe_{name.replace(' ', '_')}.png")
            print(f"\n=== {name}: {len(nodes)} nodi ===")
            for n in nodes:
                if n["label"]:
                    print(f"  role={n['role']!r:12} tappable={n['tappable']!s:5} "
                          f"label={n['label']!r}")
        targets = [n for n in nodes if SERVICE_RE.search(n["label"])]
        if not targets:
            raise RuntimeError(f"{name}: servizio 'Trasporto Pubblico' non trovato "
                               f"({len(nodes)} nodi letti)")
        return is_open(nodes)
    finally:
        page.close()


def run_cycle(topic, state, probe=False):
    from playwright.sync_api import sync_playwright
    results = {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for name, url in POINTS.items():
                results[name] = check_point(browser, name, url, probe=probe)
        finally:
            browser.close()
    return results


# --------------------------------------------------------------- stato -----

def handle_results(results, state, topic, now, send=notify):
    """Notifica solo quando una sede passa da bloccata a selezionabile
    (e ripete ogni 15 minuti finche' resta aperta)."""
    for name, is_open in results.items():
        s = state.setdefault(name, {"open": False, "last": None})
        if is_open:
            due = (not s["open"]) or s["last"] is None or (now - s["last"]) >= REMIND_AFTER
            if due:
                send(topic, f"GTT: posto disponibile - {name}",
                     f"Il servizio Trasporto Pubblico di {name} e' ora selezionabile su "
                     f"Ufirst. Controlla le date 6-8 novembre e prenota subito.",
                     click=POINTS[name])
                s["last"] = now
                log(f"NOTIFICA inviata: {name} selezionabile")
        s["open"] = is_open
    log("esito: " + ", ".join(f"{k}={'APERTO' if v else 'bloccato'}"
                              for k, v in results.items()))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default=os.environ.get("NTFY_TOPIC"))
    ap.add_argument("--once", action="store_true",
                    help="un solo controllo (per GitHub Actions o cron)")
    ap.add_argument("--probe", action="store_true",
                    help="stampa i nodi letti e salva uno screenshot, poi esce")
    args = ap.parse_args()

    if args.probe:
        run_cycle(args.topic, {}, probe=True)
        return

    if not args.topic:
        sys.exit("Manca il topic ntfy: imposta NTFY_TOPIC oppure usa --topic")

    state, fails, fail_alerted = {}, 0, False
    if not args.once:
        try:
            notify(args.topic, "GTT monitor avviato",
                   "Il monitor degli slot GTT e' attivo. Se vedi questo messaggio, "
                   "le notifiche funzionano.", priority="default")
        except Exception as e:  # noqa: BLE001
            log(f"ATTENZIONE: notifica di avvio non inviata ({e})")
        log(f"monitor avviato, topic ntfy: {args.topic}")

    while True:
        try:
            results = run_cycle(args.topic, state)
            handle_results(results, state, args.topic, now_rome())
            fails, fail_alerted = 0, False
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"errore ({fails}): {e}")
            if args.once:
                # Senza memoria tra un'esecuzione e l'altra: avviso al massimo
                # una volta l'ora (solo se il controllo cade nei primi 5 minuti).
                alert = now_rome().minute < 5
                text = f"Controllo fallito: {e}"
            else:
                alert = fails >= FAIL_ALERT_AFTER and not fail_alerted
                text = f"{fails} controlli consecutivi falliti. Ultimo errore: {e}"
            if alert:
                try:
                    notify(args.topic, "GTT monitor: errore", text, priority="high")
                    fail_alerted = True
                except Exception:  # noqa: BLE001
                    pass
        if args.once:
            return
        wait = seconds_until_next_check(now_rome())
        log(f"prossimo controllo tra {int(wait)} s")
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
