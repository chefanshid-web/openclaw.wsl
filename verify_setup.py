import os
import sys
import traceback

def main():
    print("=" * 60)
    print(" HUGGING FACE & DEVOPS ENVIRONMENT VERIFICATION")
    print("=" * 60)
    print(f"Python interpreter : {sys.executable}")
    print(f"Python version     : {sys.version.split()[0]}")
    
    # 1. Imports
    print("\n--- [1. Core Libraries Verification] ---")
    import torch
    import transformers
    import datasets
    import pdfplumber
    import playwright
    import bs4
    from PIL import Image

    print(f"[OK] torch version           : {torch.__version__} (CUDA available: {torch.cuda.is_available()})")
    print(f"[OK] transformers version    : {transformers.__version__}")
    print(f"[OK] datasets version        : {datasets.__version__}")
    print(f"[OK] pdfplumber version      : {pdfplumber.__version__}")
    print(f"[OK] beautifulsoup4 version  : {bs4.__version__}")
    import importlib.metadata
    print(f"[OK] playwright version      : {importlib.metadata.version('playwright')}")

    # 2. Model A: LayoutLMv3
    print("\n--- [2. Model A: microsoft/layoutlmv3-base Verification] ---")
    from transformers import AutoProcessor, AutoModel
    processor = AutoProcessor.from_pretrained("microsoft/layoutlmv3-base", apply_ocr=False)
    model_a = AutoModel.from_pretrained("microsoft/layoutlmv3-base")
    print("[OK] LayoutLMv3 processor & model loaded successfully!")

    # Forward pass test with synthetic layout
    img = Image.new("RGB", (224, 224), color=(255, 255, 255))
    words = ["Executive", "Chef", "Qatar", "Luxury", "Hotel"]
    boxes = [
        [10, 10, 50, 30],
        [55, 10, 95, 30],
        [10, 35, 60, 55],
        [65, 35, 120, 55],
        [125, 35, 180, 55]
    ]
    boxes_1000 = [[int(coord * 1000 / 224) for coord in b] for b in boxes]
    inputs = processor(img, words, boxes=boxes_1000, return_tensors="pt")
    with torch.no_grad():
        out_a = model_a(**inputs)
    print(f"[OK] LayoutLMv3 inference successful! Tensor output: {out_a.last_hidden_state.shape}")

    # 3. Model B: facebook/bart-large-mnli
    print("\n--- [3. Model B: facebook/bart-large-mnli Verification] ---")
    from transformers import pipeline, AutoTokenizer, AutoModelForSequenceClassification
    tokenizer = AutoTokenizer.from_pretrained("facebook/bart-large-mnli")
    model_b = AutoModelForSequenceClassification.from_pretrained("facebook/bart-large-mnli")
    classifier = pipeline("zero-shot-classification", model=model_b, tokenizer=tokenizer, device=-1)
    
    test_text = "Candidate must possess 5+ years experience as Chef de Partie or Sous Chef in luxury 5-star hotel in Qatar or Maldives."
    candidate_sections = ["Requirements", "Responsibilities", "Benefits", "Contact Info"]
    res = classifier(test_text, candidate_labels=candidate_sections)
    
    print("[OK] BART-large-mnli zero-shot classification test successful!")
    print(f"Sample input : \"{test_text}\"")
    print(f"Top label    : {res['labels'][0]} ({res['scores'][0]*100:.2f}%)")
    for label, score in zip(res["labels"], res["scores"]):
        print(f"  - {label:<18}: {score*100:.2f}%")

    print("\n" + "=" * 60)
    print(" ALL CHECKS PASSED: Environment & Models are 100% OPERATIONAL")
    print("=" * 60)

if __name__ == "__main__":
    main()
