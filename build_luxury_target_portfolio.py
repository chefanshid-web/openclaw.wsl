"""
Master Luxury Hospitality & Cruise Line Openings Generator
Target Candidate: Chef Muhammed Ansheed
Target Markets: Qatar, Oman, Maldives, Ultra-Luxury Cruise Lines
"""

import csv
import json

TARGET_OPENINGS = [
    # -------------------------------------------------------------
    # 1. ULTRA-LUXURY CRUISE LINES (NON-SEABOURN)
    # -------------------------------------------------------------
    {
        "role_title": "Asst. Chef de Partie (Demi CDP Equivalent)",
        "company": "Explora Journeys (MSC Ultra-Luxury Fleet)",
        "destination": "Ultra-Luxury Cruise Fleet (Global Maritime)",
        "match_score": "98%",
        "strategic_advantage": "Explora Journeys is the premier new ultra-luxury cruise brand. Direct Demi CDP station ownership.",
        "apply_url": "https://www.allcruisejobs.com/i56898/asst-chef-de-partie/",
        "platform": "AllCruiseJobs / Explora Direct"
    },
    {
        "role_title": "Chef de Partie",
        "company": "Explora Journeys (MSC Ultra-Luxury Fleet)",
        "destination": "Ultra-Luxury Cruise Fleet (Global Maritime)",
        "match_score": "95%",
        "strategic_advantage": "Full station chef on luxury yacht-style vessel. 7+ years 5-star experience qualifies directly.",
        "apply_url": "https://www.allcruisejobs.com/i57172/chef-de-partie/",
        "platform": "AllCruiseJobs / Explora Direct"
    },
    {
        "role_title": "Head Butcher / Central Butchery Lead",
        "company": "Explora Journeys (MSC Ultra-Luxury Fleet)",
        "destination": "Ultra-Luxury Cruise Fleet (Global Maritime)",
        "match_score": "96%",
        "strategic_advantage": "Directly matches Ansheed's 80-100 kg/day solo central butchery mastery at Oaks Ibn Battuta Gate.",
        "apply_url": "https://www.allcruisejobs.com/i57128/head-butcher/",
        "platform": "AllCruiseJobs / Explora Direct"
    },
    {
        "role_title": "Junior Sous Chef",
        "company": "Explora Journeys (MSC Ultra-Luxury Fleet)",
        "destination": "Ultra-Luxury Cruise Fleet (Global Maritime)",
        "match_score": "90%",
        "strategic_advantage": "Executive Chef / Founder experience at Bayt Al Bites gives Ansheed strong supervisory leadership.",
        "apply_url": "https://www.allcruisejobs.com/i56894/junior-sous-chef/",
        "platform": "AllCruiseJobs / Explora Direct"
    },
    {
        "role_title": "River - Chef de Partie",
        "company": "Viking Cruises",
        "destination": "European Luxury Waterways",
        "match_score": "92%",
        "strategic_advantage": "High culinary precision, luxury hospitality standards, structured seasonal rotations.",
        "apply_url": "https://www.allcruisejobs.com/i47258/river-chef-de-partie/",
        "platform": "AllCruiseJobs / Viking Direct"
    },
    {
        "role_title": "Ocean - Chef de Cuisine / Sous Chef",
        "company": "Viking Ocean Cruises",
        "destination": "Global Ocean Luxury Fleet",
        "match_score": "88%",
        "strategic_advantage": "Fine dining restaurant concept leadership matching Michelin-listed Sand & Koal experience.",
        "apply_url": "https://www.allcruisejobs.com/i47232/ocean-sous-chef/",
        "platform": "AllCruiseJobs / Viking Direct"
    },

    # -------------------------------------------------------------
    # 2. OMAN (MUSCAT 5-STAR LUXURY)
    # -------------------------------------------------------------
    {
        "role_title": "Demi Chef de Partie",
        "company": "Mandarin Oriental, Muscat",
        "destination": "Muscat, Oman",
        "match_score": "99%",
        "strategic_advantage": "HIGHEST PRIORITY: Internal brand transfer from Emirates Palace Mandarin Oriental Abu Dhabi!",
        "apply_url": "https://careers.mandarinoriental.com/",
        "platform": "Workday / Mandarin Oriental Careers"
    },
    {
        "role_title": "Demi Chef de Partie",
        "company": "Jumeirah Muscat Bay",
        "destination": "Muscat, Oman",
        "match_score": "97%",
        "strategic_advantage": "Direct brand alumni advantage from 2-year tenure at Jumeirah Beach Hotel Dubai.",
        "apply_url": "https://careers.jumeirah.com/",
        "platform": "Oracle Taleo / Jumeirah Careers"
    },
    {
        "role_title": "Chef de Partie / Demi Chef de Partie",
        "company": "Al Bustan Palace, A Ritz-Carlton Hotel",
        "destination": "Muscat, Oman",
        "match_score": "94%",
        "strategic_advantage": "Iconic palace luxury property in Muscat matching Ansheed's local familiarity (Bayt Al Bites Muscat).",
        "apply_url": "https://careers.marriott.com/en-US/job/search?keywords=chef&location=Muscat%2C+Oman",
        "platform": "Marriott Careers / Workday"
    },
    {
        "role_title": "Chef de Partie (Fine Dining / Hot Kitchen)",
        "company": "The Chedi Muscat (GHM Luxury)",
        "destination": "Muscat, Oman",
        "match_score": "93%",
        "strategic_advantage": "Leading Hotels of the World member renowned for world-class culinary excellence.",
        "apply_url": "https://www.ghmhotels.com/en/muscat/careers/",
        "platform": "The Chedi Direct"
    },

    # -------------------------------------------------------------
    # 3. QATAR (DOHA 5-STAR & ULTRA-LUXURY)
    # -------------------------------------------------------------
    {
        "role_title": "Demi Chef de Partie / Chef de Partie",
        "company": "Raffles & Fairmont Doha (Katara Towers)",
        "destination": "Doha, Qatar",
        "match_score": "96%",
        "strategic_advantage": "Accor Luxury landmark property. Ansheed has prior Accor experience at Mövenpick Dubai.",
        "apply_url": "https://careers.accor.com/global/en/search-results?keywords=chef&location=Doha%2C+Qatar",
        "platform": "Accor Careers / Taleo"
    },
    {
        "role_title": "Demi Chef de Partie (Butchery / Hot Line)",
        "company": "Waldorf Astoria Doha Lusail (Hilton Luxury)",
        "destination": "Doha, Qatar",
        "match_score": "95%",
        "strategic_advantage": "Ultra-luxury hospitality resort with dedicated butchery & high-volume fine-dining outlets.",
        "apply_url": "https://jobs.hilton.com/us/en/search-results?keywords=Demi+Chef+de+Partie&location=Doha%2C+Qatar",
        "platform": "Hilton Careers"
    },
    {
        "role_title": "Demi Chef de Partie",
        "company": "Mandarin Oriental, Doha (Msheireb Downtown)",
        "destination": "Doha, Qatar",
        "match_score": "98%",
        "strategic_advantage": "Internal brand mobility candidate from Emirates Palace Mandarin Oriental Abu Dhabi.",
        "apply_url": "https://careers.mandarinoriental.com/en-US/search?keywords=Doha",
        "platform": "Workday / Mandarin Oriental Careers"
    },
    {
        "role_title": "Demi Chef de Partie",
        "company": "The St. Regis Doha & St. Regis Marsa Arabia",
        "destination": "Doha, Qatar",
        "match_score": "94%",
        "strategic_advantage": "Flagship 5-star luxury banquets & fine dining; perfect fit for 900+ covers & Michelin standards.",
        "apply_url": "https://careers.marriott.com/en-US/job/search?keywords=Demi+Chef&location=Doha%2C+Qatar",
        "platform": "Marriott Careers"
    },
    {
        "role_title": "Demi Chef de Partie (All Day Dining & Banquets)",
        "company": "InterContinental Doha Beach & Spa (IHG)",
        "destination": "Doha, Qatar",
        "match_score": "92%",
        "strategic_advantage": "High-volume luxury beach resort matching Jumeirah Beach Hotel operational pedigree.",
        "apply_url": "https://careers.ihg.com/en/jobs?keywords=Demi+Chef&location=Doha",
        "platform": "IHG Careers"
    },

    # -------------------------------------------------------------
    # 4. MALDIVES (ULTRA-LUXURY PRIVATE ISLAND RESORTS)
    # -------------------------------------------------------------
    {
        "role_title": "Demi Chef de Partie / Chef de Partie",
        "company": "Waldorf Astoria Maldives Ithaafushi",
        "destination": "South Malé Atoll, Maldives",
        "match_score": "96%",
        "strategic_advantage": "Top-ranked ultra-luxury resort globally; multi-station fine dining & grill specialization.",
        "apply_url": "https://jobs.hilton.com/us/en/search-results?keywords=chef&location=Maldives",
        "platform": "Hilton Careers"
    },
    {
        "role_title": "Demi Chef de Partie / CDP",
        "company": "Anantara Kihavah & Anantara Veli Maldives",
        "destination": "Baa Atoll & South Malé, Maldives",
        "match_score": "95%",
        "strategic_advantage": "Minor Hotels luxury portfolio; high-end international guest profile with strict HACCP compliance.",
        "apply_url": "https://careers.minorhotels.com/search/?q=chef&locationsearch=Maldives",
        "platform": "Workday / Minor Hotels"
    },
    {
        "role_title": "Demi Chef de Partie",
        "company": "Mövenpick Resort Kuredhivaru Maldives (Accor)",
        "destination": "Noonu Atoll, Maldives",
        "match_score": "97%",
        "strategic_advantage": "Prior Mövenpick Dubai tenure gives Ansheed direct brand heritage advantage.",
        "apply_url": "https://careers.accor.com/global/en/search-results?keywords=chef&location=Maldives",
        "platform": "Accor Careers"
    },
    {
        "role_title": "Chef de Partie / Demi Chef de Partie",
        "company": "One&Only Reethi Rah (Kerzner International)",
        "destination": "North Malé Atoll, Maldives",
        "match_score": "95%",
        "strategic_advantage": "World-renowned ultra-luxury benchmark. Exceptional compensation, service charge, and prestige.",
        "apply_url": "https://www.kerznercareers.com/search/?q=chef&locationsearch=Maldives",
        "platform": "Kerzner International Careers"
    }
]

def main():
    csv_file = "luxury_culinary_openings.csv"
    with open(csv_file, "w", newline="", encoding="utf-8") as f:
        fieldnames = [
            "match_score", "role_title", "company", "destination", 
            "strategic_advantage", "apply_url", "platform"
        ]
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(TARGET_OPENINGS)
    
    print(f"Exported {len(TARGET_OPENINGS)} high-priority luxury openings to: {csv_file}")

if __name__ == "__main__":
    main()
