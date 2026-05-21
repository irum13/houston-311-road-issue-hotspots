from __future__ import annotations

import csv
import json
import math
from pathlib import Path
from xml.sax.saxutils import escape

import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT / "data" / "raw"
PROCESSED_DIR = ROOT / "data" / "processed"
FIGURES_DIR = ROOT / "figures"

RAW_FILES = [
    RAW_DIR / "houston_311_2025_piped.txt",
    RAW_DIR / "houston_311_2026_ytd_piped.txt",
]

USECOLS = [
    "Case Number",
    "Incident Address",
    "Latitude",
    "Longitude",
    "Status",
    "Created Date Local",
    "Closed Date",
    "Incident Case Type",
    "Department",
    "Division",
    "Zip Code",
    "Customer SuperNeighborhood",
    "Channel",
]

ISSUE_TYPES = {"Flooding": "Flooding", "Pothole": "Pothole"}
STUDY_START = pd.Timestamp("2025-01-01")


def read_311_files() -> pd.DataFrame:
    frames = []
    raw_rows = 0

    for path in RAW_FILES:
        if not path.exists():
            raise FileNotFoundError(
                f"Missing raw file: {path}. Download it from the City of Houston 311 data page."
            )

        chunks = pd.read_csv(
            path,
            sep="|",
            skiprows=5,
            usecols=USECOLS,
            dtype=str,
            encoding="latin1",
            engine="python",
            quoting=csv.QUOTE_NONE,
            on_bad_lines="skip",
            chunksize=75_000,
        )

        for chunk in chunks:
            raw_rows += len(chunk)
            mask = chunk["Incident Case Type"].isin(ISSUE_TYPES)
            frames.append(chunk.loc[mask].copy())

    data = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=USECOLS)
    data.attrs["raw_rows"] = raw_rows
    return data


def clean_data(df: pd.DataFrame) -> pd.DataFrame:
    cleaned = df.copy()
    cleaned["issue_type"] = cleaned["Incident Case Type"].map(ISSUE_TYPES)
    cleaned["created_at"] = pd.to_datetime(cleaned["Created Date Local"], errors="coerce")
    cleaned["year"] = cleaned["created_at"].dt.year
    cleaned["month"] = cleaned["created_at"].dt.to_period("M").astype(str)
    cleaned["month_name"] = cleaned["created_at"].dt.strftime("%b %Y")
    cleaned["latitude"] = pd.to_numeric(cleaned["Latitude"], errors="coerce")
    cleaned["longitude"] = pd.to_numeric(cleaned["Longitude"], errors="coerce")
    cleaned["zip_code"] = (
        cleaned["Zip Code"]
        .fillna("")
        .astype(str)
        .str.extract(r"(\d{5})", expand=False)
        .fillna("Unknown")
    )

    cleaned = cleaned.dropna(subset=["created_at"])
    cleaned = cleaned[cleaned["created_at"] >= STUDY_START].copy()
    before_dedupe = len(cleaned)
    cleaned = cleaned.drop_duplicates(subset=["Case Number"], keep="first")
    cleaned.attrs["duplicates_removed"] = before_dedupe - len(cleaned)

    cleaned["valid_coordinate"] = (
        cleaned["latitude"].between(29.0, 30.3)
        & cleaned["longitude"].between(-96.0, -94.5)
    )
    return cleaned


def build_summaries(df: pd.DataFrame) -> dict[str, pd.DataFrame | dict]:
    totals = (
        df.groupby("issue_type")
        .size()
        .rename("complaints")
        .reset_index()
        .sort_values("complaints", ascending=False)
    )

    monthly = (
        df.groupby(["month", "issue_type"])
        .size()
        .rename("complaints")
        .reset_index()
        .sort_values(["month", "issue_type"])
    )

    zip_summary = (
        df[df["zip_code"] != "Unknown"]
        .pivot_table(
            index="zip_code",
            columns="issue_type",
            values="Case Number",
            aggfunc="count",
            fill_value=0,
        )
        .reset_index()
    )
    for col in ISSUE_TYPES:
        if col not in zip_summary.columns:
            zip_summary[col] = 0
    zip_summary["total_complaints"] = zip_summary["Flooding"] + zip_summary["Pothole"]
    zip_summary["overlap_flag"] = ((zip_summary["Flooding"] > 0) & (zip_summary["Pothole"] > 0)).astype(int)
    zip_summary["priority_score"] = (
        2 * zip_summary["Flooding"] + zip_summary["Pothole"] + 10 * zip_summary["overlap_flag"]
    )
    zip_summary = zip_summary.sort_values("priority_score", ascending=False)

    peak_by_type = {}
    for issue_type, group in monthly.groupby("issue_type"):
        top = group.sort_values("complaints", ascending=False).iloc[0]
        peak_by_type[issue_type] = {
            "month": top["month"],
            "complaints": int(top["complaints"]),
        }

    metrics = {
        "raw_records_loaded": int(df.attrs.get("raw_rows", 0)),
        "filtered_records": int(len(df)),
        "duplicates_removed": int(df.attrs.get("duplicates_removed", 0)),
        "flooding_complaints": int((df["issue_type"] == "Flooding").sum()),
        "pothole_complaints": int((df["issue_type"] == "Pothole").sum()),
        "valid_mapped_records": int(df["valid_coordinate"].sum()),
        "zip_codes_with_overlap": int(zip_summary["overlap_flag"].sum()),
        "date_min": df["created_at"].min().strftime("%Y-%m-%d"),
        "date_max": df["created_at"].max().strftime("%Y-%m-%d"),
        "peak_by_type": peak_by_type,
        "top_priority_zip_codes": zip_summary.head(5)["zip_code"].tolist(),
    }

    return {
        "totals": totals,
        "monthly": monthly,
        "zip_summary": zip_summary,
        "metrics": metrics,
    }


def fmt_int(value: int | float) -> str:
    return f"{int(value):,}"


def write_svg(path: Path, body: str, width: int = 1100, height: int = 700) -> None:
    svg = f"""<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
  <rect width="100%" height="100%" fill="#f8fafc"/>
  {body}
</svg>
"""
    path.write_text(svg, encoding="utf-8")


def svg_text(x: float, y: float, text: str, size: int = 18, weight: str = "400", fill: str = "#0f172a", anchor: str = "start") -> str:
    return f'<text x="{x:.1f}" y="{y:.1f}" font-family="Inter, Arial, sans-serif" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(str(text))}</text>'


def make_kpi_strip(metrics: dict) -> None:
    cards = [
        ("Raw Records", fmt_int(metrics["raw_records_loaded"])),
        ("Filtered Issues", fmt_int(metrics["filtered_records"])),
        ("Mapped Records", fmt_int(metrics["valid_mapped_records"])),
        ("Overlap ZIPs", fmt_int(metrics["zip_codes_with_overlap"])),
    ]
    body = [
        svg_text(40, 62, "Houston 311 Flooding + Pothole Hotspot Analysis", 30, "700"),
        svg_text(40, 96, f"Study window: {metrics['date_min']} to {metrics['date_max']}", 16, "400", "#475569"),
    ]
    colors = ["#c2410c", "#7e22ce", "#334155", "#db2777"]
    for i, ((label, value), color) in enumerate(zip(cards, colors)):
        x = 40 + i * 260
        body.append(f'<rect x="{x}" y="145" width="230" height="150" rx="14" fill="white" stroke="#e2e8f0"/>')
        body.append(f'<rect x="{x}" y="145" width="230" height="8" rx="4" fill="{color}"/>')
        body.append(svg_text(x + 22, 205, value, 34, "800", "#0f172a"))
        body.append(svg_text(x + 22, 242, label, 16, "600", "#475569"))
    write_svg(FIGURES_DIR / "kpi_summary.svg", "\n  ".join(body), 1100, 360)


def make_bar_chart(totals: pd.DataFrame) -> None:
    width, height = 900, 560
    left, top = 140, 110
    bar_h, gap = 80, 36
    max_v = totals["complaints"].max()
    body = [
        svg_text(45, 55, "Pothole Complaints Dominate the Filtered Dataset", 28, "700"),
        svg_text(45, 84, "Complaint totals for exact 311 case types: Flooding and Pothole", 15, "400", "#64748b"),
    ]
    palette = {"Pothole": "#c2410c", "Flooding": "#7e22ce"}
    for i, row in totals.reset_index(drop=True).iterrows():
        y = top + i * (bar_h + gap)
        value = int(row["complaints"])
        label = row["issue_type"]
        bar_w = (value / max_v) * 600
        body.append(svg_text(45, y + 50, label, 18, "700"))
        body.append(f'<rect x="{left}" y="{y}" width="600" height="{bar_h}" rx="12" fill="#e2e8f0"/>')
        body.append(f'<rect x="{left}" y="{y}" width="{bar_w:.1f}" height="{bar_h}" rx="12" fill="{palette.get(label, "#64748b")}"/>')
        body.append(svg_text(left + bar_w + 18, y + 50, fmt_int(value), 20, "700"))
    write_svg(FIGURES_DIR / "complaint_totals.svg", "\n  ".join(body), width, height)


def make_monthly_trend(monthly: pd.DataFrame) -> None:
    pivot = monthly.pivot(index="month", columns="issue_type", values="complaints").fillna(0).sort_index()
    months = list(pivot.index)
    width, height = 1100, 650
    left, right, top, bottom = 85, 45, 105, 105
    plot_w, plot_h = width - left - right, height - top - bottom
    max_v = max(1, pivot.max().max())

    def point(month_idx: int, value: float) -> tuple[float, float]:
        x = left + (month_idx / max(1, len(months) - 1)) * plot_w
        y = top + plot_h - (value / max_v) * plot_h
        return x, y

    body = [
        svg_text(45, 55, "Monthly Complaint Trends", 28, "700"),
        svg_text(45, 84, "Pothole and flooding requests by created month", 15, "400", "#64748b"),
    ]

    for i in range(5):
        y = top + i * plot_h / 4
        value = max_v * (1 - i / 4)
        body.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width-right}" y2="{y:.1f}" stroke="#e2e8f0"/>')
        body.append(svg_text(left - 12, y + 5, str(int(value)), 12, "400", "#64748b", "end"))

    colors = {"Pothole": "#c2410c", "Flooding": "#7e22ce"}
    for issue in ["Pothole", "Flooding"]:
        if issue not in pivot:
            continue
        pts = [point(i, pivot.loc[m, issue]) for i, m in enumerate(months)]
        poly = " ".join(f"{x:.1f},{y:.1f}" for x, y in pts)
        body.append(f'<polyline points="{poly}" fill="none" stroke="{colors[issue]}" stroke-width="4" stroke-linecap="round" stroke-linejoin="round"/>')
        for x, y in pts:
            body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="5" fill="white" stroke="{colors[issue]}" stroke-width="3"/>')
        body.append(f'<rect x="{width-230}" y="{120 + (0 if issue=="Pothole" else 32)}" width="18" height="18" rx="4" fill="{colors[issue]}"/>')
        body.append(svg_text(width - 202, 135 + (0 if issue=="Pothole" else 32), issue, 15, "600", "#334155"))

    for i, m in enumerate(months):
        if i % 2 == 0 or i == len(months) - 1:
            x, _ = point(i, 0)
            body.append(svg_text(x, height - 58, m, 12, "400", "#64748b", "middle"))

    write_svg(FIGURES_DIR / "monthly_trend.svg", "\n  ".join(body), width, height)


def make_top_zip_chart(zip_summary: pd.DataFrame) -> None:
    top10 = zip_summary.head(10).sort_values("priority_score")
    width, height = 1000, 720
    left, top = 135, 110
    plot_w = 710
    bar_h, gap = 38, 18
    max_v = top10["priority_score"].max()
    body = [
        svg_text(45, 55, "Top Priority ZIP Codes", 28, "700"),
        svg_text(45, 84, "Priority score = 2 x flooding + 1 x pothole + 10 x overlap flag", 15, "400", "#64748b"),
    ]
    for i, row in enumerate(top10.itertuples(index=False)):
        y = top + i * (bar_h + gap)
        score = int(row.priority_score)
        bar_w = (score / max_v) * plot_w
        body.append(svg_text(45, y + 26, row.zip_code, 17, "700"))
        body.append(f'<rect x="{left}" y="{y}" width="{plot_w}" height="{bar_h}" rx="10" fill="#e2e8f0"/>')
        body.append(f'<rect x="{left}" y="{y}" width="{bar_w:.1f}" height="{bar_h}" rx="10" fill="#ea580c"/>')
        body.append(svg_text(left + bar_w + 14, y + 25, score, 16, "700"))
        body.append(svg_text(width - 40, y + 25, f"F:{int(row.Flooding)}  P:{int(row.Pothole)}", 13, "600", "#64748b", "end"))
    write_svg(FIGURES_DIR / "top_priority_zip_codes.svg", "\n  ".join(body), width, height)


def make_priority_score_explainer() -> None:
    body = [
        svg_text(45, 60, "Priority Scoring Logic", 30, "700"),
        svg_text(45, 92, "A transparent screening score for ZIP codes where road surface and drainage complaints overlap.", 16, "400", "#64748b"),
        f'<rect x="65" y="140" width="300" height="150" rx="18" fill="white" stroke="#fed7aa" stroke-width="2"/>',
        f'<rect x="400" y="140" width="300" height="150" rx="18" fill="white" stroke="#e9d5ff" stroke-width="2"/>',
        f'<rect x="735" y="140" width="300" height="150" rx="18" fill="white" stroke="#fbcfe8" stroke-width="2"/>',
        svg_text(95, 190, "Pothole Volume", 18, "800", "#9a3412"),
        svg_text(95, 240, "+1 point", 32, "800", "#c2410c"),
        svg_text(95, 267, "per complaint", 14, "600", "#64748b"),
        svg_text(430, 190, "Flooding Volume", 18, "800", "#6b21a8"),
        svg_text(430, 240, "+2 points", 32, "800", "#7e22ce"),
        svg_text(430, 267, "per complaint", 14, "600", "#64748b"),
        svg_text(765, 190, "Overlap Signal", 18, "800", "#be185d"),
        svg_text(765, 240, "+10 points", 32, "800", "#db2777"),
        svg_text(765, 267, "if both types appear", 14, "600", "#64748b"),
        f'<rect x="70" y="340" width="960" height="92" rx="18" fill="#111827"/>',
        svg_text(110, 397, "Priority Score = Potholes + 2 x Flooding + 10 x Overlap Flag", 28, "800", "#ffffff"),
        svg_text(75, 486, "Why this matters:", 20, "800"),
        svg_text(75, 522, "Flooding is weighted higher because drainage-related issues can compound pavement damage and road safety risk.", 16, "400", "#334155"),
        svg_text(75, 552, "The overlap bonus highlights ZIP codes where pothole and flooding reports coexist during the study window.", 16, "400", "#334155"),
    ]
    write_svg(FIGURES_DIR / "priority_score_explanation.svg", "\n  ".join(body), 1100, 610)


def make_scatter_map(df: pd.DataFrame) -> None:
    mapped = df[df["valid_coordinate"]].copy()
    sample = pd.concat([
        mapped[mapped["issue_type"] == "Pothole"].sample(min(3500, (mapped["issue_type"] == "Pothole").sum()), random_state=7),
        mapped[mapped["issue_type"] == "Flooding"],
    ])
    width, height = 900, 780
    left, right, top, bottom = 70, 45, 105, 70
    plot_w, plot_h = width - left - right, height - top - bottom
    lon_min, lon_max = -95.85, -95.0
    lat_min, lat_max = 29.52, 30.12

    def xy(row) -> tuple[float, float]:
        x = left + (row.longitude - lon_min) / (lon_max - lon_min) * plot_w
        y = top + plot_h - (row.latitude - lat_min) / (lat_max - lat_min) * plot_h
        return x, y

    body = [
        svg_text(45, 55, "Spatial Distribution of Road Issue Complaints", 28, "700"),
        svg_text(45, 84, "Valid complaint coordinates; potholes sampled for readability", 15, "400", "#64748b"),
        f'<rect x="{left}" y="{top}" width="{plot_w}" height="{plot_h}" rx="16" fill="white" stroke="#cbd5e1"/>',
    ]

    for _, row in sample.iterrows():
        x, y = xy(row)
        if not (left <= x <= left + plot_w and top <= y <= top + plot_h):
            continue
        color = "#c2410c" if row.issue_type == "Pothole" else "#7e22ce"
        radius = 2.1 if row.issue_type == "Pothole" else 4.3
        opacity = 0.35 if row.issue_type == "Pothole" else 0.72
        body.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{radius}" fill="{color}" opacity="{opacity}"/>')

    body.append(f'<rect x="{width-220}" y="{120}" width="18" height="18" rx="4" fill="#c2410c" opacity="0.75"/>')
    body.append(svg_text(width - 192, 135, "Pothole", 15, "600", "#334155"))
    body.append(f'<rect x="{width-220}" y="{152}" width="18" height="18" rx="4" fill="#7e22ce" opacity="0.8"/>')
    body.append(svg_text(width - 192, 167, "Flooding", 15, "600", "#334155"))
    body.append(svg_text(left, height - 28, "Note: Static coordinate plot, not a basemap. Used for portfolio screening visualization.", 12, "400", "#64748b"))
    write_svg(FIGURES_DIR / "complaint_map.svg", "\n  ".join(body), width, height)


def mini_bar(x: int, y: int, label: str, value: int, max_value: int, color: str, width: int = 420) -> list[str]:
    bar_w = (value / max_value) * width if max_value else 0
    return [
        svg_text(x, y, label, 24, "800", "#111827"),
        f'<rect x="{x}" y="{y + 22}" width="{width}" height="34" rx="10" fill="#e5e7eb"/>',
        f'<rect x="{x}" y="{y + 22}" width="{bar_w:.1f}" height="34" rx="10" fill="{color}"/>',
        svg_text(x + width + 24, y + 49, fmt_int(value), 24, "800", "#111827"),
    ]


def make_updated_poster(metrics: dict, totals: pd.DataFrame, monthly: pd.DataFrame, zip_summary: pd.DataFrame) -> None:
    width, height = 1600, 2200
    top_zips = zip_summary.head(5).reset_index(drop=True)
    max_score = int(top_zips["priority_score"].max())
    max_total = int(totals["complaints"].max())
    peak_pothole = metrics["peak_by_type"]["Pothole"]
    peak_flooding = metrics["peak_by_type"]["Flooding"]

    body: list[str] = [
        '<rect width="1600" height="2200" fill="#f8fafc"/>',
        '<rect x="0" y="0" width="1600" height="250" fill="#111827"/>',
        svg_text(80, 92, "Houston 311 Road Issue Hotspot Analysis", 48, "900", "#ffffff"),
        svg_text(80, 145, "Flooding + pothole complaint overlap screening by ZIP code", 26, "500", "#f9a8d4"),
        svg_text(80, 198, "Official City of Houston 311 data | 2025 to 2026 YTD | Rebuilt portfolio analysis", 21, "400", "#cbd5e1"),
        svg_text(80, 322, "Research Question", 30, "900", "#111827"),
        svg_text(80, 362, "Which Houston ZIP-code areas show recurring flooding and pothole complaints, and which overlap areas should be prioritized for maintenance inspection?", 21, "500", "#334155"),
    ]

    cards = [
        ("Raw records", fmt_int(metrics["raw_records_loaded"]), "#111827"),
        ("Filtered issues", fmt_int(metrics["filtered_records"]), "#c2410c"),
        ("Mapped records", fmt_int(metrics["valid_mapped_records"]), "#7e22ce"),
        ("Overlap ZIPs", fmt_int(metrics["zip_codes_with_overlap"]), "#db2777"),
    ]
    for i, (label, value, color) in enumerate(cards):
        x = 80 + i * 365
        body.append(f'<rect x="{x}" y="420" width="320" height="170" rx="22" fill="white" stroke="#e5e7eb"/>')
        body.append(f'<rect x="{x}" y="420" width="320" height="10" rx="5" fill="{color}"/>')
        body.append(svg_text(x + 28, 500, value, 38, "900", "#111827"))
        body.append(svg_text(x + 28, 543, label, 19, "700", "#64748b"))

    body.extend([
        svg_text(80, 680, "Complaint Mix", 30, "900"),
        '<rect x="80" y="720" width="650" height="345" rx="24" fill="white" stroke="#e5e7eb"/>',
    ])
    for i, row in totals.reset_index(drop=True).iterrows():
        color = "#c2410c" if row.issue_type == "Pothole" else "#7e22ce"
        body.extend(mini_bar(125, 800 + i * 105, row.issue_type, int(row.complaints), max_total, color, 420))

    body.extend([
        svg_text(820, 680, "Peak Reporting Months", 30, "900"),
        '<rect x="820" y="720" width="700" height="345" rx="24" fill="white" stroke="#e5e7eb"/>',
        svg_text(875, 815, "Potholes", 24, "800", "#9a3412"),
        svg_text(875, 875, f"{peak_pothole['month']} | {fmt_int(peak_pothole['complaints'])} complaints", 34, "900", "#c2410c"),
        svg_text(875, 955, "Flooding", 24, "800", "#6b21a8"),
        svg_text(875, 1015, f"{peak_flooding['month']} | {fmt_int(peak_flooding['complaints'])} complaints", 34, "900", "#7e22ce"),
    ])

    body.extend([
        svg_text(80, 1160, "Top ZIP Codes By Priority Score", 30, "900"),
        '<rect x="80" y="1200" width="1440" height="450" rx="24" fill="white" stroke="#e5e7eb"/>',
    ])
    for i, row in top_zips.iterrows():
        y = 1270 + i * 70
        bar_w = (int(row["priority_score"]) / max_score) * 900
        body.append(svg_text(125, y + 28, row["zip_code"], 24, "900", "#111827"))
        body.append(f'<rect x="245" y="{y}" width="900" height="42" rx="12" fill="#e5e7eb"/>')
        body.append(f'<rect x="245" y="{y}" width="{bar_w:.1f}" height="42" rx="12" fill="#db2777"/>')
        body.append(svg_text(1175, y + 29, int(row["priority_score"]), 22, "900", "#111827"))
        body.append(svg_text(1340, y + 29, f"F:{int(row['Flooding'])}  P:{int(row['Pothole'])}", 18, "700", "#64748b"))

    body.extend([
        svg_text(80, 1745, "Method", 30, "900"),
        '<rect x="80" y="1785" width="690" height="250" rx="24" fill="white" stroke="#e5e7eb"/>',
        svg_text(125, 1848, "1. Load 2025 + 2026 YTD public 311 extracts", 21, "700", "#334155"),
        svg_text(125, 1896, "2. Filter exact case types: Flooding and Pothole", 21, "700", "#334155"),
        svg_text(125, 1944, "3. Clean dates, ZIP codes, duplicates, and coordinates", 21, "700", "#334155"),
        svg_text(125, 1992, "4. Aggregate by month and ZIP code", 21, "700", "#334155"),
        svg_text(125, 2040, "5. Rank overlap areas with a transparent score", 21, "700", "#334155"),
        svg_text(850, 1745, "Priority Score", 30, "900"),
        '<rect x="850" y="1785" width="670" height="250" rx="24" fill="#111827"/>',
        svg_text(900, 1880, "Potholes + 2 x Flooding", 32, "900", "#ffffff"),
        svg_text(900, 1935, "+ 10 x Overlap Flag", 32, "900", "#f9a8d4"),
        svg_text(900, 2000, "Use as screening, then validate with field inspection.", 22, "500", "#cbd5e1"),
        svg_text(80, 2120, "Limitations: 311 complaints reflect reported issues, not all defects. ZIP codes are screening units, not final engineering boundaries.", 18, "500", "#475569"),
        svg_text(80, 2160, "Top areas from rebuilt analysis: 77007, 77008, 77082, 77006, 77009.", 20, "800", "#111827"),
    ])
    write_svg(ROOT / "poster" / "houston_311_updated_poster.svg", "\n  ".join(body), width, height)


def export_outputs(df: pd.DataFrame, summaries: dict[str, pd.DataFrame | dict]) -> None:
    PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    FIGURES_DIR.mkdir(parents=True, exist_ok=True)

    sample_cols = ["issue_type", "month", "zip_code", "latitude", "longitude", "valid_coordinate"]
    sample = (
        df[df["valid_coordinate"]]
        .loc[:, sample_cols]
        .sample(min(500, int(df["valid_coordinate"].sum())), random_state=11)
        .sort_values(["issue_type", "zip_code"])
    )
    sample.to_csv(PROCESSED_DIR / "mapped_cases_sample.csv", index=False)
    summaries["totals"].to_csv(PROCESSED_DIR / "complaint_totals.csv", index=False)
    summaries["monthly"].to_csv(PROCESSED_DIR / "monthly_complaint_trends.csv", index=False)
    summaries["zip_summary"].to_csv(PROCESSED_DIR / "hotspot_summary_by_zip.csv", index=False)
    (PROCESSED_DIR / "metrics.json").write_text(json.dumps(summaries["metrics"], indent=2), encoding="utf-8")

    make_kpi_strip(summaries["metrics"])
    make_bar_chart(summaries["totals"])
    make_monthly_trend(summaries["monthly"])
    make_top_zip_chart(summaries["zip_summary"])
    make_priority_score_explainer()
    make_scatter_map(df)
    make_updated_poster(summaries["metrics"], summaries["totals"], summaries["monthly"], summaries["zip_summary"])


def main() -> None:
    raw_filtered = read_311_files()
    cleaned = clean_data(raw_filtered)
    summaries = build_summaries(cleaned)
    export_outputs(cleaned, summaries)
    print(json.dumps(summaries["metrics"], indent=2))


if __name__ == "__main__":
    main()
