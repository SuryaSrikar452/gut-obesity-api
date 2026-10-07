"""
===============================================================================
(Age, Height, Weight, FBG) -> Microbial Profile -> Top 3 Enzymes -> Food Compound
Machine Learning Pipeline: XGBoost Multi-Class Classifier + CLR Microbiome
FOOD INTERVENTION VERSION
===============================================================================

Workflow Overview:
------------------
1. User Inputs:
   - Takes Age (years), Height (cm), Weight (kg), and Fasting Blood Glucose (mg/dL).
   - Computes Body Mass Index: BMI = weight / (height in meters)^2.

2. Patient Matching & Microbiome Selection:
   - Users typically do not have a stool microbiome test.
   - We find the most clinically similar patient in 'fbg_input_food.xlsx'
     by calculating the standardized Euclidean distance over [Age, BMI, FBG].
   - We adopt this matched patient's 20 gut microbial relative abundances.

3. Centered Log-Ratio (CLR) Transformation:
   - Relative abundances in microbiome data sum to 100% (compositional data).
   - CLR transforms these compositional abundances to unconstrained real values
     using a small pseudo-count (1e-3) to handle zeros safely.

4. XGBoost Multi-Class Classification:
   - Features: [User Age, User BMI, User FBG, 20 CLR-transformed Microbes].
   - XGBoost Classifier (tree stumps: max_depth=1, strong L2 penalty: reg_lambda=10.0)
     outputs calibrated probabilities across all 10 EC enzymes without overfitting.

5. Top 3 Enzyme Ranking & Food Compound Matching:
   - Enzymes are ranked by predicted probability from highest to lowest.
   - For the top enzyme, we query 'IC50_Matches_food.xlsx' to find the food
     compound with the highest pIC50 potency, along with its dietary food sources.
   - Rows with the invalid placeholder IC50 2147483647 are safely excluded.
   - We also provide interventions for all top-3 ranked enzymes.

Execution:
----------
    py -3.13 fbg_food_model.py train                          # Train model & show 5-fold CV
    py -3.13 fbg_food_model.py predict 55 165 72 150          # Direct CLI prediction
    py -3.13 fbg_food_model.py predict                        # Interactive prompt
===============================================================================
"""
import sys
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.model_selection import StratifiedKFold
from xgboost import XGBClassifier

# --------------------------------------------------------------------------- #
# Paths & Global Configurations
# --------------------------------------------------------------------------- #
# Resolve the directory containing this script (d:/Nutra/.../Food)
BASE = Path(__file__).resolve().parent

# Path to the cohort Excel dataset containing clinical stats, microbes, and 10 enzymes
DATA_PATH = BASE / "fbg_input_food.xlsx"

# Path to save/load the trained machine learning bundle
MODEL_PATH = BASE / "fbg_food_xgb.joblib"

# Path to the reference database of food bioactive compounds and food sources
FOOD_PATH = BASE / "IC50_Matches_food.xlsx"

# Internal model version; incrementing this automatically triggers retraining
MODEL_VERSION = 3

# Metadata identifier columns that are not predictive features
ID_COLS = ["Sample_ID", "Table_S1_Sample_ID"]

# Excluded columns (e.g., HbA1c is not collected from the user)
DROP_COLS = ["HbA1c"]

# The 3 clinical features supplied by the user (used to match reference patients)
CLINICAL_COLS = ["Age", "BMI", "FBG_mgdL"]
FBG_COL = "FBG_mgdL"

# Number of top enzymes to rank and present
TOP_K = 3

# Invalid 32-bit integer maximum placeholder found in some raw IC50 databases
INVALID_IC50 = 2147483647

# XGBoost hyperparameters:
# - max_depth=1 (tree stumps) prevents over-fitting to noisy small-sample features.
# - reg_lambda=10.0 enforces strong L2 regularization.
# - objective="multi:softprob" outputs probabilities across all target classes.
XGB_PARAMS = dict(
    n_estimators=150,
    max_depth=1,
    learning_rate=0.03,
    subsample=0.7,
    colsample_bytree=0.5,
    min_child_weight=8,
    reg_lambda=10.0,
    tree_method="hist",
    objective="multi:softprob",
    random_state=42,
)


# --------------------------------------------------------------------------- #
# Clinical & Formatting Helpers
# --------------------------------------------------------------------------- #
def compute_bmi(height_cm, weight_kg):
    """Calculate Body Mass Index (BMI) in kg/m^2 from height in cm and weight in kg."""
    h_meters = height_cm / 100.0
    return weight_kg / (h_meters * h_meters)


def bmi_category(bmi):
    """Categorize BMI into standard World Health Organization (WHO) ranges."""
    if bmi < 18.5:
        return "Underweight"
    elif bmi < 25.0:
        return "Normal weight"
    elif bmi < 30.0:
        return "Overweight"
    else:
        return "Obese"


def fbg_category(fbg):
    """Categorize Fasting Blood Glucose (mg/dL) into standard clinical ranges."""
    if fbg < 100:
        return "Normal"
    elif fbg < 126:
        return "Impaired Fasting Glucose (Prediabetes)"
    else:
        return "Elevated (Diabetes range)"


def pretty_ec(col):
    """Format column names like 'EC_3.2.2.20_Abundance' into clean labels like 'EC 3.2.2.20'."""
    return col.replace("_Abundance", "").replace("EC_", "EC ")


def clr_transform(M):
    """
    Apply Centered Log-Ratio (CLR) transformation to compositional microbial data.
    
    Formula: CLR(x_i) = log(x_i + eps) - mean(log(x + eps))
    Why: Relative abundances sum to a constant (100%), which creates spurious
    correlations in raw counts. CLR projects compositional data into Euclidean space.
    """
    M = np.asarray(M, float)
    if M.ndim == 1:
        log_m = np.log(M + 1e-3)               # Add 1e-3 pseudo-count to handle zeros
        return log_m - log_m.mean()            # Subtract geometric mean in log-space
    log_m = np.log(M + 1e-3)
    return log_m - log_m.mean(axis=1, keepdims=True)


def nearest_patient(query, ref_clin, mu, sd):
    """
    Find the most similar patient in the cohort using z-score standardized Euclidean distance.
    
    query    : [Age, BMI, FBG] of the user.
    ref_clin : [Age, BMI, FBG] of all patients in the database.
    mu, sd   : Mean and standard deviation of clinical variables in the cohort.
    
    Returns: (index_of_best_match, distance_value)
    """
    # Standardize both query and reference points so all 3 features have equal weight
    zq = (np.asarray(query, float) - mu) / sd
    zr = (ref_clin - mu) / sd
    
    # Compute Euclidean distance across Age, BMI, and FBG
    d = np.sqrt(((zr - zq) ** 2).sum(axis=1))
    best_idx = int(np.argmin(d))
    return best_idx, float(d[best_idx])


def build_features(age, bmi, fbg, raw_microbes):
    """Combine user clinical variables with CLR-transformed microbial abundances."""
    clr_mic = clr_transform(raw_microbes)
    return np.concatenate([[age, bmi, fbg], clr_mic])


# --------------------------------------------------------------------------- #
# Data Loaders
# --------------------------------------------------------------------------- #
def load_data():
    """
    Load the cohort Excel file and split columns into:
    - Feature columns: [Age, BMI, FBG_mgdL, 20 species of microbes]
    - Microbe columns: list of the 20 microbial species
    - Target columns: list of the 10 EC enzyme abundance columns
    """
    df = pd.read_excel(DATA_PATH)
    
    # Target columns start with 'EC_'
    target_cols = [c for c in df.columns if c.startswith("EC_")]
    
    # Microbe columns are everything except IDs, dropped features, clinical, and targets
    microbe_cols = [c for c in df.columns
                    if c not in ID_COLS + DROP_COLS + CLINICAL_COLS + target_cols]
    
    feature_cols = CLINICAL_COLS + microbe_cols
    return df, feature_cols, microbe_cols, target_cols


def load_food():
    """
    Load the food bioactive compounds spreadsheet and drop rows with invalid IC50 placeholders.
    """
    fd = pd.read_excel(FOOD_PATH, sheet_name=0)
    fd["EC_Number"] = fd["EC_Number"].astype(str).str.strip()
    fd["Enzyme_Definition"] = fd["Enzyme_Definition"].astype(str).str.strip()
    
    # Filter out entries where IC50 has the 2147483647 dummy value
    fd = fd[fd["IC50_nM"] != INVALID_IC50]
    return fd.reset_index(drop=True)


def get_enzyme_definitions(fd=None):
    """Build a lookup dictionary mapping enzyme codes like 'EC 3.2.2.20' to readable names."""
    fd = load_food() if fd is None else fd
    mapping = {}
    for _, row in fd.iterrows():
        ec_key = f"EC {row['EC_Number']}"
        if ec_key not in mapping and pd.notna(row.get("Enzyme_Definition")):
            mapping[ec_key] = str(row["Enzyme_Definition"]).strip()
    return mapping


def best_food_for_enzyme(ec_label, fd=None):
    """Query the food database for an enzyme and sort by pIC50 potency descending."""
    fd = load_food() if fd is None else fd
    ec_no = ec_label.replace("EC ", "").strip()
    return fd[fd["EC_Number"] == ec_no].sort_values("pic50", ascending=False).reset_index(drop=True)


# --------------------------------------------------------------------------- #
# Model Training & Cross-Validation
# --------------------------------------------------------------------------- #
def train(verbose=True):
    """
    Train the XGBoost Classifier and validate it using 5-Fold Stratified Cross-Validation.
    
    Cross-Validation strictly evaluates the real pipeline:
    - Held-out test samples only supply Age, BMI, and FBG.
    - Their gut profile is borrowed from the nearest training patient.
    - Prevents data leakage and reports honest out-of-fold generalization accuracy.
    """
    df, feat, microbes, targ = load_data()
    
    # Extract clinical data and microbial matrix
    clin = df[CLINICAL_COLS].values.astype(float)
    mic = df[microbes].values.astype(float)
    
    # Compute CLR-transformed features for full training
    clr_mic = clr_transform(mic)
    X = np.hstack([clin, clr_mic])

    # Convert continuous enzyme levels to cohort z-scores to find each sample's top enzyme
    mu, sd = df[targ].mean(), df[targ].std()
    Y = ((df[targ] - mu) / sd).values
    true_top = Y.argmax(axis=1)                                           # The true #1 enzyme index
    true_top3 = [set(np.argsort(-y)[:TOP_K]) for y in Y]                 # The true top-3 enzyme indices
    n_classes = len(targ)

    # 5-Fold Stratified Cross-Validation
    skf = StratifiedKFold(n_splits=5, shuffle=True, random_state=42)
    oof_pipe_probs = np.zeros((len(df), n_classes))

    for tr, te in skf.split(X, true_top):
        # Fit classifier on the training fold
        model = XGBClassifier(**XGB_PARAMS).fit(X[tr], true_top[tr])

        # Clinical standardization statistics strictly derived from the training fold
        c_mu, c_sd = clin[tr].mean(axis=0), clin[tr].std(axis=0, ddof=1)
        
        # Test samples only supply Age, BMI, FBG; borrow microbes from nearest training patient
        Xp = []
        for i in te:
            j, _ = nearest_patient(clin[i], clin[tr], c_mu, c_sd)
            Xp.append(build_features(*clin[i], mic[tr][j]))
        Xp = np.array(Xp)

        # Predict probabilities on the test fold
        p_pipe = np.zeros((len(te), n_classes))
        p_pipe[:, model.classes_] = model.predict_proba(Xp)
        oof_pipe_probs[te] = p_pipe

    # Calculate validation metrics across all out-of-fold predictions
    rk = np.argsort(-oof_pipe_probs, axis=1)
    p1 = float((rk[:, 0] == true_top).mean())
    p3 = float(np.mean([true_top[i] in rk[i, :TOP_K] for i in range(len(true_top))]))
    pov = float(np.mean([len(set(rk[i, :TOP_K]) & true_top3[i]) for i in range(len(true_top))]))

    metrics = dict(
        n_rows=len(df), n_features=len(feat), n_classes=n_classes,
        pipe_top1=p1, pipe_top3=p3, pipe_overlap3=pov,
    )

    # Fit final model on all data for future predictions
    final_model = XGBClassifier(**XGB_PARAMS).fit(X, true_top)
    
    # Save model and precomputed normalization parameters to disk
    joblib.dump(
        dict(version=MODEL_VERSION, model=final_model, features=feat, microbes=microbes,
             targets=targ, mu=mu, sd=sd, metrics=metrics,
             clin_mu=clin.mean(axis=0), clin_sd=clin.std(axis=0, ddof=1)),
        MODEL_PATH,
    )

    if verbose:
        print("=" * 80)
        print("                   MODEL TRAINING & VALIDATION SUMMARY")
        print("=" * 80)
        print(f"  Cohort Dataset       : {len(df)} samples | {len(feat)} features | {n_classes} target enzymes")
        print(f"  Validation Strategy  : 5-Fold Stratified Cross-Validation (Full Pipeline)")
        print(f"  Top-1 Exact Match    : {p1:.1%}  (Random baseline: {1/n_classes:.1%})")
        print(f"  Top-3 Enzyme Coverage: {p3:.1%}  (Random baseline: {TOP_K/n_classes:.1%})")
        print(f"  Top-3 Average Overlap: {pov:.2f} / {TOP_K} enzymes")
        print(f"  Saved Model Bundle   : {MODEL_PATH.name}")
        print("=" * 80)


def load_bundle():
    """Load the trained model bundle from disk; automatically train if missing."""
    bundle = joblib.load(MODEL_PATH) if MODEL_PATH.exists() else None
    if bundle is None or bundle.get("version") != MODEL_VERSION:
        train(verbose=False)
        bundle = joblib.load(MODEL_PATH)
    return bundle


# --------------------------------------------------------------------------- #
# Prediction & Clean Terminal Display
# --------------------------------------------------------------------------- #
def predict(age, height_cm, weight_kg, fbg, top_k=TOP_K):
    """
    Main prediction entry point for dietary food interventions.
    1. Computes BMI from height and weight.
    2. Identifies nearest reference patient by clinical similarity.
    3. Transforms matched microbial profile via CLR.
    4. Predicts enzyme class probabilities using XGBoost.
    5. Displays clean, formatted terminal report with food recommendations.
    """
    bundle = load_bundle()
    df, _, _, _ = load_data()
    microbes, targ = bundle["microbes"], bundle["targets"]
    fd = load_food()
    ec_names = get_enzyme_definitions(fd)

    # 1. Compute BMI and identify nearest cohort patient
    bmi = compute_bmi(height_cm, weight_kg)
    j, dist = nearest_patient([age, bmi, fbg], df[CLINICAL_COLS].values.astype(float),
                              bundle["clin_mu"], bundle["clin_sd"])
    match = df.iloc[j]
    mic = match[microbes].values.astype(float)

    # 2. Build feature vector and predict probabilities
    x = build_features(age, bmi, fbg, mic).reshape(1, -1)
    probs = np.zeros(len(targ))
    probs[bundle["model"].classes_] = bundle["model"].predict_proba(x)[0]

    # Rank enzymes in descending order of predicted probability
    ranking = pd.DataFrame({
        "Enzyme": [pretty_ec(c) for c in targ],
        "Probability": probs,
    }).sort_values("Probability", ascending=False).reset_index(drop=True)

    # Identify the #1 enzyme and lookup its top food bioactive compound
    top_enzyme = ranking.iloc[0]["Enzyme"]
    top_cands = best_food_for_enzyme(top_enzyme, fd)
    best_food = top_cands.iloc[0] if not top_cands.empty else None

    # ----------------------------------------------------------------------- #
    # Clean Output Presentation
    # ----------------------------------------------------------------------- #
    print("\n" + "=" * 80)
    print("                         NUTRA ENZYME & FOOD PREDICTOR")
    print("=" * 80)

    # [1] Patient Profile & Matching
    print("\n[1] PATIENT PROFILE")
    print("-" * 80)
    print(f"  Age           : {int(age)} years")
    print(f"  Height / Weight: {height_cm:.1f} cm / {weight_kg:.1f} kg")
    print(f"  BMI           : {bmi:.2f} kg/m2 ({bmi_category(bmi)})")
    print(f"  Fasting Blood : {fbg:.1f} mg/dL ({fbg_category(fbg)})")
    print(f"  Cohort Match  : {match['Sample_ID']} ({match['Table_S1_Sample_ID']}) | "
          f"Age {int(match['Age'])}, BMI {match['BMI']:.1f}, FBG {match[FBG_COL]:.1f} mg/dL")

    # [2] Top 3 Target Enzymes
    print("\n[2] TOP 3 TARGET ENZYMES (Ranked by Model Probability)")
    print("-" * 80)
    print(f"  {'Rank':<6} {'Enzyme':<15} {'Probability':<14} {'Enzyme Name'}")
    print("  " + "-" * 76)
    for idx in range(min(top_k, len(ranking))):
        row = ranking.iloc[idx]
        enz = row["Enzyme"]
        prob_str = f"{row['Probability']:.2%}"
        def_str = ec_names.get(enz, "Metabolic enzyme")
        if len(def_str) > 40:
            def_str = def_str[:37] + "..."
        print(f"  #{idx+1:<5} {enz:<15} {prob_str:<14} {def_str}")

    # [3] Primary Recommendation for Top Enzyme
    print("\n[3] PRIMARY RECOMMENDATION (Targeting #1 Enzyme: " + top_enzyme + ")")
    print("-" * 80)
    if best_food is not None:
        print(f"  Food Compound : {best_food['Food_Compound']}")
        print(f"  Potency Score : pIC50 = {best_food['pic50']:.2f}  (IC50 = {int(best_food['IC50_nM']):,} nM)")
        print(f"  Target Enzyme : {top_enzyme} ({best_food.get('Enzyme_Definition', '')[:45]})")
        print("  Food Sources  :")
        src_list = [s.strip() for s in str(best_food["food source"]).split(";") if s.strip()]
        for src in src_list:
            print(f"    - {src}")
    else:
        print(f"  No food compound mapping found for {top_enzyme}.")

    # [4] Interventions for All Top 3 Targets
    print("\n[4] INTERVENTIONS FOR ALL TOP 3 TARGETS")
    print("-" * 80)
    print(f"  {'Rank':<6} {'Enzyme':<14} {'Food Compound':<22} {'pIC50':<8} {'Food Sources'}")
    print("  " + "-" * 76)
    for idx in range(min(top_k, len(ranking))):
        enz = ranking.iloc[idx]["Enzyme"]
        c = best_food_for_enzyme(enz, fd)
        if not c.empty:
            cand = c.iloc[0]
            src_str = str(cand["food source"])
            if len(src_str) > 28:
                src_str = src_str[:25] + "..."
            comp_str = str(cand["Food_Compound"])
            if len(comp_str) > 20:
                comp_str = comp_str[:17] + "..."
            print(f"  #{idx+1:<5} {enz:<14} {comp_str:<22} {cand['pic50']:<8.2f} {src_str}")
        else:
            print(f"  #{idx+1:<5} {enz:<14} {'(No match)':<22} {'-':<8} -")
    print("=" * 80 + "\n")

    return {"bmi": bmi, "matched_patient": match["Sample_ID"], "ranking": ranking, "food": best_food}


# --------------------------------------------------------------------------- #
# Script Command-Line Interface
# --------------------------------------------------------------------------- #
if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "predict"
    if cmd == "train":
        train(verbose=True)
    elif cmd == "predict":
        # Check if clinical values are passed directly via command line arguments
        if len(sys.argv) >= 6:
            a, h, w, f = map(float, sys.argv[2:6])
        else:
            # Interactive prompt if values are omitted
            print("\nEnter Patient Details:")
            a = float(input("  Age (years)      : "))
            h = float(input("  Height (cm)      : "))
            w = float(input("  Weight (kg)      : "))
            f = float(input("  FBG (mg/dL)      : "))
        predict(a, h, w, f)
    else:
        print(__doc__)
