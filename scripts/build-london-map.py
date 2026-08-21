#!/usr/bin/env python3
"""Build London itinerary map data for Amy's trip.
Computes pairwise walking distance/time via OSRM, writes london-map-data.js.
"""
import json, math, time, urllib.request, urllib.parse

# name, short label, category key, emoji, lat, lon, note, walkable, transport
SITES = [
    # Bookshops
    ("Waterstones Piccadilly", "Waterstones", "books", "\U0001F4DA", 51.5089945, -0.1358776,
     "The UK's biggest bookshop \u2014 six floors. This is 'the best biggest bookshop'.", True, ""),
    ("Daunt Books", "Daunt Books", "books", "\U0001F4DA", 51.5204371, -0.1522504,
     "Edwardian oak galleries, Marylebone \u2014 often called London's most beautiful bookshop.", True, ""),
    ("The Notting Hill Bookshop", "Notting Hill Bookshop", "books", "\U0001F4DA", 51.5156688, -0.2055627,
     "The little travel bookshop from the film Notting Hill. Pairs with Portobello Road.", True, ""),
    ("Hatchards", "Hatchards", "books", "\U0001F4DA", 51.5084753, -0.1380491,
     "London's oldest bookshop (est. 1797), on Piccadilly.", True, ""),
    # Harry Potter
    ("Platform 9\u00BE", "Platform 9\u00BE", "hp", "\U0001FA84", 51.5304907, -0.1216553,
     "King's Cross, Western Concourse \u2014 free photo op + Harry Potter shop. Next to the British Library.", True, ""),
    ("House of MinaLima", "House of MinaLima", "hp", "\U0001FA84", 51.5144594, -0.1348347,
     "Soho \u2014 free Harry Potter graphic-art gallery (graphic designers of the films).", True, ""),
    ("Cecil Court", "Cecil Court", "hp", "\U0001FA84", 51.5107273, -0.1276572,
     "The bookish lane off Charing Cross Road said to have inspired Diagon Alley.", True, ""),
    # Theatre
    ("Moulin Rouge (Piccadilly Theatre)", "Moulin Rouge", "theatre", "\U0001F3AD", 51.5109973, -0.1353952,
     "West End musical \u2014 NOT booked yet.", True, ""),
    ("Wicked (Apollo Victoria)", "Wicked", "theatre", "\U0001F3AD", 51.4955925, -0.1425702,
     "Friday night \u2014 booked.", True, ""),
    # Museums & Library
    ("British Library", "British Library", "museum", "\U0001F3DB", 51.5299119, -0.1276918,
     "Treasures Gallery \u2014 Magna Carta, Beatles lyrics, Jane Austen. Free.", True, ""),
    ("British Museum", "British Museum", "museum", "\U0001F3DB", 51.5193118, -0.1267051,
     "Rosetta Stone, Parthenon marbles. Free.", True, ""),
    ("Science Museum", "Science Museum", "museum", "\U0001F3DB", 51.4973983, -0.1746726,
     "South Kensington. Free.", True, ""),
    ("Natural History Museum", "Natural History Museum", "museum", "\U0001F3DB", 51.4965109, -0.1760019,
     "South Kensington, next door to the Science Museum. Free.", True, ""),
    ("V&A Museum", "V&A Museum", "museum", "\U0001F3DB", 51.4968838, -0.1716372,
     "South Kensington \u2014 design, fashion, sculpture. Free.", True, ""),
    ("National Maritime Museum", "Maritime Museum", "museum", "\U0001F3DB", 51.4807759, -0.0051225,
     "Greenwich \u2014 pair with the Cutty Sark + Prime Meridian. ~30 min by DLR/boat from central.", True, "DLR / Thames Clipper"),
    # Food, tea & wine
    ("High tea \u2014 Kensington Gardens", "High tea (Kensington)", "food", "\U0001F375", 51.5050865, -0.1876275,
     "The Orangery at Kensington Palace \u2014 check tea service ahead; Fortnum's is the dependable backup.", True, ""),
    ("Fortnum & Mason", "Fortnum & Mason", "food", "\U0001F375", 51.5081856, -0.1381409,
     "Diamond Jubilee Tea Salon \u2014 the classic afternoon tea.", True, ""),
    ("Borough Market", "Borough Market", "food", "\U0001F374", 51.5055815, -0.0901984,
     "Lunch stop \u2014 street food under the railway arches by London Bridge.", True, ""),
    # Landmarks & shops
    ("Buckingham Palace", "Buckingham Palace", "landmark", "\U0001F451", 51.5008349, -0.1430045,
     "Changing the Guard \u2014 check days/times.", True, ""),
    ("Big Ben", "Big Ben", "landmark", "\U0001F5FC", 51.5007042, -0.1245721,
     "Westminster \u2014 Houses of Parliament, Westminster Abbey all within a short walk.", True, ""),
    ("Harrods", "Harrods", "landmark", "\U0001F6CD", 51.4992104, -0.1629893,
     "Knightsbridge \u2014 the food halls are worth a wander.", True, ""),
    ("Tower of London", "Tower of London", "landmark", "\U0001F3F0", 51.508217, -0.0761879,
     "Crown Jewels + Tower Bridge right beside it.", True, ""),
    ("LEGO Store Leicester Square", "LEGO Store", "landmark", "\U0001F9F1", 51.5105945, -0.1307672,
     "Leicester Square flagship \u2014 next to M&M's World.", True, ""),
    # Base & Family (outside central London — not in the walking matrix)
    ("Dartford (Airbnb base)", "Dartford \u2014 Airbnb", "base", "\U0001F3E0", 51.4443059, 0.21807,
     "Your Airbnb base \u2014 outside central London.", False, "Train from London Bridge/Charing Cross ~35 min"),
    ("Hornchurch", "Hornchurch", "base", "\U0001F3E0", 51.5538747, 0.2181092,
     "Hornchurch, East London.", False, "District line (Zone 6) ~50 min from central"),
]

CATS = {
    "books":    ("Bookshops", "\U0001F4DA", "#cc785c"),
    "hp":       ("Harry Potter", "\U0001FA84", "#7c5cbf"),
    "theatre":  ("Theatre & Shows", "\U0001F3AD", "#c0392b"),
    "museum":   ("Museums & Library", "\U0001F3DB", "#5b8cb8"),
    "food":     ("Food, Tea & Wine", "\U0001F375", "#c2680c"),
    "landmark": ("Landmarks & Shops", "\U0001F451", "#2e7d32"),
    "base":     ("Base & Family", "\U0001F3E0", "#2a9d8f"),
}

def haversine(lat1, lon1, lat2, lon2):
    R = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp/2)**2 + math.cos(p1)*math.cos(p2)*math.sin(dl/2)**2
    return 2 * R * math.asin(math.sqrt(a))

def main():
    walkable = [s for s in SITES if s[7]]  # exclude non-walkable (base/family, day trips)
    n = len(walkable)
    names = [s[0] for s in walkable]

    # Try OSRM table API
    matrix = None
    source = "osrm"
    try:
        coords = ";".join(f"{lon},{lat}" for _,_,_,_,lat,lon,_,_,_ in walkable)
        url = f"https://router.project-osrm.org/table/v1/walking/{coords}?annotations=duration,distance"
        req = urllib.request.Request(url, headers={"User-Agent": "gary-pages-london-map/1.0"})
        with urllib.request.urlopen(req, timeout=60) as r:
            data = json.loads(r.read().decode())
        dur = data["durations"]  # seconds
        dist = data["distances"]  # metres
        matrix = []
        for i in range(n):
            row = []
            for j in range(n):
                if i == j:
                    row.append({"d": 0, "t": 0})
                else:
                    d_m = dist[i][j] or 0
                    t_s = dur[i][j] or 0
                    row.append({"d": round(d_m/1000, 2), "t": round(t_s/60)})
            matrix.append(row)
        print(f"OSRM table OK: {n}x{n} matrix")
    except Exception as e:
        print(f"OSRM table failed ({e}) -> haversine fallback")
        source = "haversine"
        matrix = []
        for i in range(n):
            row = []
            for j in range(n):
                if i == j:
                    row.append({"d": 0, "t": 0})
                else:
                    km = haversine(walkable[i][4], walkable[i][5], walkable[j][4], walkable[j][5])
                    d_km = km * 1.35  # walking route ~1.35x straight-line
                    t_min = (d_km / 4.4) * 60  # ~4.4 km/h effective
                    row.append({"d": round(d_km, 2), "t": round(t_min)})
            matrix.append(row)

    # Build site objects
    sites_out = []
    for s in SITES:
        name, short, cat, emoji, lat, lon, note, walkable_flag, transport = s
        sites_out.append({
            "name": name, "short": short, "cat": cat, "emoji": emoji,
            "lat": lat, "lon": lon, "note": note,
            "walkable": walkable_flag, "transport": transport,
            "color": CATS[cat][2],
        })

    out = {
        "source": source,
        "categories": {k: {"label": v[0], "emoji": v[1], "color": v[2]} for k, v in CATS.items()},
        "sites": sites_out,
        "matrix": matrix,
        "matrix_names": names,
    }
    json_str = json.dumps(out, ensure_ascii=False)
    html_path = "/Users/gary/gary-pages/london-itinerary.html"
    html = open(html_path).read()
    sm = "//__DATA_START__"
    em = "//__DATA_END__"
    start = html.index(sm) + len(sm)
    end = html.index(em)
    html = html[:start] + "\nconst LONDON_DATA = " + json_str + ";\n" + html[end:]
    open(html_path, "w").write(html)
    print(f"Inlined data into {html_path} ({len(json_str)} chars)")

    wi = names.index("Waterstones Piccadilly")
    sample = sorted(
        [(names[j], matrix[wi][j]["t"], matrix[wi][j]["d"]) for j in range(n) if j != wi],
        key=lambda x: x[1]
    )[:6]
    print("Nearest walking from Waterstones Piccadilly:")
    for nm, t, d in sample:
        print(f"  {nm:28s} {t:4d} min  {d:5.2f} km")

if __name__ == "__main__":
    main()
