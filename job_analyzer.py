"""
Job Hunting Bot - Document Analysis & Form Reverse-Engineering Module
Ecosystem: Hugging Face Transformers, LayoutLMv3, BART-large-MNLI, pdfplumber
Target Markets: Luxury Hotels & Cruise Lines (Qatar, Oman, Maldives, Global Cruise)
"""

import os
import re
import importlib.metadata
from typing import Dict, Any, List, Optional
from PIL import Image
import pdfplumber
import torch
from transformers import (
    AutoProcessor,
    AutoModel,
    AutoTokenizer,
    AutoModelForSequenceClassification,
    pipeline,
)

# ---------------------------------------------------------------------------
# Global Model Registry (Cached singletons to prevent redundant re-loads)
# ---------------------------------------------------------------------------
_MODELS = {
    "layoutlm_processor": None,
    "layoutlm_model": None,
    "bart_tokenizer": None,
    "bart_model": None,
    "bart_classifier": None,
}


def get_layoutlm():
    """Lazily load and return LayoutLMv3 processor and model."""
    if _MODELS["layoutlm_processor"] is None or _MODELS["layoutlm_model"] is None:
        print("[DevOps Pipeline] Loading microsoft/layoutlmv3-base...")
        _MODELS["layoutlm_processor"] = AutoProcessor.from_pretrained(
            "microsoft/layoutlmv3-base", apply_ocr=False
        )
        _MODELS["layoutlm_model"] = AutoModel.from_pretrained(
            "microsoft/layoutlmv3-base"
        )
        _MODELS["layoutlm_model"].eval()
    return _MODELS["layoutlm_processor"], _MODELS["layoutlm_model"]


def get_bart_classifier():
    """Lazily load and return BART MNLI zero-shot classifier pipeline."""
    if _MODELS["bart_classifier"] is None:
        print("[DevOps Pipeline] Loading facebook/bart-large-mnli...")
        tokenizer = AutoTokenizer.from_pretrained("facebook/bart-large-mnli")
        model = AutoModelForSequenceClassification.from_pretrained("facebook/bart-large-mnli")
        model.eval()
        _MODELS["bart_tokenizer"] = tokenizer
        _MODELS["bart_model"] = model
        _MODELS["bart_classifier"] = pipeline(
            "zero-shot-classification",
            model=model,
            tokenizer=tokenizer,
            device=-1  # CPU mode
        )
    return _MODELS["bart_classifier"]


def analyze_job_structure(pdf_path: str) -> Dict[str, Any]:
    """
    Analyzes the structure of a PDF (job description, resume, or requisition form)
    using LayoutLMv3 for spatial document understanding and BART for semantic parsing.

    Args:
        pdf_path: File system path to the target PDF document.

    Returns:
        Dict containing:
            - status: Success/failure
            - entities: { 'name': str, 'email': str, 'phone': str, 'role_title': str }
            - layout_embeddings_shape: Tensor shape from LayoutLMv3
            - page_count: Number of pages parsed
            - words_extracted: Total word tokens
            - text_preview: First 300 characters of parsed document
            - classified_sections: High-confidence semantic sections detected
    """
    if not os.path.exists(pdf_path):
        raise FileNotFoundError(f"PDF not found at path: {pdf_path}")

    processor, layoutlm_model = get_layoutlm()
    
    extracted_words = []
    extracted_boxes = []
    full_text_lines = []
    all_words_text = []

    with pdfplumber.open(pdf_path) as pdf:
        num_pages = len(pdf.pages)
        if num_pages == 0:
            raise ValueError(f"PDF at {pdf_path} is empty.")

        # Focus deep multimodal layout processing on the primary page (Page 1)
        first_page = pdf.pages[0]
        page_width = float(first_page.width)
        page_height = float(first_page.height)

        # Extract words with 2D spatial coordinates
        page_words = first_page.extract_words(
            x_tolerance=3,
            y_tolerance=3,
            keep_blank_chars=False,
            use_text_flow=True
        )

        for w in page_words:
            word_text = w["text"].strip()
            if not word_text:
                continue
            all_words_text.append(word_text)
            
            # Normalize bounding box coordinates to LayoutLMv3 standard (0 to 1000)
            x0 = max(0, min(1000, int(1000 * float(w["x0"]) / page_width)))
            y0 = max(0, min(1000, int(1000 * float(w["top"]) / page_height)))
            x1 = max(0, min(1000, int(1000 * float(w["x1"]) / page_width)))
            y1 = max(0, min(1000, int(1000 * float(w["bottom"]) / page_height)))
            
            # Ensure valid box geometry
            if x1 <= x0:
                x1 = min(1000, x0 + 1)
            if y1 <= y0:
                y1 = min(1000, y0 + 1)

            extracted_words.append(word_text)
            extracted_boxes.append([x0, y0, x1, y1])

        # Render first page as PIL Image for LayoutLMv3 visual stream
        page_image = first_page.to_image(resolution=72).original.convert("RGB")

        # Collect text across all pages for full regex & semantic analysis
        for p in pdf.pages:
            text = p.extract_text()
            if text:
                full_text_lines.append(text)

    full_document_text = "\n".join(full_text_lines)

    # -----------------------------------------------------------------------
    # LayoutLMv3 Multimodal Forward Pass
    # -----------------------------------------------------------------------
    # Cap token length to standard model max (512 tokens)
    sample_words = extracted_words[:120] if extracted_words else ["Job", "Description"]
    sample_boxes = extracted_boxes[:120] if extracted_boxes else [[0, 0, 100, 100]]

    encoding = processor(
        page_image,
        sample_words,
        boxes=sample_boxes,
        return_tensors="pt",
        truncation=True,
        padding="max_length",
        max_length=512
    )

    with torch.no_grad():
        layout_output = layoutlm_model(**encoding)

    # -----------------------------------------------------------------------
    # Entity Extraction (Spatial Heuristics + Pattern Engineering)
    # -----------------------------------------------------------------------
    # 1. Email Extraction
    email_match = re.search(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", full_document_text)
    detected_email = email_match.group(0).strip() if email_match else "Not Found"

    # 2. Phone Extraction (Supports GCC formats: +974, +968, +960, etc.)
    phone_match = re.search(
        r"(\+?\d{1,4}[-.\s]?)?(\(?\d{2,4}\)?[-.\s]?)?\d{3,4}[-.\s]?\d{3,4}\b",
        full_document_text
    )
    detected_phone = phone_match.group(0).strip() if phone_match else "Not Found"

    # 3. Name & Role Title Spatial Discovery
    # In luxury resumes & job specs, the Candidate Name / Document Title is located in the top 20%
    top_words = [
        w["text"] for w in page_words 
        if float(w["top"]) < (page_height * 0.22)
    ]
    
    # Target Luxury Hospitality Roles
    known_roles = [
        "Executive Chef", "Executive Sous Chef", "Chef de Cuisine",
        "Chef de Partie", "Demi Chef de Partie", "Commis Chef",
        "Pastry Chef", "Culinary Director", "Food & Beverage Director",
        "Restaurant Manager", "General Manager", "Hotel Manager",
        "Sommelier", "Bar Manager", "Front Office Manager"
    ]
    
    detected_role = "Not Found"
    for role in known_roles:
        if re.search(rf"\b{re.escape(role)}\b", full_document_text, re.IGNORECASE):
            detected_role = role
            break

    # Candidate / Contact Name detection (Top prominent header line)
    detected_name = "Not Found"
    first_few_lines = [line.strip() for line in full_document_text.splitlines() if line.strip()]
    if first_few_lines:
        for line in first_few_lines[:4]:
            # Exclude lines that are emails, URLs, or known roles
            if "@" in line or "http" in line or detected_role.lower() in line.lower():
                continue
            # Typical name has 2 to 4 capitalized words
            words_in_line = line.split()
            if 1 <= len(words_in_line) <= 4 and all(w[0].isupper() or w[0] == '(' for w in words_in_line if w):
                detected_name = line
                break
        if detected_name == "Not Found" and first_few_lines:
            detected_name = first_few_lines[0]

    # -----------------------------------------------------------------------
    # Zero-Shot Section Classification (Sample check using BART)
    # -----------------------------------------------------------------------
    classified_sections = {}
    candidate_sections = ["Requirements", "Responsibilities", "Contact Info", "Qualifications", "Benefits"]
    
    # Classify a sample meaningful chunk if document text is available
    if len(full_document_text) > 40:
        sample_chunk = full_document_text[:400]
        try:
            classifier = get_bart_classifier()
            cls_res = classifier(sample_chunk, candidate_labels=candidate_sections)
            classified_sections = {
                lbl: round(score, 4)
                for lbl, score in zip(cls_res["labels"][:3], cls_res["scores"][:3])
            }
        except Exception as e:
            classified_sections = {"error": str(e)}

    return {
        "status": "ready",
        "pdf_path": pdf_path,
        "page_count": num_pages,
        "words_extracted": len(extracted_words),
        "layout_embeddings_shape": list(layout_output.last_hidden_state.shape),
        "entities": {
            "name": detected_name,
            "email": detected_email,
            "phone": detected_phone,
            "role_title": detected_role,
        },
        "classified_sections": classified_sections,
        "text_preview": full_document_text[:250].replace("\n", " ").strip() + "..."
    }


if __name__ == "__main__":
    import json
    
    test_pdf = "Muhammed_Ansheed_Executive_Resume.pdf"
    if os.path.exists(test_pdf):
        print(f"Testing analyze_job_structure on local PDF: {test_pdf}...")
        result = analyze_job_structure(test_pdf)
        print("\n--- Extraction Result ---")
        print(json.dumps(result, indent=2))
    else:
        print("No sample PDF found to test directly. Function is defined and ready.")
