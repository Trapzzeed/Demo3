import json
import math
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import URLError
from urllib.request import Request, urlopen
from urllib.parse import urlencode


ROOT = Path(__file__).resolve().parent
ENTUR_URL = "https://api.entur.io/journey-planner/v3/graphql"
GEOCODER_URL = "https://api.entur.io/geocoder/v1/autocomplete"
SCHOOL = {"name": "Halden videregående skole, Porsnes", "latitude": 59.1251, "longitude": 11.3872}


def graphql(query):
	body = json.dumps({"query": query}).encode("utf-8")
	request = Request(
		ENTUR_URL,
		data=body,
		headers={"Content-Type": "application/json", "ET-Client-Name": "halden-vgs-busboard"},
	)
	with urlopen(request, timeout=15) as response:
		result = json.loads(response.read().decode("utf-8"))
	if result.get("errors"):
		raise RuntimeError(result["errors"][0].get("message", "Entur svarte med en feil"))
	return result.get("data") or {}


def distance_meters(latitude, longitude):
	earth_radius = 6_371_000
	lat1 = math.radians(SCHOOL["latitude"])
	lat2 = math.radians(latitude)
	delta_lat = lat2 - lat1
	delta_lon = math.radians(longitude - SCHOOL["longitude"])
	haversine = math.sin(delta_lat / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin(delta_lon / 2) ** 2
	return round(earth_radius * 2 * math.atan2(math.sqrt(haversine), math.sqrt(1 - haversine)))


def search_stop_places(text):
	query = urlencode({"text": text, "size": 50, "layers": "venue", "lang": "no"})
	request = Request(
		f"{GEOCODER_URL}?{query}",
		headers={"ET-Client-Name": "halden-vgs-busboard"},
	)
	with urlopen(request, timeout=15) as response:
		result = json.loads(response.read().decode("utf-8"))
	stops = []
	for feature in result.get("features", []):
		properties = feature.get("properties") or {}
		stop_id = properties.get("id", "")
		coordinates = (feature.get("geometry") or {}).get("coordinates") or []
		if not stop_id.startswith("NSR:StopPlace:") or len(coordinates) < 2:
			continue
		longitude, latitude = coordinates[:2]
		stops.append({
			"id": stop_id,
			"name": properties.get("name") or properties.get("label") or "Holdeplass",
			"latitude": latitude,
			"longitude": longitude,
		})
	return stops


def load_departures():
	candidates = {}
	for search_text in ("Porsnes", "Halden", "Halden bussterminal"):
		for stop in search_stop_places(search_text):
			stop["distance"] = distance_meters(stop["latitude"], stop["longitude"])
			if stop["distance"] <= 5000:
				candidates[stop["id"]] = stop

	stops = sorted(candidates.values(), key=lambda stop: stop["distance"])[:8]
	if not stops:
		raise RuntimeError("Fant ingen holdeplasser nær Porsnes. Prøv å laste siden på nytt senere.")

	stop_fields = """
	  name
	  estimatedCalls(timeRange: 86400, numberOfDepartures: 100) {
		aimedDepartureTime
		expectedDepartureTime
		cancellation
		destinationDisplay { frontText }
		quay { name }
		serviceJourney { line { publicCode name transportMode } }
	  }
	"""
	stop_queries = "\n".join(
		f"stop{index}: stopPlace(id: {json.dumps(stop['id'])}) {{ {stop_fields} }}"
		for index, stop in enumerate(stops)
	)
	departures_result = graphql("{ " + stop_queries + " }")
	result_stops = []
	for index, stop in enumerate(stops):
		live_stop = departures_result.get(f"stop{index}") or {}
		calls = live_stop.get("estimatedCalls") or []
		bus_calls = [
			call for call in calls
			if (call.get("serviceJourney", {}).get("line", {}).get("transportMode") or "").lower() == "bus"
		]
		result_stops.append({
			"id": stop["id"],
			"name": live_stop.get("name") or stop["name"],
			"distance": stop["distance"],
			"latitude": stop["latitude"],
			"longitude": stop["longitude"],
			"departures": bus_calls,
		})

	return {"school": SCHOOL, "stops": result_stops}


class BusBoardHandler(SimpleHTTPRequestHandler):
	def __init__(self, *args, **kwargs):
		super().__init__(*args, directory=str(ROOT), **kwargs)

	def do_GET(self):
		if self.path != "/api/departures":
			return super().do_GET()
		try:
			payload = load_departures()
			self.send_response(200)
		except (URLError, TimeoutError, RuntimeError, ValueError) as error:
			payload = {"error": str(error)}
			self.send_response(502)
		self.send_header("Content-Type", "application/json; charset=utf-8")
		self.send_header("Cache-Control", "no-store")
		self.end_headers()
		self.wfile.write(json.dumps(payload, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
	server = ThreadingHTTPServer(("127.0.0.1", 8000), BusBoardHandler)
	print("Bussiden er klar på http://127.0.0.1:8000")
	try:
		server.serve_forever()
	except KeyboardInterrupt:
		print("\nServeren stoppes.")
		server.server_close()
