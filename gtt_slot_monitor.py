#!/usr/bin/env python3
"""
Monitor degli slot GTT su Ufirst (Corso Francia 6 e Porta Nuova).

Ti avvisa con una notifica ntfy SOLO quando una delle due sedi ha posti liberi
nelle date che ti interessano (di default 6, 7 e 8 novembre 2026).
NON prenota nulla: quando arriva l'avviso, prenoti tu dall'app o dal sito Ufirst.

Come funziona (per ogni sede):
  1. apre la pagina Ufirst e guarda se il servizio "Trasporto Pubblico" e'
     selezionabile (se e' bloccato = overbooking, non c'e' nulla da fare);
  2. se e' selezionabile, lo seleziona, preme "Avanti" e legge dalla risposta
     di Ufirst l'elenco di TUTTI gli orari liberi (nessun dato personale viene
     inserito, nessuna prenotazione viene fatta);
  3. avvisa solo se tra gli orari ce n'e' almeno uno nelle date richieste.
  Se il servizio e' selezionabile ma le date non si riescono a leggere
  (Ufirst cambia qualcosa), ti arriva comunque un avviso, a titolo prudenziale.

Frequenza dei controlli (ora italiana), nell'uso continuo senza GitHub:
  - ogni 2 minuti tra le 23:50 e le 12:00
  - ogni ora nel resto della giornata (ripartendo alle 23:50)

Installazione (una volta sola):
  pip install playwright tzdata
  playwright install chromium

Uso:
  python gtt_slot_monitor.py --probe   # test: stampa cosa vede e invia una notifica di prova
  python gtt_slot_monitor.py --once    # un solo controllo ed esce
  python gtt_slot_monitor.py           # monitoraggio continuo (lascia il PC acceso)
  python gtt_slot_monitor.py --dates 2026-11-06,2026-11-07   # altre date

Notifiche: installa l'app ntfy sul telefono e iscriviti a un topic a tua scelta
(un nome lungo e casuale). Passalo allo script con la variabile d'ambiente
NTFY_TOPIC oppure con --topic. Il topic non e' scritto nel file: chi ne conosce
il nome puo' leggere e inviare notifiche, quindi non pubblicarlo.

Senza PC acceso: vedi .github/workflows/gtt-monitor.yml (GitHub Actions, usa
--once a ogni esecuzione e il topic dal secret NTFY_TOPIC).

Nota: la pagina Ufirst e' disegnata su canvas (Flutter). Lo script attiva
l'accessibilita' della pagina per leggerne i pulsanti. Se Ufirst cambia
struttura, usa --probe per vedere cosa e' cambiato.
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
# Solo per --probe: una sede che oggi ha il servizio aperto, cosi' si verifica
# che la lettura degli orari funzioni davvero. Non genera mai notifiche.
PROBE_EXTRA = {
    "Ivrea (solo test)": "https://www.ufirst.com/book/it/organizations/172/points/QQSP000000850",
}

TARGET_DATES = ("2026-11-06", "2026-11-07", "2026-11-08")   # ven, sab, dom

SERVICE_RE = re.compile(r"trasporto pubblico", re.I)        # il servizio che ci interessa
ANY_SERVICE_RE = re.compile(r"trasporto pubblico|sosta a pagamento|ztl|disabili", re.I)
FORWARD_PATTERN = r"^(forward|avanti|continua|prosegui)$"

FAST_START = dtime(23, 50)   # da qui ...
FAST_END = dtime(12, 0)      # ... fino a qui: controllo ogni 2 minuti
FAST_INTERVAL = 120          # secondi
SLOW_INTERVAL = 3600         # secondi
REMIND_AFTER = timedelta(minutes=15)  # nuova notifica se i posti restano liberi
FAIL_ALERT_AFTER = 5         # errori consecutivi prima di avvisarti

IT_DAYS = ["lun", "mar", "mer", "gio", "ven", "sab", "dom"]

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

# Clicca il primo pulsante (role=button o flt-tappable) la cui etichetta
# corrisponde al pattern. Restituisce il centro dell'elemento, o null.
JS_CLICK = """
(src) => {
  const re = new RegExp(src, 'i');
  const lab = el => (el.getAttribute('aria-label') ||
    Array.from(el.childNodes).filter(n => n.nodeType === 3)
      .map(n => n.textContent).join(' ')).trim();
  const el = Array.from(document.querySelectorAll('flt-semantics')).find(
    e => re.test(lab(e)) && (e.getAttribute('role') === 'button' || e.hasAttribute('flt-tappable')));
  if (!el) return null;
  const r = el.getBoundingClientRect();
  el.click();
  return {x: r.x + r.width / 2, y: r.y + r.height / 2};
}
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


# ------------------------------------------------- lettura degli orari ------

def parse_slots(payload):
    """Dalla risposta RPC di Ufirst (GetServiceResourcesAvailability) all'elenco
    ordinato degli orari liberi, come stringhe RFC3339 con l'ora italiana,
    es. '2026-11-06T09:10:00+01:00'.

    Restituisce None se la risposta non ha la forma attesa (cosi' non si scambia
    un formato cambiato per 'nessun posto libero'), [] se non ci sono orari."""
    if not isinstance(payload, dict):
        return None
    result = payload.get("result")
    if not isinstance(result, dict):
        return None
    resources = result.get("resourcesAvailability")
    if not isinstance(resources, list):
        return None
    slots = set()
    for res in resources:
        if not isinstance(res, dict):
            continue
        for ts in res.get("timeslots") or []:
            s = ts.get("startTimeRFC3339") if isinstance(ts, dict) else None
            if isinstance(s, str) and re.match(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}", s):
                slots.add(s)
    return sorted(slots)


def group_by_date(slots):
    """{'2026-11-06': ['08:00', '08:10', ...], ...}"""
    out = {}
    for s in slots:
        out.setdefault(s[:10], []).append(s[11:16])
    return out


def filter_target(slots, targets):
    return [s for s in slots if s[:10] in targets]


def describe_slots(slots):
    parts = []
    for d, times in sorted(group_by_date(slots).items()):
        dt = datetime.strptime(d, "%Y-%m-%d")
        n = len(times)
        parts.append(f"{IT_DAYS[dt.weekday()]} {dt.day}/{dt.month}: "
                     f"{n} {'posto' if n == 1 else 'posti'} ({times[0]}-{times[-1]})")
    return "; ".join(parts)


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
        page.wait_for_timeout(1000)
    return nodes


def is_open(nodes):
    """True se almeno un 'Trasporto Pubblico' e' selezionabile.

    Verificato sulle pagine reali: un servizio bloccato e' role='img' senza
    flt-tappable (disabled resta False, quindi NON va usato); uno selezionabile
    (visto a Ivrea) e' role='button' con flt-tappable.
    """
    targets = [n for n in nodes if SERVICE_RE.search(n["label"])]
    return any(n["role"] == "button" or n.get("tappable") for n in targets)


def click_label(page, pattern):
    """Clicca il pulsante con quell'etichetta. True se trovato."""
    return page.evaluate(JS_CLICK, pattern) is not None


def slots_from_responses(responses):
    """Prima risposta RPC che contiene gli orari, oppure None."""
    for r in responses:
        try:
            if "contento_secure_rpc" not in r.url:
                continue
            payload = r.json()
        except Exception:  # noqa: BLE001  (corpo non disponibile / non JSON)
            continue
        slots = parse_slots(payload)
        if slots is not None:
            return slots
    return None


def fetch_slots(page, responses, wait=20):
    """Seleziona il servizio, preme Avanti e legge gli orari liberi.

    Non inserisce alcun dato personale e non prenota: si ferma al passo in cui
    Ufirst mostra date e orari. Restituisce la lista di orari (anche vuota) o
    None se non e' riuscito a leggerli."""
    if not click_label(page, SERVICE_RE.pattern):
        return None
    # Dopo la selezione compare il pulsante Avanti.
    deadline = time.time() + 10
    pressed = False
    while time.time() < deadline and not pressed:
        pressed = click_label(page, FORWARD_PATTERN)
        if not pressed:
            page.wait_for_timeout(500)
    if not pressed:
        return None
    deadline = time.time() + wait
    while time.time() < deadline:
        slots = slots_from_responses(responses)
        if slots is not None:
            return slots
        page.wait_for_timeout(500)
    return None


def check_point(browser, name, url, targets, probe=False):
    """Restituisce {'open': bool, 'slots': list|None, 'hits': list}.

    open=False  -> servizio bloccato (overbooking)
    slots=None  -> servizio aperto ma orari non leggibili
    hits        -> orari liberi nelle date richieste"""
    page = browser.new_page(viewport={"width": 1280, "height": 900}, locale="it-IT")
    responses = []
    page.on("response", lambda r: responses.append(r))
    try:
        nodes = read_nodes(page, url)
        if probe:
            page.screenshot(path=f"probe_{name.replace(' ', '_')}.png")
            print(f"\n=== {name}: {len(nodes)} nodi ===")
            for n in nodes:
                if n["label"]:
                    print(f"  role={n['role']!r:12} tappable={n['tappable']!s:5} "
                          f"label={n['label']!r}")
        if not [n for n in nodes if SERVICE_RE.search(n["label"])]:
            raise RuntimeError(f"{name}: servizio 'Trasporto Pubblico' non trovato "
                               f"({len(nodes)} nodi letti)")
        if not is_open(nodes):
            return {"open": False, "slots": [], "hits": []}
        slots = fetch_slots(page, responses)
        if probe:
            if slots is None:
                print("  -> servizio aperto, ma NON sono riuscito a leggere gli orari")
            else:
                by = group_by_date(slots)
                print(f"  -> orari letti: {len(slots)} in {len(by)} giorni")
                for d, times in sorted(by.items()):
                    print(f"     {d}: {len(times)} orari ({times[0]}-{times[-1]})")
        hits = filter_target(slots, targets) if slots is not None else []
        return {"open": True, "slots": slots, "hits": hits}
    finally:
        page.close()


def run_cycle(topic, state, targets, probe=False):
    """Controlla tutte le sedi. Restituisce (risultati, errori): un errore su
    una sede non nasconde l'esito dell'altra."""
    from playwright.sync_api import sync_playwright
    points = dict(POINTS)
    if probe:
        points.update(PROBE_EXTRA)
    results, errors = {}, {}
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            for name, url in points.items():
                try:
                    results[name] = check_point(browser, name, url, targets, probe=probe)
                except Exception as e:  # noqa: BLE001
                    errors[name] = e
                    log(f"errore su {name}: {e}")
        finally:
            browser.close()
    return results, errors


# --------------------------------------------------------------- stato -----

def handle_results(results, state, topic, now, send=notify, once=False, targets=TARGET_DATES):
    """Notifica quando una sede ha posti nelle date richieste (o, per prudenza,
    quando e' aperta ma le date non sono leggibili) e ripete ogni 15 minuti
    finche' la situazione dura."""
    lines = []
    for name, r in results.items():
        s = state.setdefault(name, {"kind": None, "last": None})
        if not r["open"]:
            kind = None
            lines.append(f"{name}=bloccato")
        elif r["hits"]:
            kind = "hit"
            lines.append(f"{name}=POSTI NELLE DATE ({len(r['hits'])})")
        elif r["slots"] is None:
            kind = "unverified"
            lines.append(f"{name}=aperto ma date non leggibili")
        else:
            kind = None
            last = r["slots"][-1][:10] if r["slots"] else "nessuna data"
            lines.append(f"{name}=aperto, niente nelle date richieste (ultima data: {last})")

        if kind is not None:
            due = (s["kind"] != kind) or s["last"] is None or (now - s["last"]) >= REMIND_AFTER
            if kind == "unverified" and once and now.minute >= 10:
                due = False  # senza memoria tra le esecuzioni: al massimo una volta l'ora
            if due:
                if kind == "hit":
                    send(topic, f"GTT: posti liberi - {name}",
                         f"{name}: {describe_slots(r['hits'])}. Prenota subito su Ufirst.",
                         click=POINTS.get(name))
                else:
                    wanted = ", ".join(f"{d[8:10]}/{d[5:7]}" for d in targets)
                    send(topic, f"GTT: controlla a mano - {name}",
                         f"{name}: il servizio Trasporto Pubblico e' selezionabile ma non sono "
                         f"riuscito a leggere le date ({wanted}). Controlla tu su Ufirst.",
                         click=POINTS.get(name), priority="high")
                s["last"] = now
                log(f"NOTIFICA inviata ({kind}): {name}")
        s["kind"] = kind
    log("esito: " + "; ".join(lines))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--topic", default=os.environ.get("NTFY_TOPIC"))
    ap.add_argument("--once", action="store_true",
                    help="un solo controllo (per GitHub Actions o cron)")
    ap.add_argument("--duration", type=float, default=0, metavar="MINUTI",
                    help="continua a controllare per questi minuti (con gli intervalli "
                         "normali) e poi esce: per GitHub Actions, un run ogni mezz'ora")
    ap.add_argument("--probe", action="store_true",
                    help="stampa cosa legge, salva screenshot, invia una notifica di prova, poi esce")
    ap.add_argument("--dates", default=",".join(TARGET_DATES),
                    help="date da cercare, AAAA-MM-GG separate da virgola "
                         f"(default: {','.join(TARGET_DATES)})")
    args = ap.parse_args()

    targets = tuple(d.strip() for d in args.dates.split(",") if d.strip())
    for d in targets:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", d):
            sys.exit(f"Data non valida: {d!r} (usa AAAA-MM-GG)")

    if args.probe:
        run_cycle(args.topic, {}, targets, probe=True)
        if args.topic:
            try:
                notify(args.topic, "GTT monitor: notifica di prova",
                       "Se leggi questo messaggio, le notifiche funzionano.",
                       priority="default")
                print("\nNotifica di prova inviata al topic ntfy.")
            except Exception as e:  # noqa: BLE001
                print(f"\nATTENZIONE: notifica di prova NON inviata ({e})")
        else:
            print("\n(nessun topic ntfy impostato: notifica di prova non inviata)")
        return

    if not args.topic:
        sys.exit("Manca il topic ntfy: imposta NTFY_TOPIC oppure usa --topic")

    state, fails, fail_alerted = {}, 0, False
    end_at = time.time() + args.duration * 60 if args.duration > 0 else None
    if not args.once and end_at is None:
        try:
            notify(args.topic, "GTT monitor avviato",
                   "Il monitor degli slot GTT e' attivo. Se vedi questo messaggio, "
                   "le notifiche funzionano.", priority="default")
        except Exception as e:  # noqa: BLE001
            log(f"ATTENZIONE: notifica di avvio non inviata ({e})")
        log(f"monitor avviato, date cercate: {', '.join(targets)}, topic ntfy: {args.topic}")
    elif end_at is not None:
        log(f"run di {args.duration:g} minuti, date cercate: {', '.join(targets)}")

    while True:
        try:
            results, errors = run_cycle(args.topic, state, targets)
            handle_results(results, state, args.topic, now_rome(),
                           once=args.once, targets=targets)
            if errors:
                raise RuntimeError("; ".join(f"{n}: {e}" for n, e in errors.items()))
            fails, fail_alerted = 0, False
        except KeyboardInterrupt:
            raise
        except Exception as e:  # noqa: BLE001
            fails += 1
            log(f"errore ({fails}): {e}")
            if args.once:
                # Senza memoria tra un'esecuzione e l'altra: avviso al massimo
                # una volta l'ora (solo se il controllo cade nei primi 10 minuti).
                alert = now_rome().minute < 10
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
        if end_at is not None and time.time() + wait > end_at:
            log("fine del run: il prossimo controllo e' oltre la durata prevista")
            return
        log(f"prossimo controllo tra {int(wait)} s")
        time.sleep(wait)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        sys.exit(0)
