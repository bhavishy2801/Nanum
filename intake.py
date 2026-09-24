"""Donor free text -> draft donation card (§17.4). The LLM only extracts; it never makes safety calls.

The regex parser always runs, and the result is always a draft the donor confirms.
The LLM runs only when the `anthropic` package is installed and credentials exist, and it has a 3 s limit.
Any LLM failure, or a quantity mismatch against the raw text, falls back to the regex result.
"""
import json
import os
import re

from core import HORIZON

MODEL = os.environ.get("RELAY_LLM_MODEL", "claude-opus-5")

# Rough kg per unit for weight estimates when no kg is stated [ASM, A5]
UNIT_KG = {"kg": 1.0, "plate": 0.35, "roti": 0.04, "piece": 0.05, "tray": 3.0, "box": 2.0,
           "litre": 1.0, "packet": 0.25, "loaf": 0.4}
UNIT_RE = (r"(\d+(?:\.\d+)?)\s*(kgs?|kilos?|plates?|rotis?|chapatis?|pieces?|pcs|trays?|boxes|box|"
           r"litres?|liters?|ltrs?|packets?|loaves|loaf)\b")
CANON = {"kgs": "kg", "kilo": "kg", "kilos": "kg", "plates": "plate", "rotis": "roti", "chapati": "roti",
         "chapatis": "roti", "pieces": "piece", "pcs": "piece", "trays": "tray", "boxes": "box", "litres": "litre",
         "liter": "litre", "liters": "litre", "ltr": "litre", "ltrs": "litre", "packets": "packet", "loaves": "loaf"}
CATS = [  # order matters: first match wins within a word; several categories -> "mixed"
    ("dairy", r"\b(milk|curd|dahi|yog(h)?urt|cheese|cream|custard|salad|cut fruit|raita|kheer)\b"),
    ("cooked", r"\b(biryani|rice|curry|dal|daal|sabzi|sabji|roti|rotis|chapati|pasta|noodles|pulao|khichdi|sambar|"
               r"idli|dosa|thali|meals?|paneer|chole|rajma|gravy|food)\b"),
    ("bakery", r"\b(bread|buns?|cakes?|pastr(y|ies)|cookies?|biscuits?|muffins?|croissants?|loaves|loaf|bakery)\b"),
    ("produce", r"\b(vegetables|veggies|fruits?|produce|tomato(es)?|onions?|potato(es)?|bananas?|apples?)\b"),
    ("packaged", r"\b(packaged|packets?|sealed|tetra ?packs?|bottled)\b"),
]
PHONE_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|\+?\d[\d\s-]{8,}\d")


def _clock(h, m, ampm):
    h, m = int(h), int(m or 0)
    if ampm:
        h = h % 12 + (12 if ampm.lower() == "pm" else 0)
    elif 1 <= h <= 11:
        h += 12   # "we close 11" in a food-rescue chat means 23:00 [ASM]
    elif h == 12:
        h = 0     # "close 12" -> midnight
    return f"{h % 24:02d}:{m:02d}"


def regex_parse(text):
    t = text.lower()
    items = [{"qty": float(q), "unit": CANON.get(u, u)} for q, u in re.findall(UNIT_RE, t)]
    kg_stated = [i["qty"] for i in items if i["unit"] == "kg"]
    kg = sum(kg_stated) if kg_stated else sum(i["qty"] * UNIT_KG[i["unit"]] for i in items) or None
    plates = sum(i["qty"] for i in items if i["unit"] == "plate") or None

    cats = [c for c, rx in CATS if re.search(rx, t)]
    if "dairy" in cats and "bakery" in cats and not {"cooked", "produce", "packaged"} & set(cats):
        category = "dairy"   # cream/custard bakery items are time/temperature controlled
    else:
        category = cats[0] if len(cats) == 1 else ("mixed" if cats else "unknown")

    if re.search(r"\b(hot|warm|hot-held|kept hot)\b", t):
        holding = "hot"
    elif re.search(r"\b(cold|chilled|fridge|refrigerated|cooler)\b", t):
        holding = "cold"
    elif category in ("bakery", "produce", "packaged"):
        holding = "ambient"
    else:
        holding = "unknown"   # clock starts at posting (conservative)

    if re.search(r"\b(non[- ]?veg|chicken|mutton|fish|eggs?|meat|prawns?|beef|pork)\b", t):
        veg = False
    elif re.search(r"\b(veg|vegetarian|jain|pure veg)\b", t) or category == "produce":
        veg = True
    else:
        veg = None

    ready_until = None
    m = re.search(r"\b(?:clos\w*|till|until|by)\s+(?:at\s+)?(\d{1,2})(?:[:.](\d{2}))?\s*(am|pm)?\b(?!\s*(?:kg|plates?|rotis?))", t)
    if m and int(m.group(1)) <= 23 and int(m.group(2) or 0) < 60:
        ready_until = _clock(*m.groups())
    return {"items": items, "category": category, "holding": holding, "veg": veg,
            "kg": round(kg, 1) if kg else None, "plates": plates, "ready_until": ready_until}


SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["items", "category", "holding", "veg", "ready_until", "weight_kg_est", "notes"],
    "properties": {
        "items": {"type": "array", "items": {
            "type": "object", "additionalProperties": False, "required": ["name", "qty", "unit"],
            "properties": {"name": {"type": "string"},
                           "qty": {"anyOf": [{"type": "number"}, {"type": "null"}]},
                           "unit": {"type": "string", "enum": ["plates", "kg", "pieces", "trays", "boxes", "litres", "unknown"]}}}},
        "category": {"type": "string", "enum": ["cooked", "bakery", "produce", "dairy", "packaged", "mixed", "unknown"]},
        "holding": {"type": "string", "enum": ["hot", "cold", "ambient", "unknown"]},
        "veg": {"type": "string", "enum": ["true", "false", "unknown"]},
        "ready_until": {"anyOf": [{"type": "string"}, {"type": "null"}]},
        "weight_kg_est": {"anyOf": [{"type": "number"}, {"type": "null"}]},
        "notes": {"type": "string"},
    },
}
SYSTEM = ("Extract a food donation from the message. Output only JSON matching the schema. Never guess safety facts: "
          "if the holding temperature isn't stated, set holding to \"unknown\". If a quantity is ambiguous, give qty "
          "as null and keep the raw phrase in notes. Don't invent items. ready_until is the 24-hour time the donor "
          "stops being available (a restaurant saying \"we close 11\" in the evening means 23:00).")


def llm_parse(text):
    """Structured extraction via Claude. Returns None on any failure so the donor never waits on the LLM."""
    try:
        import anthropic
        client = anthropic.Anthropic(timeout=3.0, max_retries=0)
        resp = client.messages.create(
            model=MODEL, max_tokens=1024, system=SYSTEM,
            messages=[{"role": "user", "content": PHONE_EMAIL.sub("[redacted]", text)}],
            output_config={"effort": "low", "format": {"type": "json_schema", "schema": SCHEMA}},
        )
        if resp.stop_reason != "end_turn":   # refusal / max_tokens -> regex fallback
            return None
        return json.loads(next(b.text for b in resp.content if b.type == "text"))
    except Exception:   # ImportError, missing credentials, timeout, API error, bad JSON: all fall back (§8.5)
        return None


def parse(text):
    draft = regex_parse(text)
    draft.update(source="regex", warnings=[])
    llm = llm_parse(text) if text.strip() else None
    if not llm:
        return draft
    numbers = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", text)}
    if any(i["qty"] is not None and i["qty"] not in numbers for i in llm["items"]):
        draft["warnings"].append("LLM quantities didn't match the message; using the rule-based parse")
        return draft
    # Safety: when the parsers disagree on category, keep the one with the SHORTER safe horizon.
    if HORIZON.get(llm["category"], 240) <= HORIZON.get(draft["category"], 240):
        draft["category"] = llm["category"]
    else:
        draft["warnings"].append(f"LLM said {llm['category']}; kept the more conservative {draft['category']}")
    if draft["holding"] == "unknown" and llm["holding"] != "unknown":
        draft["holding"] = llm["holding"]
    if draft["veg"] is None and llm["veg"] != "unknown":
        draft["veg"] = llm["veg"] == "true"
    if not draft["ready_until"] and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", llm["ready_until"] or ""):
        draft["ready_until"] = llm["ready_until"]
    if draft["kg"] is None and llm["weight_kg_est"]:
        draft["kg"] = round(llm["weight_kg_est"], 1)
    draft.update(source="llm+regex", notes=llm["notes"], items_llm=llm["items"])
    return draft
