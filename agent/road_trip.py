from dotenv import load_dotenv

load_dotenv()

from datetime import date

from langchain_google_genai import ChatGoogleGenerativeAI

from langchain.tools import tool
from typing import Dict, Any
from tavily import TavilyClient

tavily_client = TavilyClient()

# Approximate road distances (km) between major Algerian cities, main highways.

_ROAD_KM = {
    ("Algiers", "Oran"): 415,
    ("Oran", "Algiers"): 415,
    ("Algiers", "Constantine"): 435,
    ("Algiers", "Setif"): 300,
    ("Algiers", "Annaba"): 510,
    ("Algiers", "Bejaia"): 225,
    ("Algiers", "Tlemcen"): 585,
    ("Algiers", "Chlef"): 200,
    ("Chlef", "Oran"): 215,
    ("Algiers", "Blida"): 50,
    ("Oran", "Tlemcen"): 150,
    ("Oran", "Sidi Bel Abbes"): 85,
}


@tool
def web_search(query: str) -> Dict[str, Any]:
    """Search the web for current, real-world information: fuel prices in Algeria (DZD per liter), hotels in a city, restaurants, weather forecast, attractions, and road conditions."""
    return tavily_client.search(query)


@tool
def road_distance(origin: str, destination: str) -> Dict[str, Any]:
    """Approximate road distance in km between two Algerian cities, using a built-in table of the main highways. Use it BEFORE planning fuel or stops."""
    key = (origin.strip().title(), destination.strip().title())
    km = _ROAD_KM.get(key)
    if km is None:
        return {
            "known": False,
            "message": f"No built-in distance for {key}. Use web_search to find the driving distance.",
        }
    return {
        "known": True,
        "origin": key[0],
        "destination": key[1],
        "distance_km": km,
        "approx_drive_hours": round(km / 95.0, 1),
        "note": "Approximate, main highways (Autoroute Est-Ouest). Prefer web_search for traffic/current conditions.",
    }


@tool
def fuel_cost(distance_km: float, consumption_l_per_100km: float, price_dzd_per_l: float) -> Dict[str, float]:
    """Fuel cost for a trip. distance_km: trip length, consumption_l_per_100km: car consumption, price_dzd_per_l: current fuel price in Algerian Dinars."""
    liters = distance_km / 100.0 * consumption_l_per_100km
    return {
        "distance_km": distance_km,
        "liters_consumed": round(liters, 1),
        "price_dzd_per_liter": price_dzd_per_l,
        "total_fuel_cost_dzd": round(liters * price_dzd_per_l, 0),
    }


@tool
def drive_leg(distance_km: float, avg_speed_kmh: float = 95.0, rest_every_km: float = 150.0, rest_minutes: float = 20.0) -> Dict[str, float]:
    """Plan a driving leg: total drive hours, how many rest stops are needed, and the real duration including breaks."""
    drive_hours = distance_km / avg_speed_kmh
    rest_stops = max(0.0, distance_km // rest_every_km)
    return {
        "distance_km": distance_km,
        "drive_hours": round(drive_hours, 1),
        "rest_stops": rest_stops,
        "rest_minutes_total": round(rest_stops * rest_minutes, 0),
        "total_hours_with_breaks": round(drive_hours + rest_stops * rest_minutes / 60.0, 1),
    }


@tool
def nights_between(check_in: str, check_out: str) -> int:
    """Number of nights between two dates, both formatted as YYYY-MM-DD (e.g. 2026-10-05). Use whenever the user gives dates."""
    a = date.fromisoformat(check_in)
    b = date.fromisoformat(check_out)
    return (b - a).days


@tool
def budget_totals(fuel_cost_dzd: float, hotel_per_night_dzd: float, meals_per_day_dzd: float, nights: int, days: int) -> Dict[str, float]:
    """Final budget breakdown in Algerian Dinars (DZD). Pass the values you computed with the other tools."""
    fuel = fuel_cost_dzd
    hotels = hotel_per_night_dzd * nights
    meals = meals_per_day_dzd * days
    return {
        "fuel_dzd": round(fuel, 0),
        "hotels_dzd": round(hotels, 0),
        "meals_dzd": round(meals, 0),
        "total_dzd": round(fuel + hotels + meals, 0),
    }


system_prompt = """
You are a road trip planner specialized in Algeria. The user will tell you a route (e.g. Algiers to Oran) and how many days.

Build the plan step by step, always in this order:
1. ROUTE: call road_distance for each leg.
2. FUEL: call fuel_cost with a realistic consumption (e.g. 7 L/100km) and the CURRENT fuel price in DZD/liter found via web_search.
3. STOPS: call drive_leg to decide where to take breaks.
4. HOTELS: use web_search to find hotels in the destination (or midpoint city if an overnight stop is needed).
5. RESTAURANTS / MEALS: use web_search for restaurant ideas or typical meal costs.
6. WEATHER & ACTIVITIES: use web_search for the forecast and attractions.
7. FINAL: assemble a day-by-day itinerary ending with a budget_totals summary in Algerian Dinars (DZD).

Rules:
- Use the tools one at a time and read each result before calling the next one.
- Distances: put the numbers from road_distance into the other formulas instead of guessing.
- Always give the final budget in Algerian Dinars.
- Reply in the language the user writes in (French or English).
- If road_distance does not know a pair of cities, verify the distance with web_search instead.
- OUTPUT POLICY: Never reveal chain-of-thought, reasoning steps, tool calls, tool names, tool inputs, tool outputs, or internal scratchpad. The answer to the user must be ONLY the final formatted plan.
- After completing all required steps, produce ONE clean final answer using markdown. Do NOT include sections like "Thought", "Tool Result", "call_*", or JSON dumps.
- Include: title, overview, day-by-day itinerary, hotels, restaurants, weather/activities, and budget totals in DZD. No extra meta commentary.
- If you must show numbers from tools, present them only as part of the final structured plan, never as raw execution traces.
"""

from langchain.agents import create_agent

agent = create_agent(
    model=ChatGoogleGenerativeAI(model="gemini-2.0-flash-lite"),
    tools=[web_search, road_distance, fuel_cost, drive_leg, nights_between, budget_totals],
    system_prompt=system_prompt
)