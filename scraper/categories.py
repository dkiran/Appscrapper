"""Google Play's fixed category set (id -> display name). Used as search terms; the saved Category
column is always the app's real genre from its page."""
APP_CATEGORIES = {
    "ART_AND_DESIGN": "Art & Design", "AUTO_AND_VEHICLES": "Auto & Vehicles", "BEAUTY": "Beauty",
    "BOOKS_AND_REFERENCE": "Books & Reference", "BUSINESS": "Business", "COMICS": "Comics",
    "COMMUNICATION": "Communication", "DATING": "Dating", "EDUCATION": "Education",
    "ENTERTAINMENT": "Entertainment", "EVENTS": "Events", "FINANCE": "Finance",
    "FOOD_AND_DRINK": "Food & Drink", "HEALTH_AND_FITNESS": "Health & Fitness",
    "HOUSE_AND_HOME": "House & Home", "LIBRARIES_AND_DEMO": "Libraries & Demo",
    "LIFESTYLE": "Lifestyle", "MAPS_AND_NAVIGATION": "Maps & Navigation", "MEDICAL": "Medical",
    "MUSIC_AND_AUDIO": "Music & Audio", "NEWS_AND_MAGAZINES": "News & Magazines",
    "PARENTING": "Parenting", "PERSONALIZATION": "Personalization", "PHOTOGRAPHY": "Photography",
    "PRODUCTIVITY": "Productivity", "SHOPPING": "Shopping", "SOCIAL": "Social", "SPORTS": "Sports",
    "TOOLS": "Tools", "TRAVEL_AND_LOCAL": "Travel & Local", "VIDEO_PLAYERS": "Video Players & Editors",
    "WEATHER": "Weather",
}
GAME_CATEGORIES = {
    "GAME_ACTION": "Action games", "GAME_ADVENTURE": "Adventure games", "GAME_ARCADE": "Arcade games",
    "GAME_BOARD": "Board games", "GAME_CARD": "Card games", "GAME_CASINO": "Casino games",
    "GAME_CASUAL": "Casual games", "GAME_EDUCATIONAL": "Educational games", "GAME_MUSIC": "Music games",
    "GAME_PUZZLE": "Puzzle games", "GAME_RACING": "Racing games", "GAME_ROLE_PLAYING": "Role playing games",
    "GAME_SIMULATION": "Simulation games", "GAME_SPORTS": "Sports games", "GAME_STRATEGY": "Strategy games",
    "GAME_TRIVIA": "Trivia games", "GAME_WORD": "Word games",
}
ALL_CATEGORIES = {**APP_CATEGORIES, **GAME_CATEGORIES}


def category_terms(selected):
    """selected: "ALL", "APPS", "GAMES" or a list of ids. Returns search terms (display names)."""
    if selected in (None, [], ""):
        return []
    if selected == "ALL":
        return list(ALL_CATEGORIES.values())
    if selected == "APPS":
        return list(APP_CATEGORIES.values())
    if selected == "GAMES":
        return list(GAME_CATEGORIES.values())
    bad = [c for c in selected if c not in ALL_CATEGORIES]
    if bad:
        raise ValueError(f"unknown categories {bad}; run: python main.py --list-categories")
    return [ALL_CATEGORIES[c] for c in selected]
