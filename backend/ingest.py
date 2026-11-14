"""Download the NASA source PDFs and build the page-level research corpus.

Run from the project root:

    .venv/bin/python -m backend.ingest

PDFs already in data/papers/ are reused (they can also be placed there by hand).
Text is extracted page by page and chunked within each page, so every chunk cites
exactly one 1-based PDF page. Pages with little or garbled text are reported, never
filled in. The corpus is written to data/processed/nasa_corpus.json.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

import httpx

from backend.papers import DEFAULT_CORPUS_PATH, PAPERS_DIR, SOURCES

CORPUS_FORMAT = 1
DOWNLOAD_TIMEOUT = httpx.Timeout(60.0, connect=15.0)
MAX_PDF_BYTES = 80 * 1024 * 1024
TARGET_WORDS = 110
MAX_WORDS = 170
MIN_CHUNK_WORDS = 25
EMPTY_PAGE_CHARS = 40
SPARSE_PAGE_WORDS = 40
POOR_ALPHA_RATIO = 0.6

_SENTENCE_END = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\[])")


class IngestError(Exception):
    pass


def is_pdf_file(path: Path) -> bool:
    try:
        with path.open("rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def download_pdf(source: dict[str, str], dest: Path, client: httpx.Client) -> None:
    fd, tmp_name = tempfile.mkstemp(dir=dest.parent, prefix=f".{dest.name}.", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as fh:
            with client.stream("GET", source["pdf_url"]) as resp:
                if resp.status_code != 200:
                    raise IngestError(f"HTTP {resp.status_code} from {source['pdf_url']}")
                size = 0
                for block in resp.iter_bytes():
                    size += len(block)
                    if size > MAX_PDF_BYTES:
                        raise IngestError(f"download exceeded {MAX_PDF_BYTES // (1024 * 1024)} MB")
                    fh.write(block)
        if not is_pdf_file(Path(tmp_name)):
            raise IngestError(f"response from {source['pdf_url']} is not a PDF (missing %PDF- header)")
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, dest)
    except httpx.HTTPError as exc:
        raise IngestError(f"{exc.__class__.__name__} while downloading {source['pdf_url']}: {exc}") from exc
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)


def ensure_pdf(source: dict[str, str], papers_dir: Path, client: httpx.Client | None) -> tuple[Path, str]:
    path = papers_dir / source["filename"]
    if path.exists():
        if not is_pdf_file(path):
            raise IngestError(
                f"{path} exists but is not a PDF; delete or replace it with the PDF from {source['pdf_url']}"
            )
        return path, "reused existing file"
    if client is None:
        raise IngestError(f"{path.name} not found and downloads are disabled (--offline)")
    download_pdf(source, path, client)
    return path, "downloaded"


_PRIVATE_USE = re.compile("[\ue000-\uf8ff]")


def clean_page_text(text: str) -> str:
    text = text.replace("\u00ad", "")
    # Symbol-font bullets extract as private-use code points with no standard meaning.
    text = _PRIVATE_USE.sub("•", text)
    text = re.sub(r"([a-z])-\n([a-z])", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


def assess_page(text: str) -> str:
    if len(text) < EMPTY_PAGE_CHARS:
        return "empty"
    visible = [c for c in text if not c.isspace()]
    alpha = sum(c.isalpha() for c in visible) / len(visible)
    if alpha < POOR_ALPHA_RATIO:
        return "poor"
    if len(text.split()) < SPARSE_PAGE_WORDS:
        return "sparse"
    return "ok"


def chunk_page(text: str) -> list[str]:
    """Pack whole sentences into chunks of about TARGET_WORDS, never crossing the page."""
    pieces: list[str] = []
    for sentence in _SENTENCE_END.split(text):
        words = sentence.split()
        while len(words) > MAX_WORDS:
            pieces.append(" ".join(words[:MAX_WORDS]))
            words = words[MAX_WORDS:]
        if words:
            pieces.append(" ".join(words))

    chunks: list[list[str]] = []
    current: list[str] = []
    count = 0
    for piece in pieces:
        n = len(piece.split())
        if current and count + n > MAX_WORDS:
            chunks.append(current)
            current, count = [], 0
        current.append(piece)
        count += n
        if count >= TARGET_WORDS:
            chunks.append(current)
            current, count = [], 0
    if current:
        if chunks and count < MIN_CHUNK_WORDS and len(" ".join(chunks[-1]).split()) + count <= MAX_WORDS:
            chunks[-1].extend(current)
        else:
            chunks.append(current)
    return [" ".join(c) for c in chunks]


def extract_document(source: dict[str, str], path: Path) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    from pypdf import PdfReader

    try:
        reader = PdfReader(path)
        pages = list(reader.pages)
    except Exception as exc:  # noqa: BLE001
        raise IngestError(f"{path.name} could not be parsed as a PDF: {exc}") from exc

    page_status: dict[str, list[int]] = {"empty": [], "poor": [], "sparse": [], "error": []}
    chunks: list[dict[str, Any]] = []
    for page_number, page in enumerate(pages, start=1):
        try:
            text = clean_page_text(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            page_status["error"].append(page_number)
            continue
        status = assess_page(text)
        if status != "ok":
            page_status[status].append(page_number)
        if status == "empty":
            continue
        for index, chunk_text in enumerate(chunk_page(text), start=1):
            chunks.append({
                "chunk_id": f"{source['doc_id']}-p{page_number}-c{index}",
                "doc_id": source["doc_id"],
                "title": source["title"],
                "source_url": source["source_url"],
                "pdf_url": source["pdf_url"],
                "page": page_number,
                "page_quality": status,
                "chunk_index": index,
                "word_count": len(chunk_text.split()),
                "text": chunk_text,
            })

    document = {
        **{k: source[k] for k in ("doc_id", "ntrs_id", "title", "source_url", "pdf_url", "filename")},
        "status": "ingested",
        "error": None,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        "page_count": len(pages),
        "chunk_count": len(chunks),
        "pages_with_text": len(pages) - len(page_status["empty"]) - len(page_status["error"]),
        "empty_pages": page_status["empty"],
        "poorly_extracted_pages": page_status["poor"],
        "sparse_pages": page_status["sparse"],
        "failed_pages": page_status["error"],
    }
    return document, chunks


def missing_document(source: dict[str, str], error: str) -> dict[str, Any]:
    return {
        **{k: source[k] for k in ("doc_id", "ntrs_id", "title", "source_url", "pdf_url", "filename")},
        "status": "missing",
        "error": error,
        "sha256": None,
        "page_count": 0,
        "chunk_count": 0,
        "pages_with_text": 0,
        "empty_pages": [],
        "poorly_extracted_pages": [],
        "sparse_pages": [],
        "failed_pages": [],
    }


def build_corpus(papers_dir: Path, offline: bool = False) -> tuple[dict[str, Any], list[str]]:
    from pypdf import __version__ as pypdf_version

    papers_dir.mkdir(parents=True, exist_ok=True)
    documents: list[dict[str, Any]] = []
    chunks: list[dict[str, Any]] = []
    failures: list[str] = []
    client = None if offline else httpx.Client(timeout=DOWNLOAD_TIMEOUT, follow_redirects=True)
    try:
        for source in SOURCES:
            label = f"{source['doc_id']} (NTRS {source['ntrs_id']})"
            try:
                path, how = ensure_pdf(source, papers_dir, client)
                document, doc_chunks = extract_document(source, path)
            except IngestError as exc:
                failures.append(f"{label}: {exc}")
                print(f"FAILED  {label}: {exc}", file=sys.stderr)
                documents.append(missing_document(source, str(exc)))
                continue
            documents.append(document)
            chunks.extend(doc_chunks)
            print(f"OK      {label}: {how}; {document['page_count']} pages, {document['chunk_count']} chunks")
            for key, label_text in (
                ("empty_pages", "no extractable text"),
                ("poorly_extracted_pages", "poorly extracted (mostly non-letter characters)"),
                ("sparse_pages", f"sparse (< {SPARSE_PAGE_WORDS} words)"),
                ("failed_pages", "extraction error"),
            ):
                if document[key]:
                    print(f"        pages {label_text}: {document[key]}")
    finally:
        if client is not None:
            client.close()

    corpus = {
        "format_version": CORPUS_FORMAT,
        "extractor": f"pypdf {pypdf_version}",
        "chunking": {
            "unit": "sentences packed within a single PDF page",
            "target_words": TARGET_WORDS,
            "max_words": MAX_WORDS,
        },
        "page_numbering": "1-based PDF page index (not printed page labels)",
        "documents": documents,
        "chunks": chunks,
    }
    return corpus, failures


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False, indent=1)
            fh.write("\n")
        os.chmod(tmp_name, 0o644)
        os.replace(tmp_name, path)
    except BaseException:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
        raise


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--papers-dir", type=Path, default=PAPERS_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_CORPUS_PATH)
    parser.add_argument("--offline", action="store_true", help="only use PDFs already in the papers directory")
    args = parser.parse_args(argv)

    corpus, failures = build_corpus(args.papers_dir, offline=args.offline)
    write_json_atomic(args.output, corpus)
    print(f"Wrote {len(corpus['chunks'])} chunks from "
          f"{sum(d['status'] == 'ingested' for d in corpus['documents'])}/{len(SOURCES)} documents to {args.output}")
    if failures:
        print("\nSome sources could not be ingested. Download each PDF manually from the URL above, "
              f"save it in {args.papers_dir} with the listed filename, and rerun:", file=sys.stderr)
        for source in SOURCES:
            print(f"  {source['filename']}  <-  {source['pdf_url']}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
