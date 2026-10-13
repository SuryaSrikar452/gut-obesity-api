"""Microbiome CSV validation + CLR preprocessing.

The two recommendation models (phytochemical + food) were trained on a cohort
table whose 20 microbial columns are PERCENT relative abundance of the whole
gut community (the 20 species sum to 0-78 %, median ~16.6 %, NOT 100 %).
Preprocessing used at training time: CLR with a 1e-3 pseudocount over those 20
columns, in the order below. Nothing here changes that contract.
"""
import io
import numpy as np
import pandas as pd

# Exact feature order of fbg_input_phyto.xlsx / fbg_input_food.xlsx (verified in tests).
SPECIES = [
    "Roseburia_intestinalis", "Clostridium_bolteae", "Bacteroides_vulgatus",
    "Bacteroides_dorei", "Clostridium_hathewayi", "Haemophilus_parainfluenzae",
    "Bifidobacterium_longum", "Veillonella_unclassified", "Eggerthella_lenta",
    "Streptococcus_salivarius", "Prevotella_copri", "Bacteroides_ovatus",
    "Bacteroides_thetaiotaomicron", "Eubacterium_rectale", "Streptococcus_anginosus",
    "Gordonibacter_pamelaeae", "Clostridium_asparagiforme", "Dorea_formicigenerans",
    "Escherichia_unclassified", "Eubacterium_ventriosum",
]
PSEUDOCOUNT = 1e-3
MAX_BYTES = 2 * 1024 * 1024
MAX_SAMPLES = 200
ID_COLUMNS = ["sample_id", "sampleid", "sample", "id", "sample_name", "table_s1_sample_id"]


class CsvValidationError(ValueError):
    """Raised with a user-facing message; `details` holds per-field problems."""
    def __init__(self, message, details=None):
        super().__init__(message)
        self.details = details or []


def clr(M):
    """Centered log-ratio, identical to the transform used when the models were trained."""
    M = np.asarray(M, float)
    lg = np.log(M + PSEUDOCOUNT)
    return lg - lg.mean() if M.ndim == 1 else lg - lg.mean(axis=1, keepdims=True)


def parse_microbiome_csv(raw: bytes):
    """Validate an uploaded CSV. Returns {"samples": [{sample_id, abundances(list of 20)}],
    "warnings": [...], "extra_columns": [...], "id_column": str|None}.
    Never fills in or substitutes any value; any problem raises CsvValidationError."""
    if not raw:
        raise CsvValidationError("The file is empty.")
    if len(raw) > MAX_BYTES:
        raise CsvValidationError(f"File is too large (limit {MAX_BYTES // 1024} KB).")
    if b"\x00" in raw[:4096]:
        raise CsvValidationError("That does not look like a text CSV file.")
    try:
        df = pd.read_csv(io.BytesIO(raw), dtype=str, skipinitialspace=True, encoding="utf-8-sig")
    except Exception:
        raise CsvValidationError("The file could not be parsed as CSV.")
    df.columns = [str(c).strip() for c in df.columns]
    if df.empty:
        raise CsvValidationError("The CSV has a header but no samples.")
    if len(df) > MAX_SAMPLES:
        raise CsvValidationError(f"Too many samples (limit {MAX_SAMPLES}).")
    if len(set(df.columns)) != len(df.columns):
        raise CsvValidationError("Duplicate column names found.")

    missing = [s for s in SPECIES if s not in df.columns]
    if missing:
        raise CsvValidationError(
            f"CSV is missing {len(missing)} of the {len(SPECIES)} required species columns.",
            details=missing)

    id_col = next((c for c in df.columns if c.lower() in ID_COLUMNS and c not in SPECIES), None)
    ids = [str(v).strip() for v in df[id_col]] if id_col else [f"row_{i + 1}" for i in range(len(df))]
    if id_col and (any(not i or i.lower() == "nan" for i in ids)):
        raise CsvValidationError(f"Column '{id_col}' has empty sample identifiers.")
    if len(set(ids)) != len(ids):
        raise CsvValidationError("Sample identifiers must be unique.")

    block = df[SPECIES].apply(lambda c: pd.to_numeric(c, errors="coerce"))
    bad = []
    for r in range(len(block)):
        for c in SPECIES:
            v = block.iloc[r][c]
            raw_v = df.iloc[r][c]
            if pd.isna(raw_v) or str(raw_v).strip() == "":
                bad.append(f"{ids[r]}: '{c}' is empty")
            elif pd.isna(v) or not np.isfinite(v):
                bad.append(f"{ids[r]}: '{c}' is not a finite number")
            elif v < 0:
                bad.append(f"{ids[r]}: '{c}' is negative")
    if bad:
        raise CsvValidationError("Abundance values must be non-negative numbers with none missing.", bad[:20])

    X = block.values.astype(float)
    over = [ids[i] for i in range(len(X)) if X[i].max() > 100 or X[i].sum() > 100 + 1e-6]
    if over:
        raise CsvValidationError(
            "Values look like raw counts or are above 100 %. The models expect percent relative "
            "abundance of the whole community (the 20 species together must be <= 100).", over[:20])
    empty = [ids[i] for i in range(len(X)) if X[i].sum() == 0]
    if empty:
        raise CsvValidationError(
            "These samples have zero abundance for all 20 species, so they carry no usable signal.", empty[:20])

    warnings = []
    if X.sum(axis=1).max() <= 1.0:
        warnings.append("All sample totals are <= 1. If your values are proportions (0-1) rather than "
                        "percent, results will be wrong; multiply by 100 before uploading.")
    extra = [c for c in df.columns if c not in SPECIES and c != id_col]
    return {
        "samples": [{"sample_id": ids[i], "abundances": [float(v) for v in X[i]]} for i in range(len(X))],
        "warnings": warnings, "extra_columns": extra, "id_column": id_col,
    }
