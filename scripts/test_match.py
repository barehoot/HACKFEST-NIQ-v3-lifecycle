"""
test_match.py

Quick sanity check for sub-step 9/10: given a sample resume-like snippet,
does Chroma return a sensible closest role?

This is a throwaway/scratch test script, not part of the main pipeline.
"""

import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

from build_chroma import get_collection

_, collection = get_collection()

sample_text = "I process vendor invoices, handle 3-way matching, and resolve payment exceptions in SAP"

results = collection.query(query_texts=[sample_text], n_results=3)

print(f"Query: {sample_text}\n")
print("Top matches (closest first):")
for role_id, distance in zip(results["ids"][0], results["distances"][0]):
    print(f"  {role_id}  (distance: {distance:.4f})")