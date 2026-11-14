"""Official NASA documents used by the research assistant, and where they are stored."""

from __future__ import annotations

from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
PAPERS_DIR = PROJECT_ROOT / "data" / "papers"
DEFAULT_CORPUS_PATH = PROJECT_ROOT / "data" / "processed" / "nasa_corpus.json"

SOURCES: tuple[dict[str, str], ...] = (
    {
        "doc_id": "bass2-summary",
        "ntrs_id": "20210011385",
        "title": "BASS-II Summary Report",
        "source_url": "https://ntrs.nasa.gov/citations/20210011385",
        "pdf_url": "https://ntrs.nasa.gov/api/citations/20210011385/downloads/TM-20210011385.pdf",
        "filename": "TM-20210011385.pdf",
    },
    {
        "doc_id": "bass2-results",
        "ntrs_id": "20160000593",
        "title": "Combustion of Solids in Microgravity: Results from the BASS-II Experiment",
        "source_url": "https://ntrs.nasa.gov/citations/20160000593",
        "pdf_url": "https://ntrs.nasa.gov/api/citations/20160000593/downloads/20160000593.pdf",
        "filename": "20160000593.pdf",
    },
    {
        "doc_id": "saffire-results",
        "ntrs_id": "20170008805",
        "title": "Results of Large-Scale Spacecraft Flammability Tests",
        "source_url": "https://ntrs.nasa.gov/citations/20170008805",
        "pdf_url": "https://ntrs.nasa.gov/api/citations/20170008805/downloads/20170008805.pdf",
        "filename": "20170008805.pdf",
    },
)
