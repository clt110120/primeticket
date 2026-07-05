import os, json, re, tempfile
from flask import Flask, request, jsonify, send_file, render_template
from groq import Groq
import fitz  # PyMuPDF
from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 20 * 1024 * 1024

GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")

AIRLINE_BRANDS = {
    "thai airways":       "#7B0D1E",
    "srilankan":          "#A6192E",
    "qatar":              "#5C0632",
    "gulf air":           "#C8922A",
    "etihad":             "#BD8B13",
    "emirates":           "#CC0000",
    "singapore airlines": "#003B6F",
    "lufthansa":          "#05164D",
    "british airways":    "#075AAA",
    "air india":          "#E31837",
    "flynas":             "#FF6600",
    "flydubai":           "#E0002A",
    "indigo":             "#1A1F71",
    "air arabia":         "#E31837",
    "salam":              "#006400",
}

# Carry-on override rules — keyword in airline name → carry-on value
# Default for all other airlines: 7 kg
CARRYON_RULES = {
    "air arabia": "10 kg",
    "salam":      "5 kg",
}
DEFAULT_CARRYON = "7 kg"

def get_carryon(airline_name):
    """Return correct carry-on allowance for given airline."""
    al = airline_name.lower()
    for keyword, allowance in CARRYON_RULES.items():
        if keyword in al:
            return allowance
    return DEFAULT_CARRYON

# ── Logo helper ───────────────────────────────────────────────────────────────
def draw_logo(cv, logo_bytes, logo_ext, W, H, MARGIN, MTOP, GREY_MID_COLOR):
    """Draw uploaded logo in top-right corner. Returns y of bottom of logo area."""
    from reportlab.lib.utils import ImageReader
    import io
    T          = MTOP
    # For compact layouts (direct RT, MTOP < 20mm), start logo lower to avoid covering airline name
    logo_start_offset = 18*mm if MTOP < 20*mm else 5*mm
    LOGO_H     = 15 * mm if MTOP < 20*mm else 30 * mm
    LOGO_MAX_W = 66 * mm if MTOP < 20*mm else 132 * mm
    logo_top   = H - T - logo_start_offset

    try:
        ir     = ImageReader(io.BytesIO(logo_bytes))
        iw, ih = ir.getSize()
        scale  = LOGO_H / ih
        logo_w = iw * scale
        if logo_w > LOGO_MAX_W:
            scale  = LOGO_MAX_W / iw
            logo_w = LOGO_MAX_W
        logo_h = ih * scale
        logo_x = W - MARGIN - logo_w
        logo_bottom = logo_top - logo_h

        cv.drawImage(ImageReader(io.BytesIO(logo_bytes)),
                     logo_x, logo_bottom,
                     width=logo_w, height=logo_h,
                     preserveAspectRatio=True, mask='auto')

        return logo_bottom - 3*mm   # bottom of logo block

    except Exception as e:
        app.logger.error(f"Logo draw error: {e}")
        return H - T - 18*mm


EXTRACT_PROMPT = """You are a flight data extractor. Extract all flight booking details from the text below and return ONLY a raw JSON object. No markdown, no code fences, no explanation — just the JSON.

Use this exact structure:
{
  "passengers": [
    {
      "passenger_name": "FULL NAME IN CAPS",
      "title": "MR or MRS or MS or DR or empty string",
      "ticket_number": "ticket number as string"
    }
  ],
  "booking_ref": "PNR / airline booking reference",
  "all_refs": [
    {"label": "Airline Booking Reference or Agency Ref or PNR etc", "value": "XXXXXX"}
  ],
  "airline_name": "Full airline name",
  "brand_hex": "#hexcolor",
  "pages": [
    {
      "page_label": "Outbound Journey or Return Journey or empty string",
      "flights": [
        {
          "flight_no": "XX 123",
          "operated_by": "Airline name",
          "dep_code": "AAA",
          "dep_city": "City name",
          "dep_airport": "Airport name short",
          "dep_terminal": "Terminal X or empty string",
          "dep_time": "HH:MM",
          "dep_date": "DD Mon YYYY",
          "arr_code": "BBB",
          "arr_city": "City name",
          "arr_airport": "Airport name short",
          "arr_terminal": "Terminal X or empty string",
          "arr_time": "HH:MM",
          "arr_date": "DD Mon YYYY",
          "cabin": "Economy or Business or First",
          "carryon": "X kg or X Piece or -",
          "checked": "X kg or X Piece or -",
          "aircraft": "",
          "status": "CONFIRMED",
          "fare_type": "-",
          "seat": "-",
          "transit": null,
          "stopover": null
        }
      ]
    }
  ]
}

Rules:
- Always put passengers in the "passengers" array, even if there is only one passenger
- Each passenger has their own name, title, and ticket number
- All passengers share the same flights (pages), booking_ref, and airline info
- Extract ALL reference numbers found in the document into "all_refs" list (airline ref, agency ref, booking number, PNR, etc.)
- For "booking_ref" pick the airline's own reference (not agency/trip.com/booking.com ref)
- If there is a layover/transfer BETWEEN two flights, set transit on the FIRST flight:
  {"airport": "Airport short name", "duration": "Xhr Ymins", "baggage_status": "checked_through or reclaim"}
- If there is a technical stop/intermediate stop WITHIN a flight (same flight number, brief stop), set stopover:
  {"code": "IATA code e.g. MLE", "city": "City name", "airport": "Airport short name", "duration": "Xhr"}
- A flight can have BOTH a stopover (within the flight) AND a transit (after landing, before next flight)
- For round trips: use TWO pages — "Outbound Journey" and "Return Journey"
- For one-way: use ONE page with page_label as empty string
- brand_hex: Thai Airways=#7B0D1E, SriLankan Airlines=#A6192E, Qatar Airways=#5C0632,
  Gulf Air=#C8922A, Etihad Airways=#BD8B13, Emirates=#CC0000, Singapore Airlines=#003B6F,
  Lufthansa=#05164D, British Airways=#075AAA, Air India=#E31837, default=#1A1A1A
- Keep airport names short (max 30 chars)
- All times in 24hr HH:MM format

ITINERARY TEXT:
"""

# Keywords that indicate an airline's own reference (higher priority)
AIRLINE_REF_KEYWORDS = [
    "airline", "carrier", "flight ref", "airline ref", "airline booking",
    "airline reference", "pnr", "locator", "record locator"
]

# Keywords that indicate agency/third-party references (lower priority)
AGENCY_REF_KEYWORDS = [
    "agency", "trip.com", "booking.com", "agent", "travel agent",
    "booking no", "booking number", "booking ref", "order", "itinerary"
]

def shorten_airline(name):
    """Standardise long airline names to short form."""
    if not name:
        return name
    n = name.strip()
    # Direct replacements first
    REPLACEMENTS = {
        'srilankan airlines': 'SriLankan Airlines',
        'sri lankan airlines': 'SriLankan Airlines',
        'qatar airways': 'Qatar Airways',
        'gulf air': 'Gulf Air',
        'etihad airways': 'Etihad Airways',
        'emirates airline': 'Emirates',
        'emirates airlines': 'Emirates',
        'air arabia': 'Air Arabia',
        'flydubai': 'flydubai',
        'flynas': 'flynas',
        'salam air': 'SalamAir',
        'salamair': 'SalamAir',
        'indigo': 'IndiGo',
        'interglobe aviation': 'IndiGo',
        'air india': 'Air India',
        'singapore airlines': 'Singapore Airlines',
        'thai airways international': 'Thai Airways',
        'thai airways intl': 'Thai Airways',
        'lufthansa german airlines': 'Lufthansa',
        'british airways': 'British Airways',
        'malaysia airlines': 'Malaysia Airlines',
        'malindo air': 'Malindo Air',
        'batik air': 'Batik Air',
        'air asia': 'AirAsia',
        'airasia': 'AirAsia',
        'turkish airlines': 'Turkish Airlines',
        'oman air': 'Oman Air',
        'kuwait airways': 'Kuwait Airways',
        'royal jordanian': 'Royal Jordanian',
        'egyptair': 'EgyptAir',
        'egypt air': 'EgyptAir',
    }
    low = n.lower()
    for k, v in REPLACEMENTS.items():
        if low == k:
            return v
    # Generic truncation: if name contains "airlines" or "airways" keep as-is up to 22 chars
    # otherwise append nothing; just cap at 22 chars
    if len(n) > 22:
        truncated = n[:20].rsplit(' ', 1)[0]
        return truncated
    return n


def pick_airline_ref(data):
    """
    If all_refs has 2+ entries, pick the airline reference.
    Falls back to booking_ref if none clearly identified.
    """
    all_refs = data.get('all_refs', [])
    if len(all_refs) < 2:
        return data.get('booking_ref', '')

    # Score each ref: +1 for airline keywords, -1 for agency keywords
    best_ref = None
    best_score = -99

    for ref in all_refs:
        label = ref.get('label', '').lower()
        value = ref.get('value', '').strip()
        if not value:
            continue
        score = 0
        for kw in AIRLINE_REF_KEYWORDS:
            if kw in label:
                score += 2
        for kw in AGENCY_REF_KEYWORDS:
            if kw in label:
                score -= 2
        # Short alphanumeric codes (5-7 chars) typical of airline PNRs
        if value.isalnum() and 5 <= len(value) <= 7:
            score += 1
        if score > best_score:
            best_score = score
            best_ref = value

    return best_ref or data.get('booking_ref', '')


def extract_pdf_text(pdf_bytes):
    """Extract all text from PDF bytes using PyMuPDF."""
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    text = ""
    for page in doc:
        text += page.get_text() + "\n"
    doc.close()
    return text.strip()


def extract_with_groq(pdf_bytes_list):
    """Extract flight data from PDF text using Groq."""
    client = Groq(api_key=GROQ_API_KEY)

    # Extract text from all PDFs
    combined_text = ""
    for i, pdf_bytes in enumerate(pdf_bytes_list):
        text = extract_pdf_text(pdf_bytes)
        if len(pdf_bytes_list) > 1:
            combined_text += f"\n--- DOCUMENT {i+1} ---\n"
        combined_text += text + "\n"

    prompt = EXTRACT_PROMPT + combined_text

    response = client.chat.completions.create(
        model="llama-3.3-70b-versatile",
        messages=[{"role": "user", "content": prompt}],
        temperature=0.1,
        max_tokens=3000,
    )

    raw = response.choices[0].message.content.strip()
    raw = re.sub(r'```json|```', '', raw).strip()
    return json.loads(raw)


def generate_eticket_pdf(data, logo_bytes=None, logo_ext=None):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
    output_path = tmp.name
    tmp.close()

    W, H    = A4

    pages        = data.get('pages', [])
    total_pages  = len(pages)

    # Detect direct round trip: exactly 2 pages, 1 direct flight each, no transit
    is_direct_rt = (
        total_pages == 2 and
        all(len(p.get('flights',[])) == 1 and
            not p['flights'][0].get('transit')
            for p in pages)
    )

    # Detect single direct flight: 1 page, 1 flight, no transit
    total_flights = sum(len(p.get('flights',[])) for p in pages)
    is_single_direct = (
        total_pages == 1 and
        total_flights == 1 and
        not pages[0]['flights'][0].get('transit')
    )

    # Detect if all flights are the same airline → show small logo in card header
    all_airlines = [f.get('operated_by','').lower().strip()
                    for p in pages for f in p.get('flights',[])]
    all_same_airline = len(set(all_airlines)) == 1 and bool(logo_bytes)

    # Card logo dimensions (small — fits in 8mm card header)
    CARD_LOGO_H   = 5.5 * mm
    CARD_LOGO_MAX = 28 * mm

    # Margins and spacing
    if is_direct_rt:
        # Single page: compact header (no big logo), tight margins
        MARGIN       = 12 * mm
        MTOP         = 12 * mm
        MBOTTOM      = 12 * mm
        EXTRA_GAP    = 0 * mm
    elif is_single_direct:
        MARGIN       = 14 * mm
        MTOP         = 30 * mm
        MBOTTOM      = 30 * mm
        EXTRA_GAP    = 18 * mm
    else:
        MARGIN       = 14 * mm
        MTOP         = 20 * mm
        MBOTTOM      = 20 * mm
        EXTRA_GAP    = 0 * mm

    TGAP = 16 * mm

    # ── Chunk pages so max 2 flight cards per PDF page ────────────────────
    # Each entry: { 'flights': [...], 'page_label': str, 'is_first': bool,
    #               'all_flights_in_journey': [...], 'is_last_chunk': bool }
    CARDS_PER_PAGE = 2
    render_chunks = []
    for page in pages:
        flights    = page.get('flights', [])
        page_label = page.get('page_label', '')
        for chunk_i, start in enumerate(range(0, len(flights), CARDS_PER_PAGE)):
            chunk = flights[start:start + CARDS_PER_PAGE]
            render_chunks.append({
                'flights':                chunk,
                'page_label':             page_label if chunk_i == 0 else '',
                'is_first':               chunk_i == 0,
                'all_flights_in_journey': flights,    # full list for dot map
                'is_last_chunk':          (start + CARDS_PER_PAGE) >= len(flights),
            })

    total_chunks = len(render_chunks)

    # Recalculate total_flights after chunking for is_single_direct check
    # (already computed above, no change needed)

    BRAND      = colors.HexColor(data.get('brand_hex', '#1A1A1A'))
    BLACK      = colors.HexColor("#1A1A1A")
    GREY_DARK  = colors.HexColor("#222222")
    GREY_MID   = colors.HexColor("#4E4E4E")
    GREY_LIGHT = colors.HexColor("#DEDEDE")
    GREY_LINE  = colors.HexColor("#9B9B9B")

    cv          = canvas.Canvas(output_path, pagesize=A4)
    cv.setTitle(f"E-Ticket - {data.get('passenger_name','')}")
    cv.setAuthor('Prime Lanka Tours')

    # For single-page direct round trip, render as 1 PDF page
    render_pages  = 1 if is_direct_rt else total_pages
    display_pages = render_pages

    def hr(y, x1=None, x2=None, color=None, lw=0.4):
        cv.saveState()
        cv.setStrokeColor(color or GREY_LINE)
        cv.setLineWidth(lw)
        cv.line(x1 or MARGIN, y, x2 or W - MARGIN, y)
        cv.restoreState()

    for ci, chunk in enumerate(render_chunks):
        flights    = chunk['flights']
        page_label = chunk['page_label']
        is_first   = chunk['is_first']
        is_last_c  = chunk['is_last_chunk']
        all_j_fl   = chunk['all_flights_in_journey']
        T          = MTOP

        # ── Page break ─────────────────────────────────────────────────────
        if is_direct_rt and ci > 0:
            pass   # direct RT: both sections on one page
        elif ci > 0:
            cv.showPage()

        # ── Header (drawn on first chunk of each journey, or every chunk for continuation) ──
        draw_header = (not is_direct_rt and is_first) or (is_direct_rt and ci == 0) or (not is_direct_rt and not is_first)

        if not is_direct_rt or ci == 0:
            # Brand bar
            cv.setFillColor(BRAND)
            cv.rect(0, H - T - 4*mm, W, 4*mm, fill=1, stroke=0)

            # Airline name (left)
            cv.setFillColor(BRAND)
            cv.setFont("Helvetica-Bold", 18)
            airline_name_str = data.get('airline_name', '').upper()
            cv.drawString(MARGIN, H - T - 16*mm, airline_name_str)

            # Logo (top right) — skip for direct RT to keep header compact
            if logo_bytes and not is_direct_rt:
                logo_bottom_y = draw_logo(cv, logo_bytes, logo_ext, W, H, MARGIN, MTOP,
                                          colors.HexColor("#4E4E4E"))
            else:
                # No logo: set logo_bottom_y just below airline name + ETR label
                logo_bottom_y = H - T - 25*mm

            # "Electronic ticket receipt" below airline name, font size 10
            cv.setFillColor(colors.HexColor("#4E4E4E"))
            cv.setFont("Helvetica", 10)
            cv.drawString(MARGIN, H - T - 23*mm, "Electronic ticket receipt")

            AFTER_LOGO_GAP = 5*mm
            divider_y = logo_bottom_y - AFTER_LOGO_GAP
            hr(divider_y, lw=0.6)

            # Passenger name
            title = data.get('title', '')
            pax   = ((title + ' ') if title else '') + data.get('passenger_name', '')
            cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 13)
            pax_y = divider_y - 9*mm
            cv.drawString(MARGIN, pax_y, pax.strip())

            # Right column — anchored to divider_y so it stays in the header zone
            rx = W - MARGIN
            ry = divider_y - 4*mm
            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7.5)
            cv.drawRightString(rx, ry, f"{data.get('airline_name','')} reference")
            ry -= 5*mm
            cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 9)
            cv.drawRightString(rx, ry, data.get('booking_ref', ''))
            ry -= 7*mm
            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7.5)
            cv.drawRightString(rx, ry, "Ticket number")
            ry -= 5*mm
            cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 8)
            cv.drawRightString(rx, ry, data.get('ticket_number', ''))

            # Thank you lines
            ty_y = pax_y - 9*mm
            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 8)
            cv.drawString(MARGIN, ty_y, "Thank you for your booking.")
            cv.drawString(MARGIN, ty_y - 5*mm, "We look forward to welcoming you soon.")

            # Journey dots — show all flights in this journey
            all_f_map = [f for p in pages for f in p.get('flights',[])] if is_direct_rt else all_j_fl
            dot_y = ty_y - 18*mm
            dcodes = [all_f_map[0]['dep_code']] + [f['arr_code'] for f in all_f_map]
            ddates = [all_f_map[0]['dep_date']] + [f['arr_date'] for f in all_f_map]
            dfnos  = [f['flight_no'] for f in all_f_map]
            xs = 18*mm; xe = W / 2
            gap = (xe - xs) / (len(dcodes) - 1) if len(dcodes) > 1 else 0
            for i, (code, date) in enumerate(zip(dcodes, ddates)):
                cx = xs + i * gap
                cv.setFillColor(BRAND); cv.circle(cx, dot_y, 3, fill=1, stroke=0)
                cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6.5)
                dw = cv.stringWidth(date, "Helvetica", 6.5)
                cv.drawString(cx - dw/2, dot_y + 5*mm, date)
                cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 8)
                cw = cv.stringWidth(code, "Helvetica-Bold", 8)
                cv.drawString(cx - cw/2, dot_y - 6*mm, code)
                if i < len(dfnos):
                    nx = xs + (i+1)*gap; mid = (cx+nx)/2
                    cv.saveState()
                    cv.setStrokeColor(GREY_LINE); cv.setLineWidth(0.8); cv.setDash([2,2],0)
                    cv.line(cx+3, dot_y, nx-3, dot_y)
                    cv.restoreState()
                    cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6.5)
                    fw = cv.stringWidth(dfnos[i], "Helvetica", 6.5)
                    cv.drawString(mid - fw/2, dot_y - 5.5*mm, dfnos[i])

            bottom_divider_y = dot_y - 12*mm
            hr(bottom_divider_y, lw=0.5)
            cy = bottom_divider_y - 5*mm - EXTRA_GAP

        else:
            if is_direct_rt:
                # Direct RT ci>0: content continues on same page, cy already set
                pass
            else:
                # Continuation chunk on new page — compact header + mini dot map
                cv.setFillColor(BRAND)
                cv.rect(0, H - T - 4*mm, W, 4*mm, fill=1, stroke=0)
                cv.setFillColor(BRAND); cv.setFont("Helvetica-Bold", 14)
                cv.drawString(MARGIN, H - T - 14*mm, data.get('airline_name', '').upper())
                cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7.5)
                cv.drawRightString(W-MARGIN, H-T-11*mm, "Continued")

                # Mini dot map for remaining flights in this chunk
                rem_codes = [flights[0]['dep_code']] + [f['arr_code'] for f in flights]
                rem_dates = [flights[0]['dep_date']] + [f['arr_date'] for f in flights]
                rem_fnos  = [f['flight_no'] for f in flights]
                dot_y2 = H - T - 30*mm
                xs2 = 18*mm; xe2 = W / 2
                gap2 = (xe2 - xs2) / (len(rem_codes) - 1) if len(rem_codes) > 1 else 0
                for i, (code, date) in enumerate(zip(rem_codes, rem_dates)):
                    cx = xs2 + i * gap2
                    cv.setFillColor(BRAND); cv.circle(cx, dot_y2, 3, fill=1, stroke=0)
                    cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6.5)
                    dw = cv.stringWidth(date, "Helvetica", 6.5)
                    cv.drawString(cx - dw/2, dot_y2 + 5*mm, date)
                    cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 8)
                    cw = cv.stringWidth(code, "Helvetica-Bold", 8)
                    cv.drawString(cx - cw/2, dot_y2 - 6*mm, code)
                    if i < len(rem_fnos):
                        nx = xs2 + (i+1)*gap2; mid = (cx+nx)/2
                        cv.saveState()
                        cv.setStrokeColor(GREY_LINE); cv.setLineWidth(0.8); cv.setDash([2,2],0)
                        cv.line(cx+3, dot_y2, nx-3, dot_y2)
                        cv.restoreState()
                        cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6.5)
                        fw = cv.stringWidth(rem_fnos[i], "Helvetica", 6.5)
                        cv.drawString(mid - fw/2, dot_y2 - 5.5*mm, rem_fnos[i])

                hr(dot_y2 - 12*mm, lw=0.5)
                cy = dot_y2 - 18*mm

        # ── Section label bar for direct RT ────────────────────────────────
        if is_direct_rt and page_label:
            lbh = 7*mm
            cv.setFillColor(colors.HexColor("#F0F0F0"))
            cv.rect(MARGIN, cy - lbh, W - 2*MARGIN, lbh, fill=1, stroke=0)
            cv.setFillColor(BRAND)
            cv.rect(MARGIN, cy - lbh, 3, lbh, fill=1, stroke=0)
            cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 8)
            cv.drawString(MARGIN + 6*mm, cy - 4.5*mm, page_label.upper())
            cy -= lbh + 3*mm

        # ── Flight cards ───────────────────────────────────────────────────
        for flight in flights:
            CH = 44*mm; CW = W - 2*MARGIN
            cv.saveState()
            cv.setStrokeColor(GREY_LINE); cv.setLineWidth(0.6)
            cv.roundRect(MARGIN, cy-CH, CW, CH, 3, fill=0, stroke=1)
            cv.restoreState()
            cv.setFillColor(GREY_LIGHT)
            cv.rect(MARGIN, cy-8*mm, CW, 8*mm, fill=1, stroke=0)

            # Flight number (left side of card header)
            cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 9)
            cv.drawString(MARGIN+4*mm, cy-5.5*mm, flight.get('flight_no',''))
            fnw = cv.stringWidth(flight.get('flight_no',''), "Helvetica-Bold", 9)

            if all_same_airline:
                # Dot + airline name text (left side, after flight no)
                cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 9)
                cv.drawString(MARGIN+4*mm+fnw+2*mm, cy-5.5*mm, "\u00b7")
                cv.setFillColor(BLACK); cv.setFont("Helvetica", 8.5)
                cv.drawString(MARGIN+4*mm+fnw+5*mm, cy-5.5*mm, flight.get('operated_by',''))
                # Cabin right-aligned
                cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 8)
                cv.drawRightString(W-MARGIN-2*mm, cy-5.5*mm, flight.get('cabin','Economy'))
            else:
                # Different airlines — text name + cabin right-aligned
                cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 9)
                cv.drawString(MARGIN+4*mm+fnw+2*mm, cy-5.5*mm, "\u00b7")
                cv.setFillColor(BLACK); cv.setFont("Helvetica", 8.5)
                cv.drawString(MARGIN+4*mm+fnw+5*mm, cy-5.5*mm, flight.get('operated_by',''))
                cv.setFont("Helvetica-Bold", 8)
                cv.drawRightString(W-MARGIN-2*mm, cy-5.5*mm, flight.get('cabin','Economy'))

            hr(cy-8*mm, x1=MARGIN, x2=MARGIN+CW)
            bt = cy-10*mm; lx = MARGIN+4*mm; infox = W*0.62

            def to12(t24):
                """Convert HH:MM 24h to 12h am/pm label."""
                try:
                    h, m = map(int, t24.split(':'))
                    suffix = 'am' if h < 12 else 'pm'
                    h12 = h % 12 or 12
                    return f"{h12}:{m:02d}{suffix}"
                except Exception:
                    return ''

            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7.5)
            cv.drawString(lx, bt-2*mm, flight.get('dep_city',''))
            acw = cv.stringWidth(flight.get('arr_city',''), "Helvetica", 7.5)
            cv.drawString(infox-2*mm-acw-10*mm, bt-2*mm, flight.get('arr_city',''))
            cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 26)
            cv.drawString(lx, bt-12*mm, flight.get('dep_code',''))
            dcw26 = cv.stringWidth(flight.get('dep_code',''), "Helvetica-Bold", 26)
            acw26 = cv.stringWidth(flight.get('arr_code',''), "Helvetica-Bold", 26)
            arr_col = infox-2*mm-acw26-10*mm
            cv.drawString(arr_col, bt-12*mm, flight.get('arr_code',''))

            # Departure time (bold) + 12hr in grey
            dep_t = flight.get('dep_time','')
            arr_t = flight.get('arr_time','')
            cv.setFillColor(GREY_DARK); cv.setFont("Helvetica-Bold", 9)
            cv.drawString(lx, bt-16*mm, dep_t)
            dep_tw = cv.stringWidth(dep_t, "Helvetica-Bold", 9)
            cv.drawString(arr_col, bt-16*mm, arr_t)
            arr_tw = cv.stringWidth(arr_t, "Helvetica-Bold", 9)
            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6.5)
            cv.drawString(lx + dep_tw + 1.5*mm, bt-15.5*mm, f"({to12(dep_t)})")
            cv.drawString(arr_col + arr_tw + 1.5*mm, bt-15.5*mm, f"({to12(arr_t)})")

            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7.5)
            cv.drawString(lx, bt-20*mm, flight.get('dep_date',''))
            cv.drawString(arr_col, bt-20*mm, flight.get('arr_date',''))
            cv.setFont("Helvetica", 7)
            cv.drawString(lx, bt-24*mm, flight.get('dep_airport',''))
            cv.drawString(arr_col, bt-24*mm, flight.get('arr_airport',''))
            if flight.get('dep_terminal'): cv.drawString(lx, bt-27.5*mm, flight['dep_terminal'])
            if flight.get('arr_terminal'): cv.drawString(arr_col, bt-27.5*mm, flight['arr_terminal'])
            dep_end = lx+dcw26+3*mm; arr_start = arr_col-3*mm; arrow_y = bt-9*mm
            cv.saveState()
            cv.setStrokeColor(GREY_LINE); cv.setLineWidth(0.7); cv.setDash([2,2],0)
            cv.line(dep_end, arrow_y, arr_start-3, arrow_y)
            cv.restoreState()
            cv.setFillColor(GREY_MID)
            p = cv.beginPath()
            p.moveTo(arr_start, arrow_y); p.lineTo(arr_start-4, arrow_y+2); p.lineTo(arr_start-4, arrow_y-2); p.close()
            cv.drawPath(p, fill=1, stroke=0)

            # ── Technical stopover dot on arrow ────────────────────────────
            if flight.get('stopover'):
                sv = flight['stopover']
                mid_x = (dep_end + arr_start) / 2
                # Blue dot on the arrow line
                cv.setFillColor(colors.HexColor("#2471A3"))
                cv.circle(mid_x, arrow_y, 2.5, fill=1, stroke=0)
                # Duration above dot
                sv_dur = sv.get('duration','')
                if sv_dur:
                    cv.setFillColor(colors.HexColor("#2471A3"))
                    cv.setFont("Helvetica", 5.5)
                    dw2 = cv.stringWidth(sv_dur, "Helvetica", 5.5)
                    cv.drawString(mid_x - dw2/2, arrow_y + 2*mm, sv_dur)
                # IATA code below dot
                sv_code = sv.get('code') or sv.get('city','')[:3].upper()
                cv.setFillColor(colors.HexColor("#2471A3"))
                cv.setFont("Helvetica-Bold", 6)
                sw2 = cv.stringWidth(sv_code, "Helvetica-Bold", 6)
                cv.drawString(mid_x - sw2/2, arrow_y - 3.5*mm, sv_code)
                # "Technical Stop" label below IATA code
                ts_lbl = "Technical Stop"
                cv.setFont("Helvetica", 5)
                tw = cv.stringWidth(ts_lbl, "Helvetica", 5)
                cv.drawString(mid_x - tw/2, arrow_y - 6.5*mm, ts_lbl)
            rows = [("Seat",     flight.get('seat','-')),
                    ("Carry-on", flight.get('carryon','-')),
                    ("Checked",  flight.get('checked','-'))]
            if flight.get('meal'):
                rows.append(("Meal", "Confirmed"))
            ry2 = bt-2*mm
            for lbl2, val2 in rows:
                cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7); cv.drawString(infox, ry2, lbl2)
                # Meal "Confirmed" in green
                if lbl2 == "Meal":
                    cv.setFillColor(colors.HexColor("#1E8449")); cv.setFont("Helvetica-Bold", 7.5)
                else:
                    cv.setFillColor(BLACK); cv.setFont("Helvetica", 7.5)
                cv.drawString(infox+18*mm, ry2, val2)
                ry2 -= 5*mm
            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7); cv.drawString(infox, ry2, "Status")
            cv.setFillColor(BRAND); cv.setFont("Helvetica-Bold", 7.5); cv.drawString(infox+18*mm, ry2, flight.get('status','CONFIRMED'))

            # ── Right side: logo + airline name + bordered flight number ───
            if all_same_airline and logo_bytes:
                try:
                    from reportlab.lib.utils import ImageReader
                    import io as _io

                    rx_area_x = infox + 32*mm          # start of right empty area
                    rx_area_w = W - MARGIN - 2*mm - rx_area_x
                    cx_area   = rx_area_x + rx_area_w / 2  # centre of area

                    # ── Logo (fit to available width, cap height at 14mm) ──
                    ir     = ImageReader(_io.BytesIO(logo_bytes))
                    iw, ih = ir.getSize()
                    # Scale to fill available width first
                    lg_w   = rx_area_w
                    lg_h   = ih * (lg_w / iw)
                    # Cap height at 14mm
                    if lg_h > 14*mm:
                        lg_h = 14*mm
                        lg_w = iw * (lg_h / ih)
                    lg_x = cx_area - lg_w / 2
                    lg_y = bt - 3*mm - lg_h
                    cv.drawImage(ImageReader(_io.BytesIO(logo_bytes)),
                                 lg_x, lg_y, width=lg_w, height=lg_h,
                                 preserveAspectRatio=True, mask='auto')

                    # ── Airline name (centred, below logo) ──
                    al_y = lg_y - 4*mm
                    al_text = shorten_airline(flight.get('operated_by', ''))
                    cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6.5)
                    al_w = cv.stringWidth(al_text, "Helvetica", 6.5)
                    # Trim to fit if still too wide
                    while al_w > rx_area_w and len(al_text) > 3:
                        al_text = al_text[:-1]
                        al_w = cv.stringWidth(al_text, "Helvetica", 6.5)
                    cv.drawString(cx_area - al_w/2, al_y, al_text)

                    # ── Bordered box for flight number only ──
                    fn_text = flight.get('flight_no', '')
                    cv.setFont("Helvetica-Bold", 11)
                    fn_tw   = cv.stringWidth(fn_text, "Helvetica-Bold", 11)
                    fn_pad_x = 4*mm
                    fn_pad_y = 2*mm
                    fn_bw   = fn_tw + fn_pad_x * 2
                    fn_bh   = 7*mm
                    fn_bx   = cx_area - fn_bw / 2
                    fn_by   = al_y - 3*mm - fn_bh

                    # Box with brand colour border
                    cv.saveState()
                    cv.setStrokeColor(BRAND)
                    cv.setLineWidth(1.2)
                    cv.roundRect(fn_bx, fn_by, fn_bw, fn_bh, 2, fill=0, stroke=1)
                    cv.restoreState()

                    # Flight number text centred in box
                    cv.setFillColor(BRAND)
                    cv.setFont("Helvetica-Bold", 11)
                    cv.drawString(fn_bx + fn_pad_x, fn_by + (fn_bh - 4*mm)/2 + 1*mm, fn_text)

                except Exception:
                    pass
            cy -= CH + 3*mm
            if flight.get('transit'):
                tr = flight['transit']
                checked2 = tr.get('baggage_status') == 'checked_through'
                bcol = colors.HexColor("#1E8449" if checked2 else "#CA6F1E")
                sh = 9*mm; sw = W-2*MARGIN; ssy = cy-TGAP/2-sh/2
                cv.setFillColor(colors.HexColor("#F7F7F7"))
                cv.roundRect(MARGIN, ssy, sw, sh, 2, fill=1, stroke=0)
                cv.setFillColor(BRAND); cv.rect(MARGIN, ssy, 2, sh, fill=1, stroke=0)
                tyl = ssy+sh-3.2*mm; tyv = ssy+1.8*mm
                c1 = MARGIN+5*mm; c2 = MARGIN+sw*0.38; c3 = MARGIN+sw*0.68
                cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6)
                cv.drawString(c1, tyl, "LAYOVER"); cv.drawString(c2, tyl, "TRANSIT AT"); cv.drawString(c3, tyl, "BAGGAGE")
                cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 8.5); cv.drawString(c1, tyv, tr.get('duration',''))
                cv.setFont("Helvetica", 7.5); cv.drawString(c2, tyv, tr.get('airport',''))
                cv.setFillColor(bcol); cv.setFont("Helvetica-Bold", 7.5)
                cv.drawString(c3, tyv, "Checked through" if checked2 else "Reclaim & re-check")
                cy -= TGAP

        # ── Baggage + footer only on last chunk of last journey ─────────────
        is_last_overall = (ci == total_chunks - 1)
        if (not is_direct_rt and is_last_c and is_last_overall) or (is_direct_rt and ci == total_chunks - 1):
            all_f_bag = [f for p in pages for f in p.get('flights',[])]
            brows = [(f"{f.get('dep_city','')} -> {f.get('arr_city','')} ({f.get('flight_no','')})",
                      f"Carry-on: {f.get('carryon','-')}  |  Checked: {f.get('checked','-')}")
                     for f in all_f_bag]

            use_2col = len(brows) > 2
            ROW_H    = 9.5*mm
            HDR_H    = 7*mm
            PAD      = 4*mm

            if use_2col:
                # Split rows into left and right columns
                half     = (len(brows) + 1) // 2
                left_r   = brows[:half]
                right_r  = brows[half:]
                col_rows = max(len(left_r), len(right_r))
                box_h    = HDR_H + col_rows * ROW_H + PAD
            else:
                box_h    = HDR_H + len(brows) * ROW_H + PAD

            # Stick baggage box right below last flight card
            bag_y = cy - 3*mm
            # Safety clamp — never overlap footer
            footer_safe = MBOTTOM + 10*mm
            if bag_y - box_h < footer_safe:
                bag_y = footer_safe + box_h

            bw = W - 2*MARGIN
            cv.setFillColor(colors.HexColor("#FDFBF3"))
            cv.setStrokeColor(colors.HexColor("#E8D98A")); cv.setLineWidth(0.5)
            cv.roundRect(MARGIN, bag_y - box_h, bw, box_h, 3, fill=1, stroke=1)
            cv.setFillColor(BRAND); cv.setFont("Helvetica-Bold", 8)
            cv.drawString(MARGIN + 4*mm, bag_y - 5*mm, "BAGGAGE ALLOWANCE")

            if use_2col:
                col_w  = bw / 2
                # Thin vertical divider between columns
                div_x  = MARGIN + col_w
                cv.saveState()
                cv.setStrokeColor(colors.HexColor("#E8D98A")); cv.setLineWidth(0.4)
                cv.line(div_x, bag_y - HDR_H, div_x, bag_y - box_h + 2*mm)
                cv.restoreState()

                def draw_col(rows, x_start):
                    sy = bag_y - HDR_H - 2*mm
                    for seg_t, seg_d in rows:
                        cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 7)
                        cv.drawString(x_start + 4*mm, sy, seg_t); sy -= 4*mm
                        cv.setFillColor(GREY_DARK); cv.setFont("Helvetica", 6.5)
                        cv.drawString(x_start + 4*mm, sy, seg_d); sy -= 5.5*mm

                draw_col(left_r,  MARGIN)
                draw_col(right_r, MARGIN + col_w)
            else:
                sy = bag_y - HDR_H - 2*mm
                for seg_t, seg_d in brows:
                    cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 7)
                    cv.drawString(MARGIN + 4*mm, sy, seg_t); sy -= 4*mm
                    cv.setFillColor(GREY_DARK); cv.setFont("Helvetica", 6.5)
                    cv.drawString(MARGIN + 4*mm, sy, seg_d); sy -= 5.5*mm

        # Footer on every chunk
        # ── Policy notice above footer ─────────────────────────────────────
        policy = data.get('ticket_policy','')
        POLICY_TEXTS = {
            'non_refundable': '⚠  This ticket is non-refundable. No refunds will be issued for cancellations or no-shows.',
            'change_24h':     'ℹ  Changes to this ticket must be requested at least 24 hours before departure. Fees may apply.',
            'change_48h':     'ℹ  Changes to this ticket must be requested at least 48 hours before departure. Fees may apply.',
        }
        if policy and policy in POLICY_TEXTS:
            notice_text = POLICY_TEXTS[policy]
            notice_col  = colors.HexColor("#7D3C00") if policy == 'non_refundable' else colors.HexColor("#1A5276")
            bg_col      = colors.HexColor("#FEF9E7") if policy == 'non_refundable' else colors.HexColor("#EBF5FB")
            notice_y    = MBOTTOM + 14*mm
            notice_h    = 7*mm
            cv.setFillColor(bg_col)
            cv.roundRect(MARGIN, notice_y - notice_h + 2*mm, W - 2*MARGIN, notice_h, 2, fill=1, stroke=0)
            cv.saveState()
            cv.setStrokeColor(notice_col); cv.setLineWidth(0.4)
            cv.roundRect(MARGIN, notice_y - notice_h + 2*mm, W - 2*MARGIN, notice_h, 2, fill=0, stroke=1)
            cv.restoreState()
            cv.setFillColor(notice_col); cv.setFont("Helvetica", 7)
            cv.drawString(MARGIN + 3*mm, notice_y - 3.5*mm, notice_text)

        hr(MBOTTOM+5*mm)
        cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7)
        cv.drawString(MARGIN, MBOTTOM+1.5*mm, "All times are local to each city")
        if is_direct_rt:
            pg = "Page 1 of 1"
        else:
            pg = f"Page {ci+1} of {total_chunks}"
        cv.drawString(W-MARGIN-cv.stringWidth(pg,"Helvetica",7), MBOTTOM+1.5*mm, pg)


    cv.showPage()
    cv.save()
    return output_path


@app.route('/')
def index():
    return render_template('index.html')


@app.route('/generate', methods=['POST'])
def generate():
    if not GROQ_API_KEY:
        return jsonify({'error': 'GROQ_API_KEY not configured on server'}), 500

    files = request.files.getlist('pdfs')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No PDF files uploaded'}), 400

    overrides = {}
    for field in ['booking_ref', 'airline_name', 'brand_hex']:
        val = request.form.get(field, '').strip()
        if val:
            overrides[field] = val

    # Read uploaded logo if provided
    logo_bytes = None
    logo_ext   = None
    logo_file  = request.files.get('logo')
    if logo_file and logo_file.filename:
        logo_bytes = logo_file.read()
        logo_ext   = logo_file.filename.rsplit('.', 1)[-1].lower()

    # Read new fields
    checked_baggage = request.form.get('checked_baggage','').strip()
    meal_included   = request.form.get('meal_included','') == '1'
    ticket_policy   = request.form.get('ticket_policy','').strip()

    # Manual passenger override (from form)
    manual_pax_name   = request.form.get('passenger_name','').strip()
    manual_pax_title  = request.form.get('title','').strip()
    manual_pax_ticket = request.form.get('ticket_number','').strip()

    try:
        pdf_bytes_list = [f.read() for f in files]
        data = extract_with_groq(pdf_bytes_list)
        data.update(overrides)

        # Normalise: support both old single-pax and new multi-pax structure
        if 'passengers' not in data:
            # Old format: wrap in passengers list
            data['passengers'] = [{
                'passenger_name': data.get('passenger_name', 'PASSENGER'),
                'title':          data.get('title', ''),
                'ticket_number':  data.get('ticket_number', ''),
            }]

        # Manual override: if user filled in name/ticket, override first (or only) passenger
        if manual_pax_name:
            data['passengers'][0]['passenger_name'] = manual_pax_name
        if manual_pax_title:
            data['passengers'][0]['title'] = manual_pax_title
        if manual_pax_ticket:
            data['passengers'][0]['ticket_number'] = manual_pax_ticket

        # Shorten/standardise airline name
        data['airline_name'] = shorten_airline(data.get('airline_name',''))

        # Store policy for PDF renderer
        data['ticket_policy'] = ticket_policy

        # Pick best booking ref
        if 'booking_ref' not in overrides:
            data['booking_ref'] = pick_airline_ref(data)

        # Auto brand colour
        if not data.get('brand_hex') or data['brand_hex'] in ('#1A1A1A','#000000',''):
            al = data.get('airline_name', '').lower()
            for key, hx in AIRLINE_BRANDS.items():
                if key in al:
                    data['brand_hex'] = hx
                    break

        # Apply per-flight rules
        carryon = get_carryon(data.get('airline_name', ''))
        for page in data.get('pages', []):
            for flight in page.get('flights', []):
                flight['carryon'] = carryon
                if checked_baggage:
                    flight['checked'] = checked_baggage
                flight['meal'] = meal_included
                if flight.get('status','').upper() == 'HK':
                    flight['status'] = 'CONFIRMED'

        passengers = data.get('passengers', [])
        pnr = re.sub(r'[^A-Z0-9]', '', data.get('booking_ref','').upper())

        # Single passenger → return single PDF directly
        if len(passengers) == 1:
            pax = passengers[0]
            pax_data = {**data,
                        'passenger_name': pax.get('passenger_name','PASSENGER'),
                        'title':          pax.get('title',''),
                        'ticket_number':  pax.get('ticket_number','')}
            pdf_path  = generate_eticket_pdf(pax_data, logo_bytes=logo_bytes, logo_ext=logo_ext)
            firstname = pax.get('passenger_name','PASSENGER').strip().upper().split()[0]
            filename  = f"{pnr}_{firstname}.pdf"
            return send_file(pdf_path, as_attachment=True,
                             download_name=filename, mimetype='application/pdf')

        # Multiple passengers → generate one PDF each and zip them
        import zipfile, tempfile
        zip_tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.zip')
        zip_tmp.close()
        with zipfile.ZipFile(zip_tmp.name, 'w', zipfile.ZIP_DEFLATED) as zf:
            for pax in passengers:
                pax_data = {**data,
                            'passenger_name': pax.get('passenger_name','PASSENGER'),
                            'title':          pax.get('title',''),
                            'ticket_number':  pax.get('ticket_number','')}
                pdf_path  = generate_eticket_pdf(pax_data, logo_bytes=logo_bytes, logo_ext=logo_ext)
                firstname = pax.get('passenger_name','PASSENGER').strip().upper().split()[0]
                pdf_name  = f"{pnr}_{firstname}.pdf"
                zf.write(pdf_path, pdf_name)

        zip_filename = f"{pnr}_tickets.zip"
        return send_file(zip_tmp.name, as_attachment=True,
                         download_name=zip_filename, mimetype='application/zip')

    except json.JSONDecodeError as e:
        return jsonify({'error': f'Could not parse flight data: {str(e)}'}), 422
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/health')
def health():
    return jsonify({'status': 'ok', 'model': 'llama-3.3-70b-versatile (groq)'})


if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)
