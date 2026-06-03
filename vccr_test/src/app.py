from pathlib import Path
import re
import gzip
import io
import streamlit as st
import sqlite3
import pandas as pd
import os
from openai import AzureOpenAI
from system_prompt import SYSTEM_PROMPT
import requests
import json
from datetime import datetime, timezone, timedelta
from lxml import etree
from traffic_helper import get_traffic_info

# ── Config ─────────────────────────────────────────────────────────────
DB_PATH = str(Path(__file__).parent.parent / "data" / "agent.db")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
NS_API_KEY = os.environ.get("NS_API_KEY")
GOOGLE_MAPS_API_KEY = ("AIzaSyCqk3PC6RBMQzR6rUtqHavWEOFxczZDsOA")

# Client (will read credentials from environment if not passed)
azure_client = AzureOpenAI(
    api_key=AZURE_OPENAI_API_KEY,
    api_version=AZURE_OPENAI_API_VERSION,
    azure_endpoint=AZURE_OPENAI_ENDPOINT)


def _chat_completion(messages, max_tokens=1000, temperature=0):
    """Kleine wrapper rond AzureOpenAI chat completion, geeft tekst terug."""
    try:
        resp = azure_client.chat.completions.create(
            model=AZURE_OPENAI_DEPLOYMENT,
            messages=messages,
            max_tokens=max_tokens,
            temperature=temperature,
        )
        # verschillende response-vormen afdekken
        try:
            return resp.choices[0].message.content
        except Exception:
            try:
                return resp.choices[0].message["content"]
            except Exception:
                return str(resp)
    except Exception as e:
        return f"FOUT_LLM: {e}"

# Importeer classifier NA instellen van de API-key (client wordt bij import aangemaakt)
from classifier import classificeer_vraag, haal_chunks_op  # noqa: E402

# ── Database functies ──────────────────────────────────────────────────
def voer_sql_uit(sql):
    try:
        conn = sqlite3.connect(DB_PATH)
        result = pd.read_sql(sql, conn)
        conn.close()
        return result.to_string(index=False)
    except Exception as e:
        return f"FOUT: {e}"

def haal_werknemer_info(pers_nummer):
    conn = sqlite3.connect(DB_PATH)
    info = pd.read_sql(f"""
        SELECT Personeelsnummer, Status, Klasse, Abonnement, groep
        FROM kaarten 
        WHERE Personeelsnummer = '{pers_nummer}'
        LIMIT 1
    """, conn)
    conn.close()
    return info

def haal_alle_personeelsnummers():
    conn = sqlite3.connect(DB_PATH)
    nummers = pd.read_sql("""
        SELECT DISTINCT Personeelsnummer, groep, Status
        FROM kaarten 
        WHERE Status = 'Actief'
        ORDER BY groep, Personeelsnummer
    """, conn)
    conn.close()
    return nummers

# ── Agent functies ─────────────────────────────────────────────────────
def beantwoord_datavraag(vraag, pers_nummer, groep):
    """Beantwoordt een data-vraag via Text-to-SQL (Functie 1)."""
    system = SYSTEM_PROMPT.format(pers_nummer=pers_nummer, groep=groep)
    messages = [{"role": "system", "content": system}, {"role": "user", "content": vraag}]
    llm_antwoord = _chat_completion(messages, max_tokens=1000, temperature=0)

    if "```sql" in llm_antwoord:
        sql = llm_antwoord.split("```sql")[1].split("```")[0].strip()
        resultaat = voer_sql_uit(sql)

        messages.append({"role": "assistant", "content": llm_antwoord})
        messages.append({"role": "user", "content":
            f"Het resultaat van de query is:\n\n{resultaat}\n\n"
            f"Geef nu een duidelijk antwoord in het Nederlands. Geen SQL meer."
        })

        final_response = _chat_completion(messages, max_tokens=1000, temperature=0)
        return final_response
    else:
        return llm_antwoord

def bepaal_vraagtype(vraag: str) -> str:
    """Routeert de vraag naar het juiste type handler."""
    sys_prompt = """Je bent een router voor een mobiliteits-chatbot.
Bepaal of de vraag van de werknemer gaat over:
- DATA: persoonlijke reisgegevens, gemaakte kosten, eerdere trajecten (→ database query nodig)
- BELEID: regels, procedures, rechten, vergoedingen (→ beleidsdocument nodig)
- OV-ADVIES: toekomstig reisadvies, routeplanning, actuele treintijden (van A naar B) (→ NS API)
- WEG-ADVIES: actuele verkeerssituatie, files, incidenten op de weg (→ Google Maps API)
- BEIDE: combinatie van persoonlijke data én beleidsregels
 
Antwoord met ALLEEN één woord: DATA, BELEID, OV-ADVIES, WEG-ADVIES of BEIDE"""
 
    messages = [
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": vraag}
    ]
    response = _chat_completion(messages, max_tokens=50, temperature=0)
 
    # Normaliseer: lowercase, strip witruimte, aliassen opvangen
    vraagtype = response.strip().lower()
    aliassen = {
        "advies":      "ov-advies",
        "ov advies":   "ov-advies",
        "weg advies":  "weg-advies",
        "combinatie":  "beide",
    }
    return aliassen.get(vraagtype, vraagtype)


def extract_road_number(vraag: str) -> str | None:
    """Zoek een snelweg- of hoofdwegnummer in de vraag."""
    match = re.search(r"\b([AaNn])\s?(\d{1,3})\b", vraag)
    if not match:
        return None
    return f"{match.group(1).upper()}{match.group(2)}"


def summarize_traffic_data(road_data: dict, road_number: str) -> str:
    """Gebruik het LLM om een leesbaar antwoord te maken uit ANWB-verkeersdata."""
    if road_data.get("error"):
        return f"Fout bij ophalen: {road_data.get('details')}"
    if road_data.get("message"):
        return road_data["message"]

    prompt = f"""Je bent een Nederlandse verkeersassistent.
Je krijgt ANWB verkeersdata voor weg {road_number}.
Geef een kort, duidelijk en begrijpelijk antwoord in het Nederlands over de actuele situatie op deze weg.
Noem de belangrijkste incidenten, files en wegwerkzaamheden die op dit moment van invloed zijn.
Als er geen incidenten zijn, zeg dan dat de weg momenteel geen meldingen heeft.
Gebruik geen JSON of code in je antwoord.

Data:
{json.dumps(road_data, ensure_ascii=False)}"""

    messages = [
        {"role": "system", "content": "Je bent een behulpzame en beknopte verkeersassistent."},
        {"role": "user", "content": prompt},
    ]
    return _chat_completion(messages, max_tokens=400, temperature=0.2)


def beantwoord_reisadvies_vraag(vraag):
    road_number = extract_road_number(vraag)
    if road_number:
        road_data = get_traffic_info(road_number)
        return summarize_traffic_data(road_data, road_number)

    # 1. Bepaal de huidige tijd in Nederland
    nl_timezone = timezone(timedelta(hours=2))
    nu_nl = datetime.now(nl_timezone)
    
    api_tijd = nu_nl.strftime("%Y-%m-%dT%H:%M:%S")
    leesbare_tijd = nu_nl.strftime("%H:%M")

    # 2. Haal stations uit de vraag met de LLM
    extractie_prompt = """Haal het vertrekstation en aankomststation uit de volgende vraag.
    Antwoord ALLEEN in dit format: Vertrekstation|Aankomststation
    Bijvoorbeeld: Den Haag Centraal|Utrecht Centraal"""
    
    messages = [{"role": "system", "content": extractie_prompt}, {"role": "user", "content": vraag}]
    stations_str = _chat_completion(messages, max_tokens=50, temperature=0)
    
    try:
        van_station, naar_station = stations_str.split('|')
    except ValueError:
        return "Sorry, ik kon de stations niet goed uit je vraag halen. Kun je je vraag stellen als 'Hoe kom ik van [station] naar [station]'?"

    # 3. Roep de NS API aan
    ns_key = os.environ.get("NS_API_KEY")
    if not ns_key:
        return "Fout: De NS_API_KEY ontbreekt in de omgeving."
        
    url = "https://gateway.apiportal.ns.nl/reisinformatie-api/api/v3/trips"
    headers = {'Ocp-Apim-Subscription-Key': ns_key}
    
    params = {
        'fromStation': van_station.strip(), 
        'toStation': naar_station.strip(),
        'dateTime': api_tijd
    }
    
    try:
        response = requests.get(url, headers=headers, params=params)
        if response.status_code != 200:
            return f"Oeps, de NS API gaf een foutmelding (Code: {response.status_code}). Misschien herkent de API het station '{van_station.strip()}' of '{naar_station.strip()}' niet."
        api_data = response.json()
    except Exception as e:
        return f"Er ging iets mis met het ophalen van de NS data: {e}"
    
    # 4. FILTER TREINEN DIE AL WEG ZIJN ERUIT
    # We tellen 2 minuten op bij de huidige tijd als 'looptijd' naar het perron
    loop_tijd = nu_nl + timedelta(minutes=2)
    loop_tijd_iso = loop_tijd.strftime("%Y-%m-%dT%H:%M:%S")
    
    gekozen_reis = None
    if "trips" in api_data:
        for trip in api_data["trips"]:
            try:
                # De NS API geeft tijden in dit format: "2026-05-20T12:45:00+0200"
                # We pakken de eerste 19 tekens en vergelijken deze met onze "looptijd"
                vertrek_tijd_trip = trip["legs"][0]["origin"]["plannedDateTime"][:19]
                
                # Ligt deze trein in de toekomst? Dan pakken we hem en stoppen we met zoeken!
                if vertrek_tijd_trip >= loop_tijd_iso:
                    gekozen_reis = trip
                    break
            except (KeyError, IndexError):
                continue
                
    # Fallback: als we niks vonden (wat zeldzaam is), pakken we de tweede trein in de lijst
    if not gekozen_reis and "trips" in api_data and len(api_data["trips"]) > 1:
        gekozen_reis = api_data["trips"][1]
    # Uiterste fallback: gewoon de eerste pakken als er echt maar 1 in de lijst zit
    elif not gekozen_reis and "trips" in api_data and len(api_data["trips"]) > 0:
        gekozen_reis = api_data["trips"][0]
        
    if not gekozen_reis:
        return f"Ik kon helaas geen aankomende reis vinden tussen {van_station} en {naar_station}."

    reis_json_str = json.dumps(gekozen_reis)

    # 5. Laat de LLM de ruwe JSON vertalen (met kennis van de huidige tijd)
    format_prompt = f"""Je bent een behulpzame NS-reisassistent. Het is momenteel {leesbare_tijd} uur.
    Hier is ruwe JSON data van de NS API voor het éérstvolgende haalbare reisadvies van {van_station} naar {naar_station}.
    Haal de geplande vertrektijd, aankomsttijd, reistijd, het vertrekspoor en eventuele overstappen eruit.
    Maak er een kort, vlot en leesbaar Nederlands antwoord van. Vertel in hoeveel minuten de trein vertrekt.
    Gebruik geen JSON of programmeercode in je antwoord.
    
    Data: {reis_json_str}"""
    
    messages_format = [{"role": "system", "content": "Je vertaalt JSON naar leesbare tekst."}, {"role": "user", "content": format_prompt}]
    return _chat_completion(messages_format, max_tokens=600, temperature=0.3)

def beantwoord_verkeersadvies_vraag(vraag):
    """Beantwoordt een verkeersvraag via Google Maps API."""
    verkeerscontext = haal_verkeersinfo(vraag)
    
    messages = [
        {
            "role": "system",
            "content": (
                "Je bent een Nederlandse verkeersassistent. "
                "Beantwoord de vraag op basis van de actuele verkeersdata van Google Maps. "
                "Wees beknopt en praktisch. Geef concrete adviezen over files, incidenten en reistijden."
            ),
        },
        {
            "role": "user",
            "content": f"Actuele verkeersdata:\n{verkeerscontext}\n\nVraag: {vraag}",
        },
    ]
    return _chat_completion(messages, max_tokens=600, temperature=0)


def haal_verkeersinfo(vraag):
    """Haalt actuele verkeersinformatie op via Google Maps API."""
    
    if not GOOGLE_MAPS_API_KEY:
        return f"❌ **GOOGLE_MAPS_API_KEY ONTBREEKT**\n\nDe omgevingsvariabele GOOGLE_MAPS_API_KEY is niet ingesteld. Voer dit uit in de terminal:\n```\nexport GOOGLE_MAPS_API_KEY=\"your_actual_google_maps_api_key\"\n```"
    
    # Stap 1: Probeer het vertrekpunt en bestemming uit de vraag te extraheren
    locatie_prompt = """Je bent een locatie-extractor. Haal het vertrekpunt en bestemming uit de verkeers/routevraag.
    
    Mogelijke outputs:
    - "FROM: Amsterdam TO: Rotterdam" (als je duidelijke locaties herkent)
    - "LOCATION: A4" of "LOCATION: Amsterdam centrum" (als het om een specifieke plaats/weg gaat)
    - "NETHERLANDS" (als het om algemene verkeersinfo gaat)

    Vraag: """ + vraag + """

    Antwoord ALLEEN met één van de bovenstaande formats."""
    
    messages = [
        {"role": "system", "content": "Je bent een Nederland-geografische expert."},
        {"role": "user", "content": locatie_prompt}
    ]
    
    locatie_bepaling = _chat_completion(messages, max_tokens=100, temperature=0).strip()
    print(f"[DEBUG] Locatie bepaling: {locatie_bepaling}")
    
    # Stap 2: Bepaal origin en destination
    origin = None
    destination = None
    
    if "FROM:" in locatie_bepaling and "TO:" in locatie_bepaling:
        try:
            parts = locatie_bepaling.split("FROM:")[1].split("TO:")
            origin = parts[0].strip()
            destination = parts[1].strip()
            print(f"[DEBUG] Extracteerde route: van {origin} naar {destination}")
        except Exception as e:
            print(f"[DEBUG] Fout bij route parsing: {e}")
            origin = None
            destination = None
    elif "LOCATION:" in locatie_bepaling:
        # Voor specifieke locaties gebruiken we Directions API rond die locatie
        location = locatie_bepaling.split("LOCATION:")[1].strip()
        # We gebruiken dezelfde locatie als origin en destination voor een 'round trip' analyse
        origin = location
        destination = location
        print(f"[DEBUG] Gebruikte locatie: {location}")
    
    # Stap 3: Fallback: Amsterdam naar Rotterdam (standaard Nederlands drukke route)
    if not origin or not destination:
        print(f"[DEBUG] Geen specifieke route gevonden, gebruik standaard route Amsterdam-Rotterdam")
        origin = "Amsterdam"
        destination = "Rotterdam"
    
    url = "https://maps.googleapis.com/maps/api/directions/json"
    
    params = {
        "origin": origin,
        "destination": destination,
        "key": GOOGLE_MAPS_API_KEY,
        "departure_time": "now",
        "traffic_model": "best_guess"
    }
    
    try:
        # DEBUG: Log wat we doen
        print(f"[DEBUG] Google Maps API call: {origin} → {destination}")
        print(f"[DEBUG] URL: {url}")
        print(f"[DEBUG] Params: {params}")
        
        response = requests.get(url, params=params, timeout=10)
        
        print(f"[DEBUG] Response status code: {response.status_code}")
        print(f"[DEBUG] Response text: {response.text[:500]}")  # Eerste 500 chars
        
        if response.status_code != 200:
            error_msg = response.text if response.text else "Onbekende fout"
            return f"❌ **Google Maps API Error (Code {response.status_code})**\n\nFout van Google Maps API:\n```\n{error_msg[:200]}\n```\n\n**Mogelijke oorzaken:**\n- GOOGLE_MAPS_API_KEY is ongeldig\n- De API key heeft onvoldoende rechten ingeschakeld\n- Rate limit bereikt\n\nCheck je API key en probeer het later opnieuw."
        
        data = response.json()
        print(f"[DEBUG] Parsed JSON successful. Status: {data.get('status')}")
        
        if data.get('status') != 'OK':
            error_msg = data.get('error_message', 'Onbekende fout')
            return f"❌ **Google Maps Error**: {error_msg}"
        
        # Parse de respons en maak het leesbaar
        verkeersinfo = f"📍 **Verkeersinfo voor route:** {origin} → {destination}\n\n"
        
        # Analyze routes
        routes = data.get('routes', [])
        if not routes:
            return verkeersinfo + "❌ Geen route gevonden."
        
        for idx, route in enumerate(routes[:3], 1):  # Top 3 routes
            legs = route.get('legs', [])
            if not legs:
                continue
                
            total_duration = 0
            total_distance = 0
            traffic_info = []
            
            for leg in legs:
                # Get duration in traffic (if available)
                duration_in_traffic = leg.get('duration_in_traffic', {})
                if duration_in_traffic:
                    duration_val = duration_in_traffic.get('value', 0) // 60  # Convert to minutes
                    total_duration += duration_val
                else:
                    duration_val = leg.get('duration', {}).get('value', 0) // 60
                    total_duration += duration_val
                
                distance_val = leg.get('distance', {}).get('value', 0) / 1000  # Convert to km
                total_distance += distance_val
                
                steps = leg.get('steps', [])
                for step in steps:
                    step_duration = step.get('duration', {}).get('value', 0)
                    if step_duration > 0:  # Alleen significante stappen
                        instruction = step.get('html_instructions', '').replace('<b>', '').replace('</b>', '').replace('<div', '').replace('</div>', '')
                        traffic_info.append(instruction)
            
            summary = route.get('summary', f'Route {idx}')
            verkeersinfo += f"🛣️ **{summary}**\n"
            verkeersinfo += f"  • ⏱️ Reistijd: **{total_duration} minuten**\n"
            verkeersinfo += f"  • 📏 Afstand: **{total_distance:.1f} km**\n"
            
            if traffic_info:
                verkeersinfo += f"  • 🚗 Routedetails:\n"
                for info in traffic_info[:5]:  # Top 5 details
                    verkeersinfo += f"    - {info}\n"
            
            verkeersinfo += "\n"
        
        return verkeersinfo
        
    except requests.exceptions.Timeout:
        return "❌ **Timeout**: Google Maps API antwoordt niet. Probeer het later opnieuw."
    except requests.exceptions.ConnectionError:
        return "❌ **Verbindingsfout**: Kan Google Maps API niet bereiken. Check je internetverbinding."
    except json.JSONDecodeError as e:
        return f"❌ **JSON Parse Error**: De API gaf een ongeldig antwoord.\n\nFout: {str(e)}"
    except Exception as e:
        return f"❌ **Onverwachte fout**: {str(e)}"

def beantwoord_beleidsvraag(vraag, groep):
    """Beantwoordt een beleidsvraag via classify-then-retrieve (Functie 2)."""
    labels = classificeer_vraag(vraag, groep)
    if labels == ["GEEN_MATCH"]:
        return "Sorry, ik kan deze vraag niet beantwoorden op basis van het mobiliteitsbeleid. Neem contact op met Forensz (forensz@vccr.nl) voor hulp."

    context = haal_chunks_op(labels, groep)

    system = f"""Je bent een vriendelijke mobiliteitsadviseur voor werknemers van VCCR/Forensz.
            Beantwoord de vraag van de werknemer op basis van het onderstaande mobiliteitsbeleid.
            Geef een duidelijk, concreet antwoord in het Nederlands.
            Verwijs naar Forensz (forensz@vccr.nl) als de werknemer actie moet ondernemen.
            Als het antwoord niet in de context staat, zeg dat eerlijk.

    MOBILITEITSBELEID:
    {context}"""
    messages = [{"role": "system", "content": system}, {"role": "user", "content": vraag}]
    return _chat_completion(messages, max_tokens=1000, temperature=0)



def stel_vraag(vraag: str, pers_nummer: str, groep: str) -> tuple[str, str]:
    """
    Hoofdfunctie: routeert naar de juiste handler.
 
    Returns:
        (antwoord, vraagtype)
    """
    vraagtype = bepaal_vraagtype(vraag)
 
    if vraagtype == "beleid":
        antwoord = beantwoord_beleidsvraag(vraag, groep)
 
    elif vraagtype == "data":
        antwoord = beantwoord_datavraag(vraag, pers_nummer, groep)
 
    elif vraagtype == "weg-advies":
        antwoord = beantwoord_verkeersadvies_vraag(vraag)
 
    elif vraagtype == "ov-advies":
        antwoord = beantwoord_reisadvies_vraag(vraag)
 
    elif vraagtype == "beide":
        data_antwoord   = beantwoord_datavraag(vraag, pers_nummer, groep)
        beleid_antwoord = beantwoord_beleidsvraag(vraag, groep)
 
        messages = [
            {
                "role": "system",
                "content": (
                    "Je bent een mobiliteitsadviseur. "
                    "Combineer de onderstaande twee antwoorden tot één samenhangend antwoord "
                    "in het Nederlands. Vermijd herhaling."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Vraag: {vraag}\n\n"
                    f"Reisdata-antwoord:\n{data_antwoord}\n\n"
                    f"Beleid-antwoord:\n{beleid_antwoord}"
                ),
            },
        ]
        antwoord = _chat_completion(messages, max_tokens=1200, temperature=0)
 
    else:
        # Onbekend vraagtype → val terug op datavraag
        antwoord  = beantwoord_datavraag(vraag, pers_nummer, groep)
        vraagtype = "data"
 
    return antwoord, vraagtype

# ── Streamlit Interface ────────────────────────────────────────────────
st.set_page_config(page_title="Forensz Reisassistent", layout="wide")

st.title("Forensz Reisassistent")
st.caption("AI-agent voor persoonlijk reisadvies")

# ── Sidebar: Login ────────────────────────────────────────────────────
with st.sidebar:
    st.header("🔑 Inloggen")
    
    alle_nummers = haal_alle_personeelsnummers()
    
    # Groep selecteren
    groep = st.selectbox("Werkgever", ["A", "B", "C"])
    
    # Personeelsnummers filteren op groep
    nummers_groep = alle_nummers[alle_nummers['groep'] == groep]['Personeelsnummer'].tolist()
    
    pers_nummer = st.selectbox(
        "Personeelsnummer", 
        nummers_groep,
        index=0 if nummers_groep else None
    )
    
    if pers_nummer:
        info = haal_werknemer_info(pers_nummer)
        if len(info) > 0:
            st.success(f"Ingelogd als: {pers_nummer}")
            st.write(f"**Status:** {info.iloc[0]['Status']}")
            st.write(f"**Klasse:** {'1e klas' if info.iloc[0]['Klasse'] == 1 else '2e klas'}")
            st.write(f"**Abonnement:** {info.iloc[0]['Abonnement']}")
    
    st.divider()
    st.caption("🗄️ Data-vragen (SQL):")
    st.markdown("""
    - Hoeveel heb ik uitgegeven?
    - Wat is mijn meest gebruikte traject?
    - Hoe vaak heb ik de OV-fiets gebruikt?
    - Reis ik meer in de spits of dal?
    - Geef mijn kosten per maand
    - Welke vervoerder gebruik ik het meest?
    - Wat was mijn duurste reis?
    - Hoeveel privéreizen heb ik gemaakt?
    """)
    st.caption("📋 Beleidsvragen (Document):")
    st.markdown("""
    - Ik ben mijn kaart kwijt, wat nu?
    - Wat is een correctietarief?
    - Kan ik upgraden naar eerste klas?
    - Hoeveel mobiliteitsbudget krijg ik?
    - Ik ga uit dienst, wat moet ik doen?
    - Hoe vraag ik OV-vrij reizen aan?
    - Wat zijn de regels voor de deelauto?
    """)
    st.caption("🚆 Reisadviesvragen (NS API):")
    st.markdown("""
    - Hoe kom ik van Den Haag Centraal naar Utrecht Centraal?
    - Wat is de snelste trein van Rotterdam Centraal naar Amsterdam Centraal?
    - Hoe laat vertrekt de eerstvolgende trein van Eindhoven naar 's-Hertogenbosch?
    - Kan ik vandaag met de trein van Leiden naar Haarlem reizen?
    - Wat is het vertrekspoor van de trein van Zwolle naar Amersfoort?
    """)
    st.caption(" Verkeersdata (NDW data):")
    st.markdown("""
    - Hoe is de verkeerssituatie op de A12?
    - Zijn er files op de A4 richting Amsterdam?
    - Wat is de reistijd op de A2 richting Utrecht?
    """)


    st.divider()
    st.header("🚗 Wegverkeer (ANWB)")
    st.caption("Haal actuele incidenten op voor een snelweg (bijv. A1)")
    road_input = st.text_input("Wegnummer", value="")
    if st.button("Haal verkeersinfo op"):
        if road_input.strip():
            with st.spinner("Ophalen..."):
                road_data = get_traffic_info(road_input)
                antwoord = summarize_traffic_data(road_data, extract_road_number(road_input) or road_input)
            if isinstance(road_data, dict) and road_data.get("error"):
                st.error(f"Fout bij ophalen: {road_data.get('details')}")
            else:
                st.markdown(antwoord)
        else:
            st.info("Voer een wegnummer in, bijvoorbeeld 'A1'.")

# ── Chat Interface ────────────────────────────────────────────────────

def verrijk_vraag_met_geheugen(huidige_vraag, berichten):
    """Plakt de vorige interactie (kort) voor de huidige vraag voor context."""
    if not berichten:
        return huidige_vraag
        
    # Pak alleen de laatste beurt (max 1 vraag en 1 antwoord)
    laatste_berichten = berichten[-2:]
    
    context_str = "[CONTEXT VORIGE BEURT]\n"
    for msg in laatste_berichten:
        rol = "Gebruiker" if msg["role"] == "user" else "Assistent"
        # Haal enters weg en knip af op 200 tekens (extreem token-efficiënt!)
        tekst = msg["content"].replace("\n", " ")
        if len(tekst) > 200:
            tekst = tekst[:197] + "..."
        context_str += f"{rol}: {tekst}\n"
        
    # Let op: deze return staat nu goed, BUITEN de for-loop
    return f"{context_str}\n[HUIDIGE VRAAG]\n{huidige_vraag}" 

if not pers_nummer:
    st.info("👈 Selecteer eerst een personeelsnummer in de sidebar om te beginnen.")
else:
    # Chat geschiedenis bijhouden
    if "messages" not in st.session_state:
        st.session_state.messages = []
    
    # Reset chat als personeelsnummer verandert
    if "current_user" not in st.session_state or st.session_state.current_user != pers_nummer:
        st.session_state.messages = []
        st.session_state.current_user = pers_nummer
    
    _TYPE_LABELS = {
        "data": "🗄️ Data-vraag (SQL)",
        "beleid": "📋 Beleidsvraag (Document)",
        "ov-advies": "🚆 Reisadvies (NS API)",
        "weg-advies": "🚗 Verkeersadvies (NDW)",
        "beide": "🔄 Gecombineerd",
    }

    # Toon eerdere berichten
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["role"] == "assistant" and "vraagtype" in message:
                st.caption(_TYPE_LABELS.get(message["vraagtype"], ""))

    # De chat input staat nu weer op de juiste plek!
    if vraag := st.chat_input("Stel een vraag over je reizen..."):
        # 1. Toon de originele, korte vraag direct in de UI
        with st.chat_message("user"):
            st.markdown(vraag)
            
        # 2. Bouw de geheime, uitgebreide vraag met geheugen voor de AI
        vraag_met_geheugen = verrijk_vraag_met_geheugen(vraag, st.session_state.messages)
        
        # 3. Sla de originele (korte) vraag op in de zichtbare geschiedenis
        st.session_state.messages.append({"role": "user", "content": vraag})
        
        # 4. Genereer antwoord (maar voer de VRAAG MET GEHEUGEN aan de AI)
        with st.chat_message("assistant"):
            with st.spinner("Even denken..."):
                antwoord, vraagtype = stel_vraag(vraag_met_geheugen, pers_nummer, groep)
            st.markdown(antwoord)
            st.caption(_TYPE_LABELS.get(vraagtype, ""))
            
        st.session_state.messages.append({"role": "assistant", "content": antwoord, "vraagtype": vraagtype})  
    