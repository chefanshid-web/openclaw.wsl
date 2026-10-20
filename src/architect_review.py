"""
OpenClaw Quant Lab - Silent Architect Automated Code Reviewer
Integrates the Gemini API to review recent commits / diffs for mathematical flaws,
Sniper Architecture violations (e.g., using Taker orders, removing the weekend gate,
allowing negative math), or logical bugs.
"""

import os
import sys
import subprocess
import warnings
from pathlib import Path
from dotenv import load_dotenv

# Suppress deprecation warnings from legacy google.generativeai SDK
warnings.filterwarnings("ignore", category=FutureWarning)
try:
    import google.generativeai as genai
except ImportError:
    genai = None

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FLAG_FILE = PROJECT_ROOT / "GEMINI_FLAG.md"


def get_git_diff(revision: str = "HEAD~1 HEAD", cwd: Path = PROJECT_ROOT) -> str:
    """Run git diff via subprocess to obtain recent code changes."""
    cmd = ["git", "diff"] + revision.split()
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            check=True
        )
        return result.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error running git diff: {e.stderr}", file=sys.stderr)
        return ""


def review_diff_with_gemini(diff_text: str, api_key: str = None) -> str:
    """Send code diff to Gemini API for Silent Architect review."""
    if not genai:
        raise RuntimeError("google-generativeai is not installed.")

    if not api_key:
        load_dotenv(PROJECT_ROOT / ".env")
        api_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")

    if not api_key:
        raise ValueError("GEMINI_API_KEY not found in environment or .env file.")

    genai.configure(api_key=api_key)

    strict_prompt = (
        "You are the Silent Architect for a quantitative trading desk. "
        "Review this code diff. If you see a mathematical flaw, a violation of our "
        "Sniper Architecture (e.g., using Taker orders, removing the weekend gate, "
        "allowing negative math), or a logical bug, output a FLAG. Otherwise, output APPROVED.\n\n"
        f"Code Diff:\n{diff_text}"
    )

    models_to_try = [
        "gemini-3.8-flash",
        "gemini-flash-latest",
        "gemini-3.5-flash",
        "gemini-2.5-flash",
    ]

    last_error = None
    for model_name in models_to_try:
        try:
            model = genai.GenerativeModel(model_name)
            response = model.generate_content(strict_prompt)
            if response and response.text:
                return response.text.strip()
        except Exception as e:
            last_error = e
            continue

    raise RuntimeError(f"All Gemini models failed. Last error: {last_error}")


def run_architect_review(revision: str = "HEAD~1 HEAD", diff_text: str = None) -> str:
    """
    Main review execution routine:
    1. Obtains git diff.
    2. Sends diff to Gemini API.
    3. If FLAG is detected, writes GEMINI_FLAG.md in project root.
    4. Prints result to terminal.
    """
    print(f"[Architect Review] Inspecting diff: {revision}...")
    if diff_text is None:
        diff_text = get_git_diff(revision)

    if not diff_text.strip():
        print("[Architect Review] Diff is empty. No changes detected.")
        return "APPROVED"

    review_result = review_diff_with_gemini(diff_text)
    print("\n--- Silent Architect Review Output ---")
    print(review_result)
    print("--------------------------------------\n")

    if "FLAG" in review_result.upper():
        FLAG_FILE.write_text(review_result, encoding="utf-8")
        print(f"[!] FLAG detected! Detailed report written to {FLAG_FILE}")
    else:
        # If APPROVED and an old flag file exists, remove it
        if FLAG_FILE.exists():
            FLAG_FILE.unlink()
        try:
            from run_lab import save_approved_hash
            saved_hash = save_approved_hash()
            print(f"[+] Diff APPROVED by Silent Architect. Saved approved hash: {saved_hash}")
        except Exception as e:
            print(f"[!] Warning: Could not update approved hash: {e}")
        print("[+] Diff APPROVED by Silent Architect.")

    return review_result


if __name__ == "__main__":
    rev = sys.argv[1] if len(sys.argv) > 1 else "HEAD~1 HEAD"
    run_architect_review(revision=rev)
