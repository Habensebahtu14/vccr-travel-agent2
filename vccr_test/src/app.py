from pathlib import Path
import streamlit as st
import sqlite3
import pandas as pd
import os
from openai import AzureOpenAI
from system_prompt import SYSTEM_PROMPT
import requests
import json
from datetime import datetime, timezone, timedelta

# ── Config ─────────────────────────────────────────────────────────────
DB_PATH = str(Path(__file__).parent.parent / "data" / "agent.db")
AZURE_OPENAI_API_KEY = os.environ.get("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT = os.environ.get("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION = os.environ.get("AZURE_OPENAI_API_VERSION", "2024-12-01-preview")
AZURE_OPENAI_DEPLOYMENT = os.environ.get("AZURE_OPENAI_DEPLOYMENT", "gpt-4.1-mini")
NS_API_KEY = os.environ.get("NS_API_KEY")

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


def bepaal_vraagtype(vraag):
    sys_prompt = """Je bent een router voor een mobiliteits-chatbot.
            Bepaal of de vraag van de werknemer gaat over:
            - DATA: persoonlijke reisgegevens, gemaakte kosten, eerdere trajecten (→ database query nodig)
            - BELEID: regels, procedures, rechten, vergoedingen (→ beleidsdocument nodig)
            - ADVIES: toekomstig reisadvies, routeplanning, actuele treintijden (van A naar B) (→ API nodig)
            - BEIDE: combinatie van persoonlijke data én beleidsregels

            Antwoord met ALLEEN één woord: DATA, BELEID, ADVIES of COMBINATIE"""
    messages = [{"role": "system", "content": sys_prompt}, {"role": "user", "content": vraag}]
    response = _chat_completion(messages, max_tokens=50, temperature=0)
    return response.strip().lower()

def beantwoord_reisadvies_vraag(vraag):
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

def stel_vraag(vraag, pers_nummer, groep):
    """Hoofdfunctie: routeert naar de juiste handler. Geeft (antwoord, vraagtype) terug."""
    vraagtype = bepaal_vraagtype(vraag)

    if vraagtype == "beleid":
        antwoord = beantwoord_beleidsvraag(vraag, groep)
    elif vraagtype == "data":
        antwoord = beantwoord_datavraag(vraag, pers_nummer, groep)
    elif vraagtype == "advies":
        antwoord = beantwoord_reisadvies_vraag(vraag)
    elif vraagtype == "beide":
        data_antwoord = beantwoord_datavraag(vraag, pers_nummer, groep)
        beleid_antwoord = beantwoord_beleidsvraag(vraag, groep)
        messages = [
            {"role": "system", "content": "Je bent een mobiliteitsadviseur. Combineer de onderstaande twee antwoorden tot één samenhangend antwoord in het Nederlands. Vermijd herhaling."},
            {"role": "user", "content": f"Vraag: {vraag}\n\nReisdata-antwoord:\n{data_antwoord}\n\nBeleid-antwoord:\n{beleid_antwoord}"},
        ]
        antwoord = _chat_completion(messages, max_tokens=1200, temperature=0)
    else:
        antwoord = beantwoord_datavraag(vraag, pers_nummer, groep)
        vraagtype = "data"

    return antwoord, vraagtype

# ── Streamlit Interface ────────────────────────────────────────────────
st.set_page_config(page_title="Forensz Reisassistent", page_icon="🚆", layout="wide")

st.title("🚆 Forensz Reisassistent")
st.caption("AI-agent voor persoonlijk OV-reisadvies")

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
        "advies": "🚆 Reisadvies (NS API)",
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
    