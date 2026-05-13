import sqlite3
from pathlib import Path
from anthropic import Anthropic
from dotenv import load_dotenv
from pathlib import Path

load_dotenv(Path(__file__).parent.parent / ".env")

# --- Configuratie ---
MODEL = "claude-sonnet-4-20250514"
DB_PATH = str(Path(__file__).parent.parent / "data" / "agent.db")
 
client = Anthropic()


def get_label_beschrijvingen(groep: str = "voorbeeld") -> str:
    """
    Haalt alle label_id + beschrijving op uit de database
    en formatteert ze als tekst voor de classifier prompt.
    Valt terug op 'voorbeeld' als er geen groep-specifiek beleid bestaat.
    """
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    cursor.execute(
        "SELECT label_id, beschrijving, keywords FROM beleid_chunks WHERE groep = ? ORDER BY label_id",
        (groep,),
    )
    rows = cursor.fetchall()
    if not rows:
        cursor.execute(
            "SELECT label_id, beschrijving, keywords FROM beleid_chunks WHERE groep = 'voorbeeld' ORDER BY label_id"
        )
        rows = cursor.fetchall()
    conn.close()

    lines = []
    for label_id, beschrijving, keywords in rows:
        lines.append(f"- {label_id}: {beschrijving} (keywords: {keywords})")

    return "\n".join(lines)


def build_classifier_prompt(groep: str = "voorbeeld") -> str:
    """
    Bouwt het system prompt voor de classifier.
    """
    label_tekst = get_label_beschrijvingen(groep)

    return f"""Je bent een classifier voor een mobiliteitsbeleid-chatbot.
        Je taak: bepaal welke categorie(ën) uit het mobiliteitsbeleid relevant zijn
        voor de vraag van een werknemer.

        REGELS:
        1. Kies 1 tot maximaal 3 labels die relevant zijn voor de vraag.
        2. Kies ALLEEN labels uit de onderstaande lijst — verzin geen nieuwe labels.
        3. Als de vraag niet past bij ENIG label, antwoord dan met: GEEN_MATCH
        4. Antwoord ALLEEN met de label_id's, gescheiden door komma's. Geen uitleg, geen extra tekst.

        BESCHIKBARE LABELS:
        {label_tekst}

        VOORBEELDEN:
        Vraag: "Hoeveel budget krijg ik per maand?" → mobiliteitsbudget_overzicht
        Vraag: "Ik ben mijn kaart kwijt" → kaart_kwijt
        Vraag: "Mag ik eerste klas reizen?" → ov_vrij_reizen, ns_business_card
        Vraag: "Wat gebeurt er als ik privé rij met de bedrijfswagen?" → bedrijfswagen_rittenadministratie, controle_sancties
        Vraag: "Hoe declareer ik zakelijke kilometers?" → fiscale_ruimte
        Vraag: "Wie is de minister-president?" → GEEN_MATCH
        """


def classificeer_vraag(vraag: str, groep: str = "voorbeeld") -> list[str]:
    """
    Classificeert een werknemersvraag naar 1-3 beleid-labels.

    Parameters
    ----------
    vraag : str
        De vraag van de werknemer.
    groep : str
        De werkgever-groep (voor het ophalen van de juiste labels).

    Returns
    -------
    list[str]
        Lijst van 1-3 label_id's, of ["GEEN_MATCH"] als de vraag
        niet past bij het mobiliteitsbeleid.
    """
    system_prompt = build_classifier_prompt(groep)

    response = client.messages.create(
        model=MODEL,
        max_tokens=100,
        temperature=0,
        system=system_prompt,
        messages=[
            {"role": "user", "content": vraag}
        ],
    )

    # Parse het antwoord
    antwoord = response.content[0].text.strip()
    labels = [label.strip() for label in antwoord.split(",")]

    return labels


def haal_chunks_op(labels: list[str], groep: str = "voorbeeld") -> str:
    """
    Haalt de chunk-teksten op voor de gegeven labels.

    Parameters
    ----------
    labels : list[str]
        Lijst van label_id's uit de classifier.
    groep : str
        De werkgever-groep.

    Returns
    -------
    str
        Gecombineerde chunk-teksten, gescheiden door een lijn.
    """
    if not labels or labels == ["GEEN_MATCH"]:
        return ""

    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()

    chunks = []
    for label_id in labels:
        cursor.execute(
            "SELECT chunk FROM beleid_chunks WHERE label_id = ? AND groep = ?",
            (label_id, groep),
        )
        row = cursor.fetchone()
        if not row:
            # Fallback naar voorbeeld-beleid als geen groep-specifieke chunk bestaat
            cursor.execute(
                "SELECT chunk FROM beleid_chunks WHERE label_id = ? AND groep = 'voorbeeld'",
                (label_id,),
            )
            row = cursor.fetchone()
        if row:
            chunks.append(row[0])

    conn.close()

    return "\n\n---\n\n".join(chunks)


# --- Test ---
if __name__ == "__main__":
    test_vragen = [
        "Hoeveel mobiliteitsbudget krijg ik?",
        "Ik ben mijn kaart kwijt, wat moet ik doen?",
        "Mag ik eerste klas reizen?",
        "Wat is een correctietarief?",
        "Ik ga uit dienst, wat moet ik doen met mijn OV-kaart?",
        "Hoe reserveer ik een deelauto?",
        "Wat zijn de sancties als ik privé rij met de bedrijfswagen?",
        "Hoe werkt de fiscale ruimte van mijn budget?",
        "Wat is de hoofdstad van Nederland?",
        "Ik ben ziek en kan 3 maanden niet reizen, wat moet ik doen?",
    ]

    print("=" * 70)
    print("CLASSIFIER TEST — Mobiliteitsbeleid")
    print("=" * 70)

    for vraag in test_vragen:
        labels = classificeer_vraag(vraag)
        print(f"\nVraag: \"{vraag}\"")
        print(f"Labels: {labels}")

        # Toon chunk-lengtes
        if labels != ["GEEN_MATCH"]:
            chunks = haal_chunks_op(labels)
            print(f"Chunk totaal: {len(chunks)} tekens")
