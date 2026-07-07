# ❌ DELETE THIS ENTIRE FUNCTION:
def generate_festival_list(years: list[int]) -> list[dict] | None:
    if not can_use_gemini(): return None
    prompt = (
        f"You are an expert in the Hindu Panchang and a creative social media manager for a devotional page. "
        f"List all major Hindu festivals for the years {years[0]} and {years[1]}. "
        "For each festival, provide 3 short, aesthetic content ideas for an AI-generated Little Krishna video page. "
        "Return ONLY a valid JSON array of objects. Do not use markdown code blocks. "
        "Format: [{\"name\": \"Festival Name\", \"date\": \"YYYY-MM-DD\", \"ideas\": [\"idea 1\", \"idea 2\", \"idea 3\"]}]"
    )
    result = _generate(prompt, max_length=4000, task_type="dm")
    if not result: return None
    
    start_idx = result.find('[')
    end_idx = result.rfind(']')
    if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
        json_str = result[start_idx:end_idx+1]
        try:
            parsed = json.loads(json_str)
            if isinstance(parsed, list) and len(parsed) > 0: return parsed
        except json.JSONDecodeError:
            pass
    logger.error(f"Failed to parse festival JSON from Gemini: {result[:200]}")
    return None