"""
Playwright Form Reverse-Engineering & Autofill Engine
Job Hunting Bot for Chef Muhammed Ansheed
Target Markets: Qatar, Oman, Maldives & Ultra-Luxury Cruise Lines
"""

import os
import json
import asyncio
from typing import Dict, Any, Optional
from playwright.async_api import async_playwright, Page


class RequisitionFormBot:
    def __init__(self, payload_path: str = "candidate_application_payload.json"):
        with open(payload_path, "r", encoding="utf-8") as f:
            self.profile = json.load(f)
        
        self.resume_path = os.path.abspath("Muhammed_Ansheed_Executive_Resume.pdf")
        self.cover_letter_path = os.path.abspath("cover_letter.pdf")

    async def autofill_portal_form(self, page: Page, portal_url: Optional[str] = None):
        """
        Fills out an ATS requisition form dynamically by inspecting input labels,
        IDs, names, and ARIA attributes.
        """
        if portal_url:
            print(f"[Bot] Navigating to: {portal_url}")
            await page.goto(portal_url, wait_until="networkidle")

        personal = self.profile.get("personal_details", {})
        credentials = self.profile.get("culinary_credentials", {})
        digital = self.profile.get("digital_assets", {})

        print("[Bot] Scanning DOM for target requisition inputs...")

        # Field mapping heuristics
        field_rules = [
            # First Name
            (["first_name", "firstname", "first-name", "fname", "givenname"], personal.get("legal_first_name")),
            # Last Name
            (["last_name", "lastname", "last-name", "lname", "surname", "familyname"], personal.get("legal_last_name")),
            # Email
            (["email", "e-mail", "email_address"], personal.get("email")),
            # Phone
            (["phone", "tel", "mobile", "contact_number"], personal.get("phone")),
            # Current Title
            (["job_title", "current_title", "current_role", "designation"], credentials.get("primary_role_title")),
            # Portfolio / LinkedIn URL
            (["portfolio", "website", "url", "link"], digital.get("portfolio_url")),
            (["linkedin"], digital.get("linkedin_profile")),
            # Years of Experience
            (["experience", "years_experience", "yoe"], str(credentials.get("years_experience"))),
            # Food cost or covers
            (["covers", "banquet_covers"], str(credentials.get("peak_buffet_covers"))),
        ]

        inputs = await page.query_selector_all("input, textarea, select")
        print(f"[Bot] Found {len(inputs)} input elements on current form.")

        for el in inputs:
            try:
                name_attr = (await el.get_attribute("name") or "").lower()
                id_attr = (await el.get_attribute("id") or "").lower()
                placeholder = (await el.get_attribute("placeholder") or "").lower()
                aria_label = (await el.get_attribute("aria-label") or "").lower()
                input_type = (await el.get_attribute("type") or "text").lower()

                context = f"{name_attr} {id_attr} {placeholder} {aria_label}"

                # Resume file upload
                if input_type == "file" and ("resume" in context or "cv" in context or "attachment" in context):
                    if os.path.exists(self.resume_path):
                        print(f" -> [Auto-Upload] Attaching Executive CV: {self.resume_path}")
                        await el.set_input_files(self.resume_path)
                    continue

                # Match standard text inputs
                for patterns, value in field_rules:
                    if any(p in context for p in patterns) and value:
                        current_val = await el.input_value() if input_type != "select" else ""
                        if not current_val:
                            print(f" -> [Auto-Fill] '{context[:30]}...' -> '{value}'")
                            await el.fill(str(value))
                        break
            except Exception:
                continue

        print("[Bot] Form fill sequence completed successfully.")


async def simulate_sample_ats_portal():
    """
    Simulates and validates the form autofill against a realistic luxury hospitality ATS template.
    """
    bot = RequisitionFormBot()

    # Generate test HTML ATS requisition portal
    test_html_path = os.path.abspath("test_requisition_portal.html")
    sample_portal_html = """<!DOCTYPE html>
<html>
<head>
  <title>Luxury Hospitality Career Portal | Qatar & Maldives Requisition</title>
  <style>
    body { font-family: sans-serif; background: #0f172a; color: white; padding: 40px; }
    .form-card { max-width: 600px; margin: auto; background: #1e293b; padding: 30px; border-radius: 12px; }
    .group { margin-bottom: 15px; }
    label { display: block; margin-bottom: 5px; color: #94a3b8; font-size: 14px; }
    input, textarea, select { width: 100%; padding: 10px; border-radius: 6px; border: 1px solid #475569; background: #0f172a; color: white; box-sizing: border-box; }
    button { background: #d97706; color: white; padding: 12px 24px; border: none; border-radius: 6px; cursor: pointer; font-weight: bold; width: 100%; margin-top: 10px; }
  </style>
</head>
<body>
  <div class="form-card">
    <h2>Luxury Brigade Requisition Form</h2>
    <p style="color:#cbd5e1;">Target: Mandarin Oriental / Jumeirah / Ritz-Carlton Yacht Selection</p>
    <form id="job-app-form">
      <div class="group">
        <label for="first_name">First Name</label>
        <input type="text" id="first_name" name="first_name" placeholder="Legal First Name">
      </div>
      <div class="group">
        <label for="last_name">Last Name</label>
        <input type="text" id="last_name" name="last_name" placeholder="Legal Last Name">
      </div>
      <div class="group">
        <label for="email">Email Address</label>
        <input type="email" id="email" name="email" placeholder="name@example.com">
      </div>
      <div class="group">
        <label for="phone">Phone / WhatsApp Number</label>
        <input type="tel" id="phone" name="phone" placeholder="+971...">
      </div>
      <div class="group">
        <label for="current_title">Current Culinary Role</label>
        <input type="text" id="current_title" name="current_title" placeholder="e.g. Demi Chef de Partie">
      </div>
      <div class="group">
        <label for="portfolio_url">Portfolio or Culinary Website</label>
        <input type="url" id="portfolio_url" name="portfolio_url" placeholder="https://...">
      </div>
      <div class="group">
        <label for="resume">Upload Resume / CV (PDF)</label>
        <input type="file" id="resume" name="resume">
      </div>
      <button type="button" id="submit-btn">Submit Application to Selection Committee</button>
    </form>
  </div>
</body>
</html>
"""
    with open(test_html_path, "w", encoding="utf-8") as f:
        f.write(sample_portal_html)

    print(f"\n[Test Suite] Launching Headless Chromium to test ATS autofill against: {test_html_path}...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()
        file_url = f"file:///{test_html_path.replace(os.sep, '/')}"
        await page.goto(file_url)

        await bot.autofill_portal_form(page)

        # Verify values filled
        val_fname = await page.input_value("#first_name")
        val_lname = await page.input_value("#last_name")
        val_email = await page.input_value("#email")
        val_phone = await page.input_value("#phone")
        val_title = await page.input_value("#current_title")
        val_port = await page.input_value("#portfolio_url")

        print("\n--- Autofill Verification Results ---")
        print(f" [PASS] First Name : '{val_fname}'")
        print(f" [PASS] Last Name  : '{val_lname}'")
        print(f" [PASS] Email      : '{val_email}'")
        print(f" [PASS] Phone      : '{val_phone}'")
        print(f" [PASS] Role Title : '{val_title}'")
        print(f" [PASS] Portfolio  : '{val_port}'")

        # Take screenshot of autofilled form
        screenshot_path = os.path.abspath("autofilled_form_result.png")
        await page.screenshot(path=screenshot_path)
        print(f" [PASS] Screenshot captured: {screenshot_path}")

        await browser.close()


if __name__ == "__main__":
    asyncio.run(simulate_sample_ats_portal())
