"""PDF + CSV rendering for a stored report snapshot (a plain dict; no DB access here)."""
import csv, io
from xml.sax.saxutils import escape
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

DISCLAIMER = ("Research use only. Results are machine-learning predictions from a small research cohort; "
              "they are not experimentally validated outcomes, not a diagnosis, and not medical or "
              "dietary advice. The glucose value comes from an experimental DIY sensor that is not "
              "clinically validated unless stated otherwise.")
LIMITATIONS = [
    "Models are trained on ~100 cohort samples and score enzymes by classification probability; "
    "scores are relative rankings, not effect sizes.",
    "Compound pIC50 values are lookups from a curated table, not predictions for this person.",
    "Only 20 species are used; the model assumes percent relative abundance of the whole community.",
    "Glucose is used as fasting blood glucose (FBG) by the models; non-fasting values are not equivalent.",
]


def _p(t, st):
    return Paragraph(escape(str(t)), st)


def _table(rows, widths, st):
    t = Table([[_p(c, st) for c in r] for r in rows], colWidths=widths, repeatRows=1)
    t.setStyle(TableStyle([("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#DCF0DE")),
                           ("GRID", (0, 0), (-1, -1), 0.4, colors.HexColor("#B0C8B2")),
                           ("VALIGN", (0, 0), (-1, -1), "TOP")]))
    return t


def _recs(items, kind, st):
    if not items:
        return [_p("No predictions.", st)]
    rows = [["#", "Enzyme (EC)", "Score %", "Compound", "pIC50", "Sources"]]
    for i, it in enumerate(items, 1):
        rows.append([i, f"{it.get('enzyme_name', '')} ({it.get('enzyme', '')})", it.get("prob", ""),
                     it.get("compound", "-"), it.get("pic50", "-"), ", ".join(it.get("sources", []) or [])])
    return [_table(rows, [8 * mm, 52 * mm, 16 * mm, 32 * mm, 14 * mm, 52 * mm], st)]


def report_to_pdf(r: dict) -> bytes:
    ss = getSampleStyleSheet()
    body = ss["BodyText"]; body.fontSize = 8.5; body.leading = 11
    h1, h2 = ss["Title"], ss["Heading2"]
    buf = io.BytesIO()
    doc = SimpleDocTemplate(buf, pagesize=A4, leftMargin=15 * mm, rightMargin=15 * mm,
                            topMargin=15 * mm, bottomMargin=15 * mm, title=f"Report {r['id']}")
    g = r.get("glucose", {})
    s = [_p("Gut Health Analyzer - Analysis Report", h1),
         _table([["Field", "Value"],
                 ["Report ID", r["id"]],
                 ["Analysed at (UTC)", r.get("created_at", "")],
                 ["Status", r.get("status", "")],
                 ["Glucose", f"{g.get('value', '')} {g.get('unit', 'mg/dL')}"],
                 ["Glucose source", g.get("source", "")],
                 ["Glucose measured at (UTC)", g.get("measured_at", "")],
                 ["Fasting", {True: "Yes", False: "No"}.get(g.get("fasting"), "Unknown")],
                 ["Microbiome sample ID", r.get("sample_id") or "-"],
                 ["Model version", r.get("model_version", "")]], [50 * mm, 130 * mm], body),
         Spacer(1, 6)]
    if g.get("fasting") is not True:
        s += [_p("Warning: glucose was not confirmed as fasting, but the models treat it as FBG.", body)]
    feats = r.get("features", [])
    s += [_p("Validated microbial features used", h2),
          _table([["Species", "Abundance (%)"]] + [[f["name"], f["value"]] for f in feats],
                 [110 * mm, 40 * mm], body),
          _p("Phytochemical recommendations", h2)]
    s += _recs((r.get("results") or {}).get("phyto"), "phyto", body)
    s += [_p("Food recommendations", h2)] + _recs((r.get("results") or {}).get("food"), "food", body)
    s += [_p("Limitations", h2)] + [_p("- " + l, body) for l in LIMITATIONS]
    s += [Spacer(1, 6), _p(DISCLAIMER, body)]
    doc.build(s)
    return buf.getvalue()


CSV_FIELDS = ["report_id", "created_at", "status", "glucose_value", "glucose_unit", "glucose_source",
              "glucose_measured_at", "fasting", "sample_id", "model_version", "kind", "rank",
              "enzyme_ec", "enzyme_name", "score_percent", "compound", "pic50", "sources"]


def reports_to_csv(reports) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, CSV_FIELDS)
    w.writeheader()
    for r in reports:
        g = r.get("glucose", {})
        base = {"report_id": r["id"], "created_at": r.get("created_at"), "status": r.get("status"),
                "glucose_value": g.get("value"), "glucose_unit": g.get("unit"),
                "glucose_source": g.get("source"), "glucose_measured_at": g.get("measured_at"),
                "fasting": g.get("fasting"), "sample_id": r.get("sample_id"),
                "model_version": r.get("model_version")}
        for kind in ("phyto", "food"):
            for i, it in enumerate((r.get("results") or {}).get(kind) or [], 1):
                row = dict(base, kind=kind, rank=i, enzyme_ec=it.get("enzyme"),
                           enzyme_name=it.get("enzyme_name"), score_percent=it.get("prob"),
                           compound=it.get("compound"), pic50=it.get("pic50"),
                           sources="; ".join(it.get("sources", []) or []))
                # neutralise spreadsheet formula injection
                for k, v in row.items():
                    if isinstance(v, str) and v[:1] in "=+-@":
                        row[k] = "'" + v
                w.writerow(row)
    return out.getvalue()
