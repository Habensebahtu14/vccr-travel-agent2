import streamlit as st
import sqlite3
import pandas as pd
import os
from anthropic import Anthropic
from system_prompt import SYSTEM_PROMPT

# ── Config ─────────────────────────────────────────────────────────────
# os.environ["ANTHROPIC_API_KEY"] = "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"
DB_PATH = "../data/agent.db"
MODEL = "claude-sonnet-4-20250514"
anthropic_client = Anthropic()

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
    messages = [{"role": "user", "content": vraag}]

    response = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=1000,
        temperature=0,
        system=system,
        messages=messages,
    )
    llm_antwoord = response.content[0].text

    if "```sql" in llm_antwoord:
        sql = llm_antwoord.split("```sql")[1].split("```")[0].strip()
        resultaat = voer_sql_uit(sql)

        messages.append({"role": "assistant", "content": llm_antwoord})
        messages.append({"role": "user", "content":
            f"Het resultaat van de query is:\n\n{resultaat}\n\n"
            f"Geef nu een duidelijk antwoord in het Nederlands. Geen SQL meer."
        })

        final_response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=1000,
            temperature=0,
            system=system,
            messages=messages,
        )
        return final_response.content[0].text
    else:
        return llm_antwoord


def genereer_sql(vraag, pers_nummer, groep):
    """Genereert alleen de SQL-query voor een datavraag (voor evaluatie)."""
    system = SYSTEM_PROMPT.format(pers_nummer=pers_nummer, groep=groep)
    response = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=1000,
        temperature=0,
        system=system,
        messages=[{"role": "user", "content": vraag}],
    )
    llm_antwoord = response.content[0].text
    if "```sql" in llm_antwoord:
        return llm_antwoord.split("```sql")[1].split("```")[0].strip()
    return None


def bepaal_vraagtype(vraag):
    """Bepaalt of de vraag een data-vraag (SQL) of beleidsvraag (document) is."""
    response = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=50,
        temperature=0,
        system="""Je bent een router voor een mobiliteits-chatbot.
            Bepaal of de vraag van de werknemer gaat over:
            - DATA: persoonlijke reisgegevens, kosten, trajecten, transacties, gebruik, statistieken (→ database query nodig)
            - BELEID: regels, procedures, rechten, wat mag/moet, hoe werkt iets, wat is een X (→ beleidsdocument nodig)
            - BEIDE: combinatie van persoonlijke data én beleidsregels

            Antwoord met ALLEEN één woord: DATA, BELEID, of BEIDE""",
                    messages=[{"role": "user", "content": vraag}],
        )
    return response.content[0].text.strip().lower()


def beantwoord_beleidsvraag(vraag, groep):
    """Beantwoordt een beleidsvraag via classify-then-retrieve (Functie 2)."""
    labels = classificeer_vraag(vraag, groep)
    if labels == ["GEEN_MATCH"]:
        return "Sorry, ik kan deze vraag niet beantwoorden op basis van het mobiliteitsbeleid. Neem contact op met Forensz (forensz@vccr.nl) voor hulp."

    context = haal_chunks_op(labels, groep)

    response = anthropic_client.messages.create(
        model=MODEL,
        max_tokens=1000,
        temperature=0,
        system=f"""Je bent een vriendelijke mobiliteitsadviseur voor werknemers van VCCR/Forensz.
            Beantwoord de vraag van de werknemer op basis van het onderstaande mobiliteitsbeleid.
            Geef een duidelijk, concreet antwoord in het Nederlands.
            Verwijs naar Forensz (forensz@vccr.nl) als de werknemer actie moet ondernemen.
            Als het antwoord niet in de context staat, zeg dat eerlijk.

    MOBILITEITSBELEID:
    {context}""",
            messages=[{"role": "user", "content": vraag}],
        )
    return response.content[0].text


def stel_vraag(vraag, pers_nummer, groep):
    """Hoofdfunctie: routeert naar de juiste handler. Geeft (antwoord, vraagtype) terug."""
    vraagtype = bepaal_vraagtype(vraag)

    if vraagtype == "beleid":
        antwoord = beantwoord_beleidsvraag(vraag, groep)
    elif vraagtype == "data":
        antwoord = beantwoord_datavraag(vraag, pers_nummer, groep)
    elif vraagtype == "beide":
        data_antwoord = beantwoord_datavraag(vraag, pers_nummer, groep)
        beleid_antwoord = beantwoord_beleidsvraag(vraag, groep)
        response = anthropic_client.messages.create(
            model=MODEL,
            max_tokens=1200,
            temperature=0,
            system="Je bent een mobiliteitsadviseur. Combineer de onderstaande twee antwoorden tot één samenhangend antwoord in het Nederlands. Vermijd herhaling.",
            messages=[{"role": "user", "content": f"Vraag: {vraag}\n\nReisdata-antwoord:\n{data_antwoord}\n\nBeleid-antwoord:\n{beleid_antwoord}"}],
        )
        antwoord = response.content[0].text
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

# ── Chat Interface ────────────────────────────────────────────────────
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
        "beide": "🔄 Gecombineerd (SQL + Document)",
    }

    # Toon eerdere berichten
    for message in st.session_state.messages:
        with st.chat_message(message["role"]):
            st.markdown(message["content"])
            if message["role"] == "assistant" and "vraagtype" in message:
                st.caption(_TYPE_LABELS.get(message["vraagtype"], ""))
    
    # Chat input
    if vraag := st.chat_input("Stel een vraag over je reizen..."):
        # Toon de vraag
        with st.chat_message("user"):
            st.markdown(vraag)
        st.session_state.messages.append({"role": "user", "content": vraag})
        
        # Genereer antwoord
        with st.chat_message("assistant"):
            with st.spinner("Even denken..."):
                antwoord, vraagtype = stel_vraag(vraag, pers_nummer, groep)
            st.markdown(antwoord)
            st.caption(_TYPE_LABELS.get(vraagtype, ""))
        st.session_state.messages.append({"role": "assistant", "content": antwoord, "vraagtype": vraagtype})