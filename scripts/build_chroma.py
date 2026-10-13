"""
build_chroma.py

Sub-step 7 (this version): empty Chroma collection setup only.
Creates/opens a persistent Chroma store at db/chroma_store/ with a
collection named "role_profiles". No data is added yet -- that comes
in later sub-steps (build embedding_text, then embed and add roles).

Safe to re-run: get_or_create_collection() means re-running this script
does not wipe out anything already added later, and does not error if
the collection already exists.
"""

from pathlib import Path
import sqlite3

import chromadb

SCRIPT_DIR = Path(__file__).resolve().parent
PROJECT_ROOT = SCRIPT_DIR.parent
CHROMA_PATH = PROJECT_ROOT / "db" / "chroma_store"
SQLITE_PATH = PROJECT_ROOT / "db" / "talent360i.sqlite"

COLLECTION_NAME = "role_profiles"


def get_collection():
    """
    Opens (creating if needed) the persistent Chroma client and the
    role_profiles collection. Centralized here so later sub-steps
    (and eventually Agent 1) all go through the same connection logic.
    """
    CHROMA_PATH.parent.mkdir(parents=True, exist_ok=True)

    client = chromadb.PersistentClient(path=str(CHROMA_PATH))
    collection = client.get_or_create_collection(name=COLLECTION_NAME)

    return client, collection


def build_empty_collection():
    client, collection = get_collection()

    count = collection.count()

    print(f"Chroma persistent store path: {CHROMA_PATH}")
    print(f"Collection name: {collection.name}")
    print(f"Current item count: {count}")


def fetch_role_descriptions():
    """
    Reads all 12 rows from role_descriptions in SQLite (9 core + 3 REF_
    reference-only roles -- both are wanted here, so resumes can match
    against reference roles too).
    """
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT role_id, role_summary, responsibility_extract, output_extract, skills_extract
        FROM role_descriptions;
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def build_embedding_text(role_summary, responsibility_extract, output_extract, skills_extract):
    """
    Labeled concatenation of the four Role_Descriptions fields into one
    block of text per role, ready to be embedded later. Labels are kept
    so the embedding model gets a hint about what kind of content each
    part represents (positioning vs. tasks vs. outcomes vs. skills) --
    matching the kind of structure a resume (summary/experience/skills)
    also tends to have.
    """
    parts = [
        f"Role Summary: {role_summary or ''}",
        f"Responsibilities: {responsibility_extract or ''}",
        f"Outputs: {output_extract or ''}",
        f"Skills: {skills_extract or ''}",
    ]
    return "\n".join(parts)


def preview_embedding_texts(limit=3):
    """
    Sub-step 8: build embedding_text for each role and print a handful,
    so we can eyeball whether the text reads sensibly. No Chroma writes
    happen here yet -- that's the next sub-step.
    """
    rows = fetch_role_descriptions()

    print(f"Total roles available: {len(rows)}\n")

    for role_id, role_summary, responsibility_extract, output_extract, skills_extract in rows[:limit]:
        text = build_embedding_text(role_summary, responsibility_extract, output_extract, skills_extract)
        print(f"--- {role_id} ---")
        print(text)
        print(f"\n[length: {len(text)} characters]\n")


def fetch_roles_with_metadata():
    """
    Joins role_descriptions with role_master to get metadata for each role.
    Uses LEFT JOIN deliberately: the 3 REF_ reference-only roles exist in
    role_descriptions but have NO row in role_master, so an INNER JOIN
    would silently drop them from Chroma -- exactly the kind of silent
    gap we've been avoiding throughout this project. Missing tower/
    role_grade/role_name for REF_ roles are given explicit placeholder
    values instead, since Chroma metadata cannot store None.
    """
    conn = sqlite3.connect(SQLITE_PATH)
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
            rd.role_id,
            rd.role_summary,
            rd.responsibility_extract,
            rd.output_extract,
            rd.skills_extract,
            rd.selection_status,
            rm.role_name,
            rm.tower,
            rm.role_grade
        FROM role_descriptions rd
        LEFT JOIN role_master rm ON rd.role_id = rm.role_id;
        """
    )
    rows = cur.fetchall()
    conn.close()
    return rows


def embed_and_add_roles():
    """
    Sub-step 9: build embedding_text for every role, embed it (using
    Chroma's default local embedding function), and add it to the
    role_profiles collection with metadata for later filtering.

    Safe to re-run: deletes all existing items in the collection first,
    then re-adds everything fresh, so running this twice does not
    duplicate or leave stale entries behind.
    """
    client, collection = get_collection()

    # Clear existing items so this stays idempotent. Chroma has no
    # "delete all" shortcut, so we fetch all current ids and delete by id.
    existing = collection.get()
    if existing["ids"]:
        collection.delete(ids=existing["ids"])

    rows = fetch_roles_with_metadata()

    ids = []
    documents = []
    metadatas = []

    for (
        role_id, role_summary, responsibility_extract, output_extract,
        skills_extract, selection_status, role_name, tower, role_grade,
    ) in rows:
        text = build_embedding_text(role_summary, responsibility_extract, output_extract, skills_extract)

        ids.append(role_id)
        documents.append(text)
        metadatas.append(
            {
                "role_id": role_id,
                "role_name": role_name or "(reference-only role, no role_master entry)",
                "tower": tower or "UNSPECIFIED",
                "role_grade": role_grade or "UNSPECIFIED",
                "selection_status": selection_status or "",
            }
        )

    collection.add(ids=ids, documents=documents, metadatas=metadatas)

    print(f"Roles embedded and added to Chroma: {len(ids)}")
    print(f"Collection count after add: {collection.count()}")


def match_resume_to_role(resume_text, n_results=2):
    """
    Sub-step 10: the real, reusable matching function Agent 1 will call.

    Given raw resume text, returns the top n_results closest roles from
    the role_profiles collection, each as a dict with role_id, metadata,
    and distance (lower distance = closer match). Returns top-2 by default
    since Agent 1's low-confidence path needs to see the second-best
    candidate too, not just the top pick.
    """
    _, collection = get_collection()

    results = collection.query(query_texts=[resume_text], n_results=n_results)

    matches = []
    for role_id, distance, metadata in zip(
        results["ids"][0], results["distances"][0], results["metadatas"][0]
    ):
        matches.append(
            {
                "role_id": role_id,
                "distance": distance,
                "metadata": metadata,
            }
        )

    return matches


def test_matching_across_towers():
    """
    Sanity check: run three sample snippets, one that should point clearly
    at each tower (AP, O2C, R2R), to confirm matching works broadly and
    not just for the one case we happened to test manually.
    """
    samples = {
        "AP-leaning": (
            "I process vendor invoices, handle 3-way matching, and resolve "
            "payment exceptions in SAP."
        ),
        "O2C-leaning": (
            "I manage customer billing, issue credit notes, and review "
            "revenue recognition for accuracy against contracts."
        ),
        "R2R-leaning": (
            "I prepare bank reconciliations, post journal entries, and "
            "ensure balance sheet accounts are supported with SOX evidence."
        ),
    }

    for label, text in samples.items():
        print(f"--- {label} ---")
        print(f"Query: {text}")
        matches = match_resume_to_role(text, n_results=2)
        for m in matches:
            print(f"  {m['role_id']}  (tower: {m['metadata']['tower']}, distance: {m['distance']:.4f})")
        print()


if __name__ == "__main__":
    build_empty_collection()
    print()
    preview_embedding_texts()
    print()
    embed_and_add_roles()
    print()
    test_matching_across_towers()