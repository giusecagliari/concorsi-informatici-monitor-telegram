# IMPORTS
import os
import time
import random
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
import requests
from bs4 import BeautifulSoup
import json
from dotenv import load_dotenv

# CONFIGURATIONS
load_dotenv()

TELEGRAM_CHAT_ID = os.environ['TELEGRAM_CHAT_ID']
TELEGRAM_TOKEN = os.environ['TELEGRAM_TOKEN']
OPENROUTER_API_KEY = os.environ.get('OPENROUTER_API_KEY')
# Se true, invia su Telegram anche gli errori tecnici. Di default il bot scrive
# solo quando c'è davvero un nuovo bando da segnalare.
NOTIFY_ERRORS = os.environ.get('NOTIFY_ERRORS', 'false').lower() in ('1', 'true', 'yes')
AI_MODELS = [
    "z-ai/glm-4.5-air:free",
    "nvidia/nemotron-nano-9b-v2:free",
    "meta-llama/llama-3.3-70b-instruct:free",
]
SEEN_BANDI_FILE = "seen_bandi.json"
# Numero massimo di URL conservati nello storico (evita che il file cresca all'infinito)
MAX_SEEN_BANDI = 1000
# Ogni quanti giorni al massimo ripetere l'alert "struttura HTML cambiata"
STRUCTURE_ALERT_COOLDOWN_DAYS = 7
KEYWORDS = ["funzionario", "specialista", "analista", "sistemista", "esperto", "ingegnere", "informatic", "ict", "cyber", "sicurezza", "digitale"]
WEBSITE_URL_ROOT = "https://www.concorsipubblici.com"
WEBSITE_URL_LIST = "https://www.concorsipubblici.com/concorsi/occupazione/pro/settore-informatico-600"

# ====================== FUNZIONI DI SCRAPING CON PLAYWRIGHT ======================

def scrape_with_playwright(url):
    """Usa Playwright per superare la protezione anti-robot"""
    # Import locale: i test offline sostituiscono questa funzione e non devono
    # avere bisogno di Playwright (né dei binari del browser) per girare.
    from playwright.sync_api import sync_playwright

    print(f"🌐 Scraping con Playwright: {url}")

    with sync_playwright() as p:
        # Usa Chromium con fingerprint reali
        browser = p.chromium.launch(
            headless=True,
            args=[
                '--disable-blink-features=AutomationControlled',
                '--disable-dev-shm-usage',
                '--no-sandbox',
                '--disable-setuid-sandbox',
                '--disable-web-security',
                '--disable-features=IsolateOrigins,site-per-process',
            ]
        )

        # Context con fingerprint reali
        context = browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
            locale='it-IT',
            timezone_id='Europe/Rome',
            extra_http_headers={
                'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8',
                'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
            }
        )

        page = context.new_page()

        try:
            # Naviga con timeout più alto
            page.goto(url, timeout=30000, wait_until='networkidle')

            # Aspetta qualche secondo extra per far caricare tutto
            page.wait_for_timeout(3000)

            # Prende il contenuto HTML
            html = page.content()

            print(f"   ✓ Pagina caricata - Lunghezza: {len(html)} caratteri")

            browser.close()
            return html

        except Exception as e:
            print(f"   ❌ Errore Playwright: {e}")
            browser.close()
            raise

def is_bando_scaduto(articolo):
    """Determina se un bando (teaser <article>) risulta scaduto.

    Il sito marca i bandi scaduti in tre modi ridondanti: la classe 'is-expired'
    sull'article, il badge <span class="expired">Scaduto</span> e la data in
    <time datetime="...">. Li controlliamo tutti per non dipendere da un solo
    dettaglio dell'HTML.
    """
    if 'is-expired' in articolo.get('class', []):
        return True

    if articolo.find('span', class_='expired'):
        return True

    time_el = articolo.find('time')
    if time_el and time_el.get('datetime'):
        try:
            scadenza = datetime.fromisoformat(time_el['datetime'].replace('Z', '+00:00'))
            if scadenza.tzinfo is None:
                scadenza = scadenza.replace(tzinfo=timezone.utc)
            if scadenza < datetime.now(timezone.utc):
                return True
        except ValueError:
            pass

    return False

def scrape_bandi():
    """Scrape la pagina dei bandi.

    Ritorna la tupla (tutti, attivi): serve distinguere il caso "la pagina non
    contiene più nessun bando" (struttura HTML cambiata o scraping bloccato)
    dal caso normalissimo "ci sono bandi ma sono tutti scaduti".
    """
    try:
        html = scrape_with_playwright(WEBSITE_URL_LIST)
        soup = BeautifulSoup(html, 'html.parser')

        # Metodo 1: Cerca il container view-content
        view_content = soup.find('div', class_='view-content')

        if view_content:
            articoli = view_content.find_all('article', class_='node--type-contest')
            print(f"   ✓ Trovati {len(articoli)} bandi nel view-content")
        else:
            # Metodo 2: Cerca direttamente tutti gli article
            articoli = soup.find_all('article', class_='node--type-contest')
            print(f"   ✓ Trovati {len(articoli)} bandi nella pagina")

        # Metodo 3 (fallback): la classe Drupal potrebbe cambiare, ripieghiamo
        # su qualunque <article> che contenga un titolo con link.
        if not articoli:
            articoli = [a for a in soup.find_all('article') if a.find('h2') and a.find('h2').find('a')]
            if articoli:
                print(f"   ✓ Trovati {len(articoli)} bandi con il selettore di fallback")

        attivi = [art for art in articoli if not is_bando_scaduto(art)]

        scaduti = len(articoli) - len(attivi)
        if scaduti:
            print(f"   ✓ Filtrati {scaduti} bandi scaduti")

        return articoli, attivi

    except Exception as e:
        print(f"❌ Errore in scrape_bandi: {e}")
        raise

def fetch_bando_details(bando_url):
    """Scrape la pagina dettagli di un singolo bando usando Playwright"""
    try:
        # Pausa casuale per sembrare più umano
        time.sleep(random.uniform(3, 6))

        html = scrape_with_playwright(bando_url)
        soup = BeautifulSoup(html, 'html.parser')
        testo_completo = soup.get_text(separator='\n', strip=True)

        print(f"   ✓ Dettagli scaricati - Lunghezza testo: {len(testo_completo)} caratteri")
        return testo_completo

    except Exception as e:
        print(f"❌ Errore in fetch_bando_details ({bando_url}): {e}")
        raise

# ====================== RESTO DELLE FUNZIONI ======================

def load_state():
    """Carica lo stato del monitor da seen_bandi.json.

    Accetta anche il vecchio formato (lista semplice di URL) per retrocompatibilità.
    """
    try:
        with open(SEEN_BANDI_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        data = None

    if isinstance(data, list):
        return {"seen": data, "last_structure_alert": None}
    if isinstance(data, dict):
        return {
            "seen": data.get("seen", []),
            "last_structure_alert": data.get("last_structure_alert"),
        }
    return {"seen": [], "last_structure_alert": None}

def save_state(state):
    """Salva lo stato del monitor su seen_bandi.json."""
    state["seen"] = state["seen"][-MAX_SEEN_BANDI:]
    with open(SEEN_BANDI_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)

def send_message_to_telegram(message):
    """Invia un messaggio testuale al bot Telegram configurato."""
    try:
        response = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            data={"chat_id": TELEGRAM_CHAT_ID, "text": message},
            timeout=10
        )
        response.raise_for_status()
    except Exception as e:
        print(f"Errore invio Telegram: {e}")

def notify_error(message):
    """Invia un errore tecnico su Telegram solo se NOTIFY_ERRORS è attivo."""
    print(message)
    if NOTIFY_ERRORS:
        send_message_to_telegram(message)

def load_profilo():
    """Carica il profilo utente da variabile d'ambiente."""
    profilo = os.environ.get('PROFILO')
    if not profilo:
        raise ValueError("Variabile d'ambiente PROFILO non configurata")
    return profilo

def read_ai_prompt():
    """Legge il prompt AI da un file di testo."""
    try:
        with open("ai_prompt.txt", "r", encoding="utf-8") as f:
            return f.read()
    except FileNotFoundError:
        raise FileNotFoundError("File ai_prompt.txt non trovato")

def ai_evaluate_bando(testo_bando, profilo):
    """Usa un modello AI per valutare se il bando è adatto al profilo dell'utente."""
    if not OPENROUTER_API_KEY:
        raise ValueError("Variabile d'ambiente OPENROUTER_API_KEY non configurata")
    ai_prompt = read_ai_prompt()
    prompt = ai_prompt.format(testo_bando=testo_bando, profilo=profilo)
    for model in AI_MODELS:
        try:
            print(f"➡️ Chiamata API a {model}...")
            response = requests.post(
                "https://openrouter.ai/api/v1/chat/completions",
                headers={"Authorization": f"Bearer {OPENROUTER_API_KEY}"},
                json={
                    "model": model,
                    "messages": [{"role": "user", "content": prompt}]
                },
                timeout=30
            )
            response.raise_for_status()
            print(f"⬅️ Risposta da {model}")
            return response.json()['choices'][0]['message']['content'].strip()
        except Exception as e:
            print(f"Errore con modello {model}: {e}, provo il prossimo...")
            time.sleep(2)  # Aspetta 2 secondi prima del prossimo tentativo
            continue
    return "⚠️ Valutazione AI non disponibile al momento."

def should_send_structure_alert(state):
    """L'alert 'struttura cambiata' va inviato al massimo una volta ogni N giorni."""
    ultimo = state.get("last_structure_alert")
    if not ultimo:
        return True
    try:
        ultimo_dt = datetime.fromisoformat(ultimo)
    except (TypeError, ValueError):
        return True
    if ultimo_dt.tzinfo is None:
        ultimo_dt = ultimo_dt.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - ultimo_dt >= timedelta(days=STRUCTURE_ALERT_COOLDOWN_DAYS)

def estrai_bandi_con_keywords(articoli):
    """Dagli <article> attivi estrae gli URL dei bandi che matchano le keywords."""
    urls = []
    for idx, bando in enumerate(articoli, 1):
        print(f"\n📌 Analizzo bando {idx}/{len(articoli)}")

        # Trova il titolo (h2 dentro l'article)
        titolo_element = bando.find('h2')
        if not titolo_element:
            print("   ⚠️ Nessun elemento titolo trovato")
            continue

        titolo = titolo_element.get_text(strip=True)
        print(f"   Titolo: {titolo[:100]}")

        # Verifica se contiene le keywords
        keyword_trovate = [kw for kw in KEYWORDS if kw in titolo.lower()]
        if not keyword_trovate:
            print("   ❌ Nessuna keyword match")
            continue

        print(f"   ✅ Match keyword: {keyword_trovate}")

        # Trova il link nell'elemento h2
        link_element = titolo_element.find('a')
        if not link_element or not link_element.get('href'):
            print("   ⚠️ Nessun link trovato")
            continue

        bando_url = urljoin(WEBSITE_URL_ROOT, link_element['href'])
        urls.append(bando_url)
        print(f"   ✅ URL aggiunto: {bando_url}")

    return urls

# MAIN
def main():
    """Esegue il monitor: scarica i bandi, rileva i nuovi e invia le notifiche Telegram."""
    try:
        print("🚀 Avvio monitor concorsi...")

        # Carica configurazioni
        profilo = load_profilo()
        print(f"✓ Profilo caricato: {profilo[:50]}...")

        # Carica stato precedente
        state = load_state()
        bandi_visti = state["seen"]
        print(f"✓ Bandi già visti: {len(bandi_visti)}")

        # Scrape bandi
        print("🌐 Recupero bandi...")
        tutti, bandi_attivi = scrape_bandi()

        # Nessun article nella pagina => lo scraping è rotto davvero.
        # È l'unico caso in cui avvisiamo, e comunque non più di una volta
        # ogni STRUCTURE_ALERT_COOLDOWN_DAYS giorni.
        if not tutti:
            print("⚠️ Nessun bando trovato nella pagina!")
            if should_send_structure_alert(state):
                send_message_to_telegram(
                    "⚠️ Attenzione: Nessun bando trovato nella pagina. "
                    "Potrebbe essere cambiata la struttura HTML."
                )
                state["last_structure_alert"] = datetime.now(timezone.utc).isoformat()
                save_state(state)
            else:
                print("   (alert già inviato di recente, non lo ripeto)")
            return

        # Lo scraping funziona: azzera il flag dell'alert
        state["last_structure_alert"] = None

        print(f"✓ Trovati {len(tutti)} bandi in pagina, di cui {len(bandi_attivi)} ancora aperti")

        # Tutti scaduti: è una situazione normale, nessun messaggio.
        if not bandi_attivi:
            print("✅ Nessun bando attualmente aperto. Nessuna notifica da inviare.")
            save_state(state)
            return

        # Filtra per keywords e estrai URL
        bandi_trovati = estrai_bandi_con_keywords(bandi_attivi)
        print(f"\n📊 Riepilogo: {len(bandi_trovati)} bandi aperti con le keywords")

        # Identifica bandi nuovi
        bandi_nuovi = [url for url in bandi_trovati if url not in bandi_visti]
        print(f"🆕 Bandi nuovi: {len(bandi_nuovi)}")

        # Processa bandi nuovi
        for i, url in enumerate(bandi_nuovi, 1):
            print(f"\n📝 Processo bando nuovo {i}/{len(bandi_nuovi)}")
            print(f"   URL: {url}")

            try:
                # Scarica dettagli del bando
                print("   📥 Scarico dettagli...")
                testo_bando = fetch_bando_details(url)

                # Valuta con AI
                print("   🤖 Valutazione AI in corso...")
                valutazione = ai_evaluate_bando(testo_bando, profilo)

                # Prepara messaggio
                messaggio = f"🆕 Nuovo bando trovato!\n\n🔗 {url}\n\n🤖 {valutazione}"

                # Invia a Telegram
                print("   📤 Invio a Telegram...")
                send_message_to_telegram(messaggio)

                # Segna come visto solo dopo l'invio riuscito, così un bando
                # fallito viene ritentato alla prossima esecuzione.
                state["seen"].append(url)

                print("   ✅ Bando processato con successo")

            except Exception as e:
                notify_error(f"⚠️ Errore processando bando: {url}\nErrore: {str(e)[:200]}")
                continue

        if not bandi_nuovi:
            print("\n✅ Nessun nuovo bando trovato.")

        # Salva lo stato aggiornato
        save_state(state)
        print(f"\n💾 Bandi in storico: {len(state['seen'])}")
        print("✅ Monitor completato con successo!")

    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        print(f"❌ ERRORE CRITICO: {error_details}")
        notify_error(f"⚠️ Errore critico nel monitor: {str(e)[:300]}")
        raise  # Rilancia l'eccezione per debugging

if __name__ == "__main__":
    main()
