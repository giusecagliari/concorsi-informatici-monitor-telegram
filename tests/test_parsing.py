"""Test offline del parsing e della logica di notifica.

Non tocca la rete: sostituisce scrape_with_playwright con una fixture HTML
che riproduce il markup reale di concorsipubblici.com.

Uso:  python tests/test_parsing.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Variabili fittizie: main.py le legge all'import, qui non inviamo nulla davvero.
os.environ.setdefault('TELEGRAM_TOKEN', 'dummy')
os.environ.setdefault('TELEGRAM_CHAT_ID', 'dummy')

import main

FIXTURE = os.path.join(os.path.dirname(__file__), 'fixtures', 'listing_page.html')

fallimenti = []


def check(nome, atteso, ottenuto):
    esito = "PASS" if atteso == ottenuto else "FAIL"
    if esito == "FAIL":
        fallimenti.append(nome)
    print(f"[{esito}] {nome}: atteso={atteso!r} ottenuto={ottenuto!r}")


def test_scraping_e_keywords():
    with open(FIXTURE, encoding='utf-8') as f:
        html = f.read()
    main.scrape_with_playwright = lambda url: html

    tutti, attivi = main.scrape_bandi()
    check("bandi totali in pagina", 4, len(tutti))
    check("bandi ancora aperti", 2, len(attivi))

    urls = main.estrai_bandi_con_keywords(attivi)
    check("bandi aperti con keyword", 1, len(urls))
    check(
        "URL assoluto del bando",
        "https://www.concorsipubblici.com/comune-di-treviso-concorso-funzionario-informatico-treviso-2030",
        urls[0] if urls else None,
    )


def test_pagina_vuota_e_alert_rate_limited():
    """Pagina senza <article>: alert una volta sola, poi silenzio per 7 giorni."""
    main.scrape_with_playwright = lambda url: "<html><body><p>Nessun risultato</p></body></html>"
    tutti, attivi = main.scrape_bandi()
    check("pagina vuota -> 0 article", 0, len(tutti))

    check("primo alert consentito", True, main.should_send_structure_alert({"last_structure_alert": None}))

    from datetime import datetime, timedelta, timezone
    ieri = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    check("alert ripetuto dopo 1 giorno", False, main.should_send_structure_alert({"last_structure_alert": ieri}))

    dieci_giorni_fa = (datetime.now(timezone.utc) - timedelta(days=10)).isoformat()
    check("alert di nuovo dopo 10 giorni", True, main.should_send_structure_alert({"last_structure_alert": dieci_giorni_fa}))


def test_stato_retrocompatibile(tmp_file="tests/_tmp_state.json"):
    """load_state deve accettare sia il vecchio formato (lista) sia il nuovo (dict)."""
    import json
    originale = main.SEEN_BANDI_FILE
    main.SEEN_BANDI_FILE = tmp_file
    try:
        with open(tmp_file, "w", encoding="utf-8") as f:
            json.dump(["https://esempio.it/bando-1"], f)
        stato = main.load_state()
        check("vecchio formato lista", ["https://esempio.it/bando-1"], stato["seen"])
        check("alert assente nel vecchio formato", None, stato["last_structure_alert"])

        stato["seen"].append("https://esempio.it/bando-2")
        main.save_state(stato)
        riletto = main.load_state()
        check("nuovo formato salvato e riletto", 2, len(riletto["seen"]))
    finally:
        main.SEEN_BANDI_FILE = originale
        if os.path.exists(tmp_file):
            os.remove(tmp_file)


if __name__ == "__main__":
    test_scraping_e_keywords()
    test_pagina_vuota_e_alert_rate_limited()
    test_stato_retrocompatibile()
    print()
    if fallimenti:
        print(f"❌ {len(fallimenti)} test falliti: {fallimenti}")
        sys.exit(1)
    print("✅ Tutti i test passati")
