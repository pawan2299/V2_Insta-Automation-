from datetime import datetime, timezone, timedelta

# 🌟 2026/2027 Hindu Festival Calendar (Add/Update dates as needed)
FESTIVALS = [
    {"name": "Guru Purnima", "date": "2026-07-29", "ideas": ["Guru Vandana Reel", "Spiritual lineage quotes", "Thanking mentors"]},
    {"name": "Janmashtami", "date": "2026-08-14", "ideas": ["Birth story cinematic reel", "Fasting recipes carousel", "Midnight Aarti aesthetic"]},
    {"name": "Ganesh Chaturthi", "date": "2026-08-25", "ideas": ["Modak recipes", "Vighnaharta shlokas", "Eco-friendly Ganesha tips"]},
    {"name": "Navratri (Start)", "date": "2026-10-10", "ideas": ["9 Days of Shakti series", "Fasting do's and don'ts", "Garba aesthetic reels"]},
    {"name": "Dussehra", "date": "2026-10-19", "ideas": ["Victory of Dharma over Adharma", "Ravana Dahan visuals", "Weapon worship (Shastra Puja)"]},
    {"name": "Diwali", "date": "2026-11-08", "ideas": ["Ayodhya return story", "Diya lighting aesthetics", "Lakshmi-Kubera mantras"]},
    {"name": "Govardhan Puja", "date": "2026-11-09", "ideas": ["Annakut darshan", "Govardhan shila worship", "Krishna lifting the hill"]},
    {"name": "Bhai Dooj", "date": "2026-11-10", "ideas": ["Yamraj and Yami story", "Sibling bond quotes", "Tilak aesthetics"]},
    {"name": "Maha Shivaratri", "date": "2027-02-24", "ideas": ["Night of Shiva timelapse", "Mahamrityunjaya mantra", "Fasting guide"]},
    {"name": "Holi", "date": "2027-03-22", "ideas": ["Radha-Krishna Holi leela", "Natural colors guide", "Braj bhumi aesthetics"]},
    {"name": "Ram Navami", "date": "2027-04-14", "ideas": ["Ayodhya visuals", "Hanuman chalisa focus", "Maryada Purushottam quotes"]},
    {"name": "Hanuman Jayanti", "date": "2027-04-20", "ideas": ["Sundarkand excerpts", "Strength & devotion reels", "Sindoor aesthetics"]},
    {"name": "Rath Yatra", "date": "2027-06-25", "ideas": ["Puri Jagannath visuals", "Chariot pulling aesthetics", "Jai Jagannath chants"]},
]

def get_upcoming_festivals(days_ahead: int = 30) -> list:
    """Returns festivals happening within the next X days."""
    # Use IST for accurate Indian festival tracking
    ist_today = (datetime.now(timezone.utc) + timedelta(hours=5, minutes=30)).date()
    upcoming = []
    
    for fest in FESTIVALS:
        fest_date = datetime.strptime(fest["date"], "%Y-%m-%d").date()
        days_until = (fest_date - ist_today).days
        
        if 0 <= days_until <= days_ahead:
            fest_copy = fest.copy()
            fest_copy["days_until"] = days_until
            fest_copy["date_obj"] = fest_date
            upcoming.append(fest_copy)
            
    return sorted(upcoming, key=lambda x: x["days_until"])
