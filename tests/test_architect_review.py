"""
Unit tests for Silent Architect Reviewer (src/architect_review.py) and Flag Gating in run_lab.py.
"""

import os
from pathlib import Path
from unittest.mock import patch, MagicMock
import pytest

from src.architect_review import (
    get_git_diff,
    review_diff_with_gemini,
    run_architect_review,
    FLAG_FILE,
)
from run_lab import check_architect_flag


class TestArchitectReview:

    def test_get_git_diff_success(self):
        diff = get_git_diff("HEAD~1 HEAD")
        assert isinstance(diff, str)

    def test_get_git_diff_invalid_revision(self):
        diff = get_git_diff("nonexistent_rev_12345 nonexistent_rev_67890")
        assert diff == ""

    @patch("src.architect_review.genai")
    def test_review_diff_with_gemini_approved(self, mock_genai):
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "APPROVED"
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        result = review_diff_with_gemini("diff content", api_key="dummy_key")
        assert result == "APPROVED"
        mock_model.generate_content.assert_called_once()

    @patch("src.architect_review.genai")
    def test_review_diff_with_gemini_flag(self, mock_genai):
        mock_model = MagicMock()
        mock_response = MagicMock()
        mock_response.text = "FLAG: Taker orders detected."
        mock_model.generate_content.return_value = mock_response
        mock_genai.GenerativeModel.return_value = mock_model

        result = review_diff_with_gemini("diff content", api_key="dummy_key")
        assert "FLAG" in result

    @patch("src.architect_review.review_diff_with_gemini")
    def test_run_architect_review_creates_flag_file(self, mock_review, tmp_path, monkeypatch):
        test_flag = tmp_path / "GEMINI_FLAG.md"
        monkeypatch.setattr("src.architect_review.FLAG_FILE", test_flag)

        mock_review.return_value = "FLAG: Violation of Sniper Architecture."
        result = run_architect_review(diff_text="sample diff")

        assert "FLAG" in result
        assert test_flag.exists()
        assert "Violation of Sniper Architecture" in test_flag.read_text(encoding="utf-8")

    @patch("src.architect_review.review_diff_with_gemini")
    def test_run_architect_review_clears_flag_file_on_approved(self, mock_review, tmp_path, monkeypatch):
        test_flag = tmp_path / "GEMINI_FLAG.md"
        test_flag.write_text("old flag", encoding="utf-8")
        monkeypatch.setattr("src.architect_review.FLAG_FILE", test_flag)

        mock_review.return_value = "APPROVED"
        result = run_architect_review(diff_text="sample diff")

        assert result == "APPROVED"
        assert not test_flag.exists()

    def test_run_architect_review_empty_diff(self):
        result = run_architect_review(diff_text="")
        assert result == "APPROVED"


class TestRunLabArchitectFlagGate:

    def test_check_architect_flag_returns_true_when_file_exists(self, tmp_path):
        flag_file = Path(__file__).resolve().parent.parent / "GEMINI_FLAG.md"
        flag_file.write_text("FLAG DETECTED", encoding="utf-8")
        try:
            assert check_architect_flag() is True
        finally:
            if flag_file.exists():
                flag_file.unlink()

    def test_check_architect_flag_returns_false_when_no_file(self):
        flag_file = Path(__file__).resolve().parent.parent / "GEMINI_FLAG.md"
        if flag_file.exists():
            flag_file.unlink()
        assert check_architect_flag() is False

    def test_check_architect_flag_returns_true_when_hash_mismatch(self):
        hash_file = Path(__file__).resolve().parent.parent / "approved_hash.txt"
        orig_hash = hash_file.read_text(encoding="utf-8") if hash_file.exists() else ""
        try:
            hash_file.write_text("invalid_tampered_hash_value_12345", encoding="utf-8")
            assert check_architect_flag() is True
        finally:
            if orig_hash:
                hash_file.write_text(orig_hash, encoding="utf-8")

    def test_check_architect_flag_returns_true_when_hash_file_missing(self):
        hash_file = Path(__file__).resolve().parent.parent / "approved_hash.txt"
        orig_hash = hash_file.read_text(encoding="utf-8") if hash_file.exists() else ""
        try:
            if hash_file.exists():
                hash_file.unlink()
            assert check_architect_flag() is True
        finally:
            if orig_hash:
                hash_file.write_text(orig_hash, encoding="utf-8")

    def test_save_approved_hash_creates_valid_file(self, tmp_path):
        from run_lab import save_approved_hash, calculate_src_hash
        custom_file = tmp_path / "test_approved_hash.txt"
        current_hash = calculate_src_hash()
        saved = save_approved_hash(output_file=custom_file)
        assert saved == current_hash
        assert custom_file.exists()
        assert custom_file.read_text(encoding="utf-8").strip() == current_hash
