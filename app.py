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
}

# Logos folder — place PNG/JPG files here named after airline
# e.g. static/logos/thai_airways.png, static/logos/qatar_airways.png
LOGOS_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'logos')

# Maps lowercase keywords in airline name -> logo filename (without extension)
AIRLINE_LOGO_MAP = {
    "thai airways":       "thai_airways",
    "srilankan":          "srilankan_airlines",
    "qatar":              "qatar_airways",
    "gulf air":           "gulf_air",
    "etihad":             "etihad_airways",
    "emirates":           "emirates",
    "singapore airlines": "singapore_airlines",
    "lufthansa":          "lufthansa",
    "british airways":    "british_airways",
    "air india":          "air_india",
    "flynas":             "flynas",
    "flydubai":           "flydubai",
    "indigo":             "indigo",
    "air arabia":         "air_arabia",
}

def find_logo(airline_name):
    """Return full path to logo file if it exists, else None."""
    al = airline_name.lower()
    for keyword, filename in AIRLINE_LOGO_MAP.items():
        if keyword in al:
            for ext in ('png', 'jpg', 'jpeg'):
                path = os.path.join(LOGOS_DIR, f"{filename}.{ext}")
                if os.path.exists(path):
                    return path
    return None


EXTRACT_PROMPT = """You are a flight data extractor. Extract all flight booking details from the text below and return ONLY a raw JSON object. No markdown, no code fences, no explanation — just the JSON.

Use this exact structure:
{
  "passenger_name": "FULL NAME IN CAPS",
  "title": "MR or MRS or MS or DR or empty string",
  "ticket_number": "ticket number as string",
  "booking_ref": "PNR / airline booking reference",
  "airline_name": "Full airline name",
  "date_of_issue": "DD Mon YYYY",
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
          "transit": null
        }
      ]
    }
  ]
}

Rules:
- If there is a layover/transfer between flights, set transit on the FIRST flight:
  {"airport": "Airport short name", "duration": "Xhr Ymins", "baggage_status": "checked_through or reclaim"}
- For round trips: use TWO pages — "Outbound Journey" and "Return Journey"
- For one-way: use ONE page with page_label as empty string
- brand_hex: Thai Airways=#7B0D1E, SriLankan Airlines=#A6192E, Qatar Airways=#5C0632,
  Gulf Air=#C8922A, Etihad Airways=#BD8B13, Emirates=#CC0000, Singapore Airlines=#003B6F,
  Lufthansa=#05164D, British Airways=#075AAA, Air India=#E31837, default=#1A1A1A
- Keep airport names short (max 30 chars)
- All times in 24hr HH:MM format

ITINERARY TEXT:
"""


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


def generate_eticket_pdf(data):
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix='.pdf')
    output_path = tmp.name
    tmp.close()

    W, H   = A4
    MARGIN  = 14 * mm
    MTOP    = 0.5 * 25.4 * mm
    MBOTTOM = 0.5 * 25.4 * mm
    TGAP    = 16 * mm

    BRAND      = colors.HexColor(data.get('brand_hex', '#1A1A1A'))
    BLACK      = colors.HexColor("#1A1A1A")
    GREY_DARK  = colors.HexColor("#222222")
    GREY_MID   = colors.HexColor("#4E4E4E")
    GREY_LIGHT = colors.HexColor("#C1C1C1")
    GREY_LINE  = colors.HexColor("#9B9B9B")

    pages       = data.get('pages', [])
    total_pages = len(pages)
    cv          = canvas.Canvas(output_path, pagesize=A4)
    cv.setTitle(f"E-Ticket - {data.get('passenger_name','')}")
    cv.setAuthor('Prime Lanka Tours')

    def hr(y, x1=None, x2=None, color=None, lw=0.4):
        cv.saveState()
        cv.setStrokeColor(color or GREY_LINE)
        cv.setLineWidth(lw)
        cv.line(x1 or MARGIN, y, x2 or W - MARGIN, y)
        cv.restoreState()

    for pi, page in enumerate(pages):
        if pi > 0:
            cv.showPage()

        flights    = page.get('flights', [])
        page_label = page.get('page_label', '')
        T          = MTOP

        # Brand bar
        cv.setFillColor(BRAND)
        cv.rect(0, H - T - 2*mm, W, 2*mm, fill=1, stroke=0)

        # Airline name (left)
        cv.setFillColor(BRAND)
        cv.setFont("Helvetica-Bold", 18)
        cv.drawString(MARGIN, H - T - 14*mm, data.get('airline_name', '').upper())

        # Logo + "Electronic ticket receipt" label (top right)
        logo_path = find_logo(data.get('airline_name', ''))
        LOGO_H    = 10 * mm   # ~3-4 text lines tall
        LOGO_MAX_W = 38 * mm  # max width — keeps proportions
        logo_y    = H - T - 3*mm   # top of logo (just below brand bar)

        if logo_path:
            try:
                from PIL import Image as PILImage
                img = PILImage.open(logo_path)
                iw, ih = img.size
                # Calculate width to maintain aspect ratio at LOGO_H height
                scale   = LOGO_H / (ih * 0.352778)   # px -> mm via 72dpi factor
                logo_w  = iw * 0.352778 * scale
                if logo_w > LOGO_MAX_W:              # cap max width
                    scale  = LOGO_MAX_W / (iw * 0.352778)
                    logo_w = LOGO_MAX_W
                    logo_h_actual = ih * 0.352778 * scale
                else:
                    logo_h_actual = LOGO_H
                logo_x = W - MARGIN - logo_w
                cv.drawImage(logo_path, logo_x, logo_y - logo_h_actual,
                             width=logo_w, height=logo_h_actual,
                             preserveAspectRatio=True, mask='auto')
                # "Electronic ticket receipt" below logo
                cv.setFillColor(GREY_MID)
                cv.setFont("Helvetica", 7.5)
                lbl = "Electronic ticket receipt"
                lw  = cv.stringWidth(lbl, "Helvetica", 7.5)
                cv.drawString(W - MARGIN - lw, logo_y - logo_h_actual - 3*mm, lbl)
            except Exception:
                # Fallback if PIL not available or image error
                cv.setFillColor(GREY_MID)
                cv.setFont("Helvetica", 8)
                lbl = "Electronic ticket receipt"
                cv.drawString(W - MARGIN - cv.stringWidth(lbl, "Helvetica", 8), H - T - 11*mm, lbl)
        else:
            # No logo — show label only
            cv.setFillColor(GREY_MID)
            cv.setFont("Helvetica", 8)
            lbl = "Electronic ticket receipt"
            cv.drawString(W - MARGIN - cv.stringWidth(lbl, "Helvetica", 8), H - T - 11*mm, lbl)

        hr(H - T - 18*mm, lw=0.6)

        # Passenger name
        title = data.get('title', '')
        pax   = ((title + ' ') if title else '') + data.get('passenger_name', '')
        cv.setFillColor(BLACK)
        cv.setFont("Helvetica-Bold", 13)
        cv.drawString(MARGIN, H - T - 27*mm, pax.strip())

        ty_off = 4*mm if (total_pages > 1 and page_label) else 0
        if page_label and total_pages > 1:
            cv.setFillColor(BRAND)
            cv.setFont("Helvetica-Bold", 8)
            cv.drawString(MARGIN, H - T - 32*mm, page_label.upper())

        # Right column
        rx = W - MARGIN
        cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7.5)
        cv.drawRightString(rx, H - T - 22*mm, f"{data.get('airline_name','')} reference")
        cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 9)
        cv.drawRightString(rx, H - T - 27.5*mm, data.get('booking_ref', ''))
        cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7.5)
        cv.drawRightString(rx, H - T - 33*mm, "Ticket number")
        cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 8)
        cv.drawRightString(rx, H - T - 37.5*mm, data.get('ticket_number', ''))
        cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7)
        cv.drawRightString(rx, H - T - 43*mm, f"Date of issue  {data.get('date_of_issue','')}")

        cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 8)
        cv.drawString(MARGIN, H - T - 36*mm - ty_off, "Thank you for your booking.")
        cv.drawString(MARGIN, H - T - 41*mm - ty_off, "We look forward to welcoming you soon.")

        # Journey dots
        dot_y = H - T - 58*mm - ty_off
        codes = [flights[0]['dep_code']] + [f['arr_code'] for f in flights]
        dates = [flights[0]['dep_date']] + [f['arr_date'] for f in flights]
        fnos  = [f['flight_no'] for f in flights]
        xs = 18*mm; xe = W / 2
        gap = (xe - xs) / (len(codes) - 1) if len(codes) > 1 else 0

        for i, (code, date) in enumerate(zip(codes, dates)):
            cx = xs + i * gap
            cv.setFillColor(BRAND); cv.circle(cx, dot_y, 3, fill=1, stroke=0)
            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6.5)
            dw = cv.stringWidth(date, "Helvetica", 6.5)
            cv.drawString(cx - dw/2, dot_y + 5*mm, date)
            cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 8)
            cw = cv.stringWidth(code, "Helvetica-Bold", 8)
            cv.drawString(cx - cw/2, dot_y - 6*mm, code)
            if i < len(fnos):
                nx = xs + (i+1)*gap; mid = (cx+nx)/2
                cv.saveState()
                cv.setStrokeColor(GREY_LINE); cv.setLineWidth(0.8); cv.setDash([2,2],0)
                cv.line(cx+3, dot_y, nx-3, dot_y)
                cv.restoreState()
                cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6.5)
                fw = cv.stringWidth(fnos[i], "Helvetica", 6.5)
                cv.drawString(mid - fw/2, dot_y - 5.5*mm, fnos[i])

        hr(H - T - 67*mm - ty_off, lw=0.5)

        # Flight cards
        cy = H - MTOP - 76*mm - ty_off

        for flight in flights:
            CH = 44*mm; CW = W - 2*MARGIN

            cv.saveState()
            cv.setStrokeColor(GREY_LINE); cv.setLineWidth(0.6)
            cv.roundRect(MARGIN, cy-CH, CW, CH, 3, fill=0, stroke=1)
            cv.restoreState()

            cv.setFillColor(GREY_LIGHT)
            cv.rect(MARGIN, cy-8*mm, CW, 8*mm, fill=1, stroke=0)

            cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 9)
            cv.drawString(MARGIN+4*mm, cy-5.5*mm, flight.get('flight_no',''))
            fnw = cv.stringWidth(flight.get('flight_no',''), "Helvetica-Bold", 9)
            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 9)
            cv.drawString(MARGIN+4*mm+fnw+2*mm, cy-5.5*mm, "\u00b7")
            cv.setFillColor(BLACK); cv.setFont("Helvetica", 8.5)
            cv.drawString(MARGIN+4*mm+fnw+5*mm, cy-5.5*mm, flight.get('operated_by',''))
            cv.setFont("Helvetica-Bold", 8)
            cv.drawRightString(W-MARGIN-2*mm, cy-5.5*mm, flight.get('cabin','Economy'))
            hr(cy-8*mm, x1=MARGIN, x2=MARGIN+CW)

            bt = cy-10*mm; lx = MARGIN+4*mm; infox = W*0.62

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

            cv.setFillColor(GREY_DARK); cv.setFont("Helvetica-Bold", 9)
            cv.drawString(lx, bt-16*mm, flight.get('dep_time',''))
            cv.drawString(arr_col, bt-16*mm, flight.get('arr_time',''))
            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7.5)
            cv.drawString(lx, bt-20*mm, flight.get('dep_date',''))
            cv.drawString(arr_col, bt-20*mm, flight.get('arr_date',''))
            cv.setFont("Helvetica", 7)
            cv.drawString(lx, bt-24*mm, flight.get('dep_airport',''))
            cv.drawString(arr_col, bt-24*mm, flight.get('arr_airport',''))
            if flight.get('dep_terminal'):
                cv.drawString(lx, bt-27.5*mm, flight['dep_terminal'])
            if flight.get('arr_terminal'):
                cv.drawString(arr_col, bt-27.5*mm, flight['arr_terminal'])

            dep_end = lx+dcw26+3*mm; arr_start = arr_col-3*mm; arrow_y = bt-9*mm
            cv.saveState()
            cv.setStrokeColor(GREY_LINE); cv.setLineWidth(0.7); cv.setDash([2,2],0)
            cv.line(dep_end, arrow_y, arr_start-3, arrow_y)
            cv.restoreState()
            cv.setFillColor(GREY_MID)
            p = cv.beginPath()
            p.moveTo(arr_start, arrow_y)
            p.lineTo(arr_start-4, arrow_y+2)
            p.lineTo(arr_start-4, arrow_y-2)
            p.close()
            cv.drawPath(p, fill=1, stroke=0)

            rows = [("Fare type", flight.get('fare_type','-')),
                    ("Seat",      flight.get('seat','-')),
                    ("Carry-on",  flight.get('carryon','-')),
                    ("Checked",   flight.get('checked','-'))]
            ry = bt-2*mm
            for lbl, val in rows:
                cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7)
                cv.drawString(infox, ry, lbl)
                cv.setFillColor(BLACK); cv.setFont("Helvetica", 7.5)
                cv.drawString(infox+18*mm, ry, val)
                ry -= 5*mm
            cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7)
            cv.drawString(infox, ry, "Status")
            cv.setFillColor(BRAND); cv.setFont("Helvetica-Bold", 7.5)
            cv.drawString(infox+18*mm, ry, flight.get('status','CONFIRMED'))

            cy -= CH + 3*mm

            if flight.get('transit'):
                tr = flight['transit']
                checked = tr.get('baggage_status') == 'checked_through'
                bcol = colors.HexColor("#1E8449" if checked else "#CA6F1E")
                sh = 9*mm; sw = W-2*MARGIN; sy = cy-TGAP/2-sh/2
                cv.setFillColor(colors.HexColor("#F7F7F7"))
                cv.roundRect(MARGIN, sy, sw, sh, 2, fill=1, stroke=0)
                cv.setFillColor(BRAND); cv.rect(MARGIN, sy, 2, sh, fill=1, stroke=0)
                tyl = sy+sh-3.2*mm; tyv = sy+1.8*mm
                c1 = MARGIN+5*mm; c2 = MARGIN+sw*0.38; c3 = MARGIN+sw*0.68
                cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 6)
                cv.drawString(c1, tyl, "LAYOVER")
                cv.drawString(c2, tyl, "TRANSIT AT")
                cv.drawString(c3, tyl, "BAGGAGE")
                cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 8.5)
                cv.drawString(c1, tyv, tr.get('duration',''))
                cv.setFont("Helvetica", 7.5)
                cv.drawString(c2, tyv, tr.get('airport',''))
                cv.setFillColor(bcol); cv.setFont("Helvetica-Bold", 7.5)
                cv.drawString(c3, tyv, "Checked through" if checked else "Reclaim & re-check")
                cy -= TGAP

        # Baggage note
        bag_y = cy-3*mm
        brows = [(f"{f.get('dep_city','')} -> {f.get('arr_city','')} ({f.get('flight_no','')})",
                  f"Carry-on: {f.get('carryon','-')}  |  Checked: {f.get('checked','-')}")
                 for f in flights]
        box_h = 7*mm + len(brows)*9.5*mm + 4*mm
        cv.setFillColor(colors.HexColor("#FDFBF3"))
        cv.setStrokeColor(colors.HexColor("#E8D98A")); cv.setLineWidth(0.5)
        cv.roundRect(MARGIN, bag_y-box_h, W-2*MARGIN, box_h, 3, fill=1, stroke=1)
        cv.setFillColor(BRAND); cv.setFont("Helvetica-Bold", 8)
        cv.drawString(MARGIN+4*mm, bag_y-5*mm, "BAGGAGE ALLOWANCE")
        sy = bag_y-7*mm-2*mm
        for seg_t, seg_d in brows:
            cv.setFillColor(BLACK); cv.setFont("Helvetica-Bold", 7)
            cv.drawString(MARGIN+4*mm, sy, seg_t); sy -= 4*mm
            cv.setFillColor(GREY_DARK); cv.setFont("Helvetica", 6.5)
            cv.drawString(MARGIN+4*mm, sy, seg_d); sy -= 5.5*mm

        # Footer
        hr(MBOTTOM+5*mm)
        cv.setFillColor(GREY_MID); cv.setFont("Helvetica", 7)
        cv.drawString(MARGIN, MBOTTOM+1.5*mm, "All times are local to each city")
        pg = f"Page {pi+1} of {total_pages}"
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
    for field in ['passenger_name','title','ticket_number','booking_ref',
                  'airline_name','date_of_issue','brand_hex']:
        val = request.form.get(field, '').strip()
        if val:
            overrides[field] = val

    try:
        pdf_bytes_list = [f.read() for f in files]
        data = extract_with_groq(pdf_bytes_list)
        data.update(overrides)

        if not data.get('brand_hex') or data['brand_hex'] in ('#1A1A1A','#000000',''):
            al = data.get('airline_name', '').lower()
            for key, hx in AIRLINE_BRANDS.items():
                if key in al:
                    data['brand_hex'] = hx
                    break

        pdf_path = generate_eticket_pdf(data)
        pax      = re.sub(r'\s+', '_', data.get('passenger_name','PASSENGER')).upper()
        tkt      = re.sub(r'[^0-9]', '', data.get('ticket_number',''))
        filename = f"eticket_{tkt}_{pax}.pdf"

        return send_file(pdf_path, as_attachment=True,
                         download_name=filename, mimetype='application/pdf')

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
