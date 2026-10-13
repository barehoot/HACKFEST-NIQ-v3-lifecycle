"""
resume_parser.py

Sub-step 16 (Phase 5): turns an uploaded resume file into the plain-text
string that match_resume() / run_agent1() expect.

Every function upstream of this (match_resume_to_role, match_resume,
run_agent1) takes resume_text as a plain string -- nothing in the repo
yet accepted an actual uploaded file. This module is the missing bridge:
given a file path (PDF, DOCX, or TXT), it extracts and returns that text.

Deliberately out of scope: OCR for scanned/image-only PDFs. If a PDF's
pages contain no extractable text layer, we raise a clear error rather
than silently returning empty text that would flow into embeddings as
noise -- the same "no silent gaps" pattern used throughout agents/.
"""

from pathlib import Path

from pypdf import PdfReader
from docx import Document

SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".txt"}


def _extract_pdf_text(file_path: Path) -> str:
    reader = PdfReader(file_path)
    pages = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(pages)


def _extract_docx_text(file_path: Path) -> str:
    # Table cells are not walked -- a resume laid out purely in tables
    # would lose that content. Nice-to-have for later, not needed for
    # the hackathon's sample resumes; flagged here rather than dropped
    # silently.
    document = Document(file_path)
    paragraphs = [p.text for p in document.paragraphs if p.text.strip()]
    return "\n".join(paragraphs)


def _extract_txt_text(file_path: Path) -> str:
    return file_path.read_text(encoding="utf-8", errors="ignore")


def extract_resume_text(file_path: str) -> str:
    """
    Given the path to an uploaded resume file, returns its extracted text.

    Raises:
        FileNotFoundError -- file_path does not exist.
        ValueError -- unsupported extension, or extraction produced no
            usable text (e.g. a scanned/image-only PDF with no text
            layer -- OCR is not supported here).
    """
    path = Path(file_path)

    if not path.exists():
        raise FileNotFoundError(f"Resume file not found: {file_path}")

    extension = path.suffix.lower()

    if extension not in SUPPORTED_EXTENSIONS:
        raise ValueError(
            f"Unsupported resume file type '{extension}'. "
            f"Supported types: {', '.join(sorted(SUPPORTED_EXTENSIONS))}."
        )

    if extension == ".pdf":
        text = _extract_pdf_text(path)
    elif extension == ".docx":
        text = _extract_docx_text(path)
    else:
        text = _extract_txt_text(path)

    text = text.strip()

    if not text:
        raise ValueError(
            f"No extractable text found in '{file_path}'. If this is a "
            "scanned/image-only PDF, OCR is not supported -- provide a "
            "text-based file instead."
        )

    return text


def print_extraction_preview(file_path, preview_chars=300):
    """
    Test helper: extracts text from file_path and prints a readable
    preview, matching the print-and-eyeball style used in
    agent1_matcher.py / agent1_blueprint.py.
    """
    print(f"--- {file_path} ---")
    try:
        text = extract_resume_text(file_path)
        print(f"Extracted length: {len(text)} characters")
        print(f"Preview: {text[:preview_chars]!r}")
    except (FileNotFoundError, ValueError) as exc:
        print(f"Extraction failed: {exc}")
    print()


if __name__ == "__main__":
    AGENTS_DIR = Path(__file__).resolve().parent
    SAMPLE_DIR = AGENTS_DIR.parent / "data" / "sample_resumes"

    # No real uploaded resume samples exist in the repo yet -- these
    # calls double as a smoke test AND as documentation of the expected
    # usage once a real PDF/DOCX is dropped into data/sample_resumes/.
    print_extraction_preview(str(SAMPLE_DIR / "sample_resume.txt"))
    print_extraction_preview(str(SAMPLE_DIR / "sample_resume.pdf"))
    print_extraction_preview(str(SAMPLE_DIR / "sample_resume.docx"))
    print_extraction_preview(str(SAMPLE_DIR / "does_not_exist.pdf"))
    print_extraction_preview("resume.jpg")
