# Clinical & Microbiome to Enzyme / Intervention Models

This project predicts enzyme profile rankings from clinical inputs (**Age, Height, Weight, Fasting Blood Glucose**) and provides targeted dietary/therapeutic recommendations based on IC50 values.

---

## Folder Structure

```
hba1c_input_phyto_output_model/
├── README.md
│
├── Food/
│   ├── fbg_food_model.py                # Main model script for food recommendations
│   ├── fbg_food_xgb.joblib              # Trained XGBoost classifier bundle
│   ├── fbg_input_food.xlsx              # Cohort dataset with 10 EC enzymes
│   └── IC50_Matches_food.xlsx           # Food compound database & IC50 mappings
│
└── Phytochemicals/
    ├── fbg_phyto_model.py               # Main model script for phytochemical recommendations
    ├── fbg_phyto_xgb.joblib             # Trained XGBoost classifier bundle
    ├── fbg_input_phyto.xlsx             # Cohort dataset with 6 EC enzymes
    └── EC_Phytochemical_pIC50_12_rows_t2d.xlsx  # Phytochemical database & plant source mappings
```

---

## How to Run

### 1. Food Model
Navigate to the `Food` folder:
```powershell
cd Food
py -3.13 fbg_food_model.py predict 55 165 72 150
```
Or run interactively (prompts for Age, Height, Weight, FBG):
```powershell
py -3.13 fbg_food_model.py predict
```

### 2. Phytochemical Model
Navigate to the `Phytochemicals` folder:
```powershell
cd Phytochemicals
py -3.13 fbg_phyto_model.py predict 55 165 72 150
```
Or run interactively:
```powershell
py -3.13 fbg_phyto_model.py predict
```

---

## Model Pipeline

1. **Input Processing**: Computes BMI from height (cm) and weight (kg).
2. **Microbiome Profile Matching**: Finds the nearest reference patient by standardized Euclidean distance over `[Age, BMI, FBG_mgdL]` and assigns their 20-species microbial relative abundance profile.
3. **Centered Log-Ratio (CLR)**: Microbiome abundances are transformed using CLR with a pseudo-count.
4. **XGBoost Classifier**: Predicts class probability distributions over the target enzymes using tree stumps and L2 regularization.
5. **Top-3 Ranking & Lookup**: Identifies the top 3 enzymes by predicted probability and maps them to the most potent compounds (highest pIC50) with their corresponding sources.
