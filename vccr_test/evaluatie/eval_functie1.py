"""
Evaluatie Functie 1: Text-to-SQL — Execution Accuracy

Methode: vergelijk het resultaat van de door de LLM gegenereerde SQL
met het resultaat van de handmatig geschreven gouden SQL.
Als beide hetzelfde antwoord teruggeven → CORRECT.

Gebruik:
    cd vccr_test
    python evaluatie/eval_functie1.py
"""

import json
import sqlite3
import sys
from pathlib import Path

from dotenv import load_dotenv

# ── Paden ──────────────────────────────────────────────────────────────────────
SCRIPT_DIR  = Path(__file__).parent
PROJECT_DIR = SCRIPT_DIR.parent
DB_PATH     = str(PROJECT_DIR / "data" / "agent.db")
DATASET_PATH = SCRIPT_DIR / "golden_dataset_f1.json"

load_dotenv(PROJECT_DIR / ".env")

# system_prompt.py heeft geen side effects — veilig te importeren
sys.path.insert(0, str(PROJECT_DIR / "src"))
from system_prompt import SYSTEM_PROMPT  # noqa: E402

from anthropic import Anthropic  # noqa: E402

MODEL  = "claude-sonnet-4-20250514"
client = Anthropic()


# ── Genereer SQL via de LLM (spiegelt genereer_sql() in app.py) ────────────────
def genereer_sql(vraag: str, pers_nummer: str, groep: str) -> str | None:
    """Vraagt de LLM om een SQL-query en geeft alleen de SQL-string terug."""
    system = SYSTEM_PROMPT.format(pers_nummer=pers_nummer, groep=groep)
    response = client.messages.create(
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


# ── Hulpfuncties ───────────────────────────────────────────────────────────────
def voer_sql_uit(sql: str) -> list[tuple] | str:
    """Voert een SQL-query uit en geeft een lijst van tuples terug, of een foutstring."""
    try:
        conn = sqlite3.connect(DB_PATH)
        cur  = conn.cursor()
        cur.execute(sql)
        rows = cur.fetchall()
        conn.close()
        return rows
    except Exception as e:
        return f"FOUT: {e}"


def normaliseer(rows: list[tuple]) -> list[tuple]:
    """
    Normaliseert rijen voor vergelijking:
    - floats afgerond op 2 decimalen
    - volgorde van rijen maakt niet uit (gesorteerd)
    """
    genormaliseerd = []
    for rij in rows:
        nieuwe_rij = [round(v, 2) if isinstance(v, float) else v for v in rij]
        genormaliseerd.append(tuple(nieuwe_rij))
    return sorted(genormaliseerd)


def vergelijk(goud: list[tuple], agent: list[tuple]) -> bool:
    return normaliseer(goud) == normaliseer(agent)


# ── Evaluatieloop ──────────────────────────────────────────────────────────────
def evalueer():
    with open(DATASET_PATH, encoding="utf-8") as f:
        dataset = json.load(f)

    vragen = dataset["vragen"]
    totaal  = len(vragen)
    correct = 0
    per_categorie: dict[str, list[bool]] = {}

    print()
    print("=" * 65)
    print("  EVALUATIE FUNCTIE 1: TEXT-TO-SQL — EXECUTION ACCURACY")
    print("=" * 65)

    for entry in vragen:
        idx        = entry["id"]
        vraag      = entry["vraag"]
        pers_nr    = entry["pers_nummer"]
        groep      = entry["groep"]
        gouden_sql = entry["gouden_sql"]
        categorie  = entry["categorie"]

        print(f"\n[{idx}/{totaal}] {vraag}")
        print(f"        Categorie  : {categorie}")

        # Stap 1: genereer SQL via de LLM
        try:
            gegenereerde_sql = genereer_sql(vraag, pers_nr, groep)
        except Exception as e:
            print(f"        Status     : FOUT (LLM-aanroep mislukt: {e})")
            per_categorie.setdefault(categorie, []).append(False)
            continue

        if gegenereerde_sql is None:
            print("        Status     : FOUT (agent gaf geen SQL terug)")
            per_categorie.setdefault(categorie, []).append(False)
            continue

        weergave_sql = gegenereerde_sql[:110] + ("…" if len(gegenereerde_sql) > 110 else "")
        print(f"        SQL agent  : {weergave_sql}")

        # Stap 2: voer beide queries uit
        goud_resultaat  = voer_sql_uit(gouden_sql)
        agent_resultaat = voer_sql_uit(gegenereerde_sql)

        # Stap 3: foutafhandeling
        if isinstance(agent_resultaat, str):
            print(f"        Status     : FOUT (SQL-fout: {agent_resultaat})")
            per_categorie.setdefault(categorie, []).append(False)
            continue

        if isinstance(goud_resultaat, str):
            print(f"        Status     : FOUT (Gouden SQL-fout: {goud_resultaat})")
            per_categorie.setdefault(categorie, []).append(False)
            continue

        # Stap 4: vergelijk genormaliseerde resultaten
        is_correct = vergelijk(goud_resultaat, agent_resultaat)

        if is_correct:
            correct += 1
            print(f"        Status     : CORRECT  ✓")
            print(f"        Resultaat  : {goud_resultaat}")
        else:
            print(f"        Status     : FOUT  ✗")
            print(f"        Goud       : {normaliseer(goud_resultaat)}")
            print(f"        Agent      : {normaliseer(agent_resultaat)}")

        per_categorie.setdefault(categorie, []).append(is_correct)

    # ── Samenvatting ─────────────────────────────────────────────────────────
    accuracy = (correct / totaal) * 100

    print()
    print("=" * 65)
    print("  SAMENVATTING")
    print("=" * 65)
    print(f"  Totaal vragen  : {totaal}")
    print(f"  Correct        : {correct}")
    print(f"  Fout           : {totaal - correct}")
    print(f"  Accuracy       : {accuracy:.1f}%")
    print()
    print("  Per categorie:")
    for cat, resultaten in per_categorie.items():
        n_correct = sum(resultaten)
        n_totaal  = len(resultaten)
        pct       = (n_correct / n_totaal) * 100
        print(f"    {cat:<16} {n_correct}/{n_totaal}  ({pct:.0f}%)")
    print("=" * 65)
    print()


if __name__ == "__main__":
    evalueer()
