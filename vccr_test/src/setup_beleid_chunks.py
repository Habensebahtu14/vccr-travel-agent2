"""
setup_beleid_chunks.py
======================
Laadt de beleid_chunks.json in een SQLite-tabel 'beleid_chunks'.
Kan worden toegevoegd aan de bestaande agent.db database.

Gebruik:
    python setup_beleid_chunks.py

Tabelstructuur:
    beleid_chunks(
        label_id        TEXT,       -- unieke label identifier
        groep           TEXT,       -- werkgever groep (A, B, C of 'voorbeeld')
        beschrijving    TEXT,       -- korte beschrijving voor de classifier
        keywords        TEXT,       -- komma-gescheiden keywords
        chunk           TEXT,       -- de eigenlijke beleidstekst
        voorbeeldvragen TEXT        -- komma-gescheiden voorbeeldvragen
    )
"""

import json
import sqlite3
from pathlib import Path


def load_chunks_to_db(
    json_path: str = "beleid_chunks.json",
    db_path: str = "data/agent.db",
    groep: str = "voorbeeld"
):
    """
    Laadt chunks uit een JSON-bestand in de SQLite database.

    Parameters
    ----------
    json_path : str
        Pad naar het JSON-bestand met de chunks.
    db_path : str
        Pad naar de SQLite database (bestaande agent.db).
    groep : str
        De werkgever-groep waarvoor dit beleid geldt.
        Gebruik 'voorbeeld' voor het voorbeeldbeleid,
        'A', 'B', of 'C' voor de specifieke werkgevers.
    """
    # Lees JSON
    with open(json_path, "r", encoding="utf-8") as f:
        data = json.load(f)

    # Verbind met database
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Maak tabel aan (als die nog niet bestaat)
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS beleid_chunks (
            label_id        TEXT,
            groep           TEXT,
            beschrijving    TEXT,
            keywords        TEXT,
            chunk           TEXT,
            voorbeeldvragen TEXT,
            PRIMARY KEY (label_id, groep)
        )
    """)

    # Verwijder bestaande chunks voor deze groep (voor herlaadbaar te zijn)
    cursor.execute("DELETE FROM beleid_chunks WHERE groep = ?", (groep,))

    # Voeg chunks toe
    for label in data["labels"]:
        cursor.execute(
            """
            INSERT INTO beleid_chunks
                (label_id, groep, beschrijving, keywords, chunk, voorbeeldvragen)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                label["label_id"],
                groep,
                label["beschrijving"],
                ", ".join(label["keywords"]),
                label["chunk"],
                " | ".join(label["voorbeeldvragen"]),
            ),
        )

    conn.commit()

    # Verificatie
    cursor.execute(
        "SELECT COUNT(*) FROM beleid_chunks WHERE groep = ?", (groep,)
    )
    count = cursor.fetchone()[0]
    print(f"✅ {count} chunks geladen voor groep '{groep}' in {db_path}")

    # Toon overzicht
    cursor.execute(
        "SELECT label_id, LENGTH(chunk) as chars FROM beleid_chunks WHERE groep = ? ORDER BY label_id",
        (groep,),
    )
    print(f"\n{'Label':<40} {'Tekens':>8}")
    print("-" * 50)
    for row in cursor.fetchall():
        print(f"{row[0]:<40} {row[1]:>8}")

    conn.close()


if __name__ == "__main__":
    # Zorg dat data directory bestaat
    Path("data").mkdir(exist_ok=True)
    load_chunks_to_db()
