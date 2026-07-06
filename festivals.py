from datetime import datetime, timezone, timedelta
import json
import logging

logger = logging.getLogger(__name__)

# 🛡️ Fallback Data (Sirf tab use hoga jab Gemini API down ho ya quota khatam ho)
FALLBACK_FESTIVALS = [
    {"name": "Guru Purnima", "date": "2026-07-29", "ideas": ["Guru Vandana Reel", "Spiritual lineage quotes", "Thanking mentors"]},
    {"name": "Janmashtami", "date": "2026-08-06", "ideas": ["Birth story cinematic reel", "Fasting recipes carousel", "Midnight Aarti aesthetic"]},
    {"name": "Ganesh Chaturthi", "date": "2026-08-25", "ideas": ["Modak recipes", "Vighnaharta shlokas", "Eco-friendly Ganesha tips"]},
    {"name": "Navratri (Start)", "date": "2026-10-10", "ideas": ["9 Days of Shakti series", "Fasting do's and don'ts", "Garba aesthetic reels"]},
    {"name": "Diwali", "date": "2026-11-08", "ideas": ["Ayodhya return story", "Diya lighting aesthetics", "Lakshmi-Kubera mantras"]},
    {"name": "Holi", "date": "2027-03-22", "ideas": ["Radha-Krishna Holi leela", "Natural colors guide", "Braj bhumi aesthetics"]},
]

def get_upcoming_festivals(days_ahead: int = 30) -> list:
    """
    Fetches festivals dynamically from Gemini API and caches them in the DB.
    """
    from database import get_config, set_config
    from gemini_client import generate_festival_list

    # 1. Check Database Cache (Prevents hitting Gemini on every /festivals command)
    cache_key = "festival_cache_2026_2027"
    cached_data = get_config(cache_key)
    
    festivals = []
    if cached_data:
        try:
            festivals = json.loads(cached_data)
            logger.info("✅ Loaded festivals from DB cache.")
        except Exception:
            festivals = []

    # 2. Fetch from Gemini if cache is empty or invalid
    if not festivals:
        logger.info("🔄 Fetching latest Hindu festivals from Gemini API...")
        current_year = datetime.now().year
        
        # Fetch for current year and next year
        fetched_data = generate_festival_list([current_year, current_year + 1])
        
        if fetched_data and isinstance(fetched_data, list) and len(fetched_data) > 0:
            festivals = fetched_data
            # Save to DB cache for future use
            try:
                set_config(cache_key, json.dumps(festivals))
                logger.info("💾 Successfully saved festivals to DB cache.")
            except Exception as e:
                logger.warning(f"Could not cache festivals: {e}")
        else:
            logger.warning("⚠️ Gemini failed to fetch festivals. Using hardcoded fallback.")
            festivals = FALLBACK_FESTIVALS

    # 3. Filter for upcoming festivals based on IST date
    ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
    upcoming = []
    
    for fest in festivals:
        try:
            fest_date = datetime.strptime(fest["date"], "%Y-%m-%d").date()
            days_until = (fest_date - ist_today).days
            
            # Only include festivals happening within the next 'days_ahead' days
            if 0 <= days_until <= days_ahead:
                fest_copy = fest.copy()
                fest_copy["days_until"] = days_until
                fest_copy["date_obj"] = fest_date
                upcoming.append(fest_copy)
        except Exception as e:
            logger.warning(f"Skipping invalid festival date: {fest.get('name')} - {e}")
            continue

    # 4. Return sorted by nearest date
    return sorted(upcoming, key=lambda x: x["days_until"])