# Dashboard integration

`POST /dashboard-data` accepts form fields `device_id`, `glucose`, `age`,
`height_cm`, and `weight_kg`. It requires a device already claimed in Supabase
and returns the submitted glucose, the latest gut prediction for that device,
and phytochemical and food recommendations.

The prediction portion is returned under `gut` (`risk_percent`, `top_microbes`,
and `created_at`). Recommendation data is returned under `recommendations`
(`bmi`, `bmi_category`, `fbg_category`, `phyto`, and `food`). If the device has
no prediction yet, the gut risk and timestamp are `null`, and `top_microbes` is
empty.

**Glucose/FBG limitation:** the endpoint passes `glucose` to the existing
recommendation model as its FBG input. The model is trained and categorized
using fasting blood glucose; a general or sensor glucose reading is not
necessarily fasting and should not be interpreted as equivalent.
